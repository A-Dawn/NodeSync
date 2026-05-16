"""NodeSync WebSocket 客户端运行时。"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Protocol

from aiohttp import ClientSession, ClientTimeout, ClientWebSocketResponse, WSMsgType

from nodesync.core.directive_result_store import DirectiveResultStore
from nodesync.core.injection_store import InjectionStore
from nodesync.core.runtime_config import NodeSyncConfig
from nodesync.shared.jsonio import dumps_json, loads_json
from nodesync.shared.logging_utils import get_logger
from nodesync.shared.models import (
    BotCapability,
    ChatMessageEvent,
    ClientRegistration,
    ContextRequest,
    ContextResponse,
    Directive,
    DirectiveResult,
    DirectiveType,
    Envelope,
    InjectionRecord,
    InjectionStatus,
    LLMRequest,
    LLMResponse,
)
from nodesync.shared.texts import build_safe_advance_reply_fallback, clean_advance_reply_content
from nodesync.shared.time_utils import now_ts

logger = get_logger("client.runtime")


class NodeSyncBridge(Protocol):
    """MaiBot 适配层需要提供的最小能力集合。"""

    def get_streams(self) -> list[str]:
        """返回当前 bot 已知或希望注册的 stream 列表。"""

    async def fetch_context(self, request: ContextRequest) -> ContextResponse:
        """按服务端要求拉取群聊上下文。"""

    async def generate_llm(self, request: LLMRequest) -> LLMResponse:
        """委托本机 MaiBot 的 LLM 能力生成内容。"""

    async def apply_context_injection(self, directive: Directive, record: InjectionRecord) -> bool:
        """应用私有上下文注入，返回是否成功。"""

    async def send_message(self, directive: Directive) -> bool:
        """按服务端指令在群内发送推进消息。"""

    async def render_advance_message(self, directive: Directive) -> str:
        """把服务端推进意图渲染成符合当前 bot 人设的群内最终发言。"""


class NodeSyncClientRuntime:
    """连接 NodeSync server，并把 MaiBot 能力暴露给协调服务。"""

    def __init__(self, config: NodeSyncConfig, bridge: NodeSyncBridge):
        self.config = config
        self.bridge = bridge
        self.injections = InjectionStore(config.injection_path)
        self.directive_results = DirectiveResultStore(config.directive_result_path)
        self._session: ClientSession | None = None
        self._ws: ClientWebSocketResponse | None = None
        self._main_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._send_lock = asyncio.Lock()

    async def start(self) -> None:
        """启动客户端后台连接循环。"""

        if self._main_task and not self._main_task.done():
            return
        self._stop_event.clear()
        timeout = ClientTimeout(total=15)
        self._session = ClientSession(timeout=timeout)
        self._main_task = asyncio.create_task(self._run_loop(), name="nodesync-client")

    async def stop(self) -> None:
        """停止客户端连接并释放资源。"""

        self._stop_event.set()
        for task in (self._heartbeat_task, self._main_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def report_message(self, event: ChatMessageEvent) -> bool:
        """上报一条群聊消息。"""

        if self._ws is None or self._ws.closed:
            return False
        envelope = Envelope(type="chat.event", payload=event.to_dict())
        return await self._send(envelope)

    def get_active_injection(self, stream_id: str, session_id: str = "") -> InjectionRecord | None:
        """读取当前 stream 可用的最新上下文注入。"""

        return self.injections.get_active(stream_id=stream_id, session_id=session_id)

    async def _run_loop(self) -> None:
        retry_delay = 1.0
        while not self._stop_event.is_set():
            try:
                await self._connect_once()
                retry_delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[NodeSyncClient] 连接循环异常: %s", exc, exc_info=True)
            if not self._stop_event.is_set():
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.8, 30.0)

    async def _connect_once(self) -> None:
        if self._session is None:
            raise RuntimeError("NodeSyncClient 尚未创建 aiohttp session")
        headers = {"Authorization": f"Bearer {self.config.auth_token}"}
        ws = await self._session.ws_connect(self.config.ws_url, headers=headers, heartbeat=20)
        self._ws = ws
        logger.info("[NodeSyncClient] 已连接服务端: %s", self.config.ws_url)
        await self._register()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="nodesync-heartbeat")
        try:
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    await self._handle_raw_message(message.data)
                elif message.type in {WSMsgType.ERROR, WSMsgType.CLOSED, WSMsgType.CLOSE}:
                    break
        finally:
            if self._heartbeat_task and not self._heartbeat_task.done():
                self._heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._heartbeat_task
            self._ws = None
            await ws.close()
            logger.info("[NodeSyncClient] 服务端连接已断开")

    async def _register(self) -> None:
        registration = ClientRegistration(
            bot_id=self.config.bot_id,
            bot_name=self.config.bot_name,
            maibot_version=self.config.maibot_version,
            adapter_version=self.config.adapter_version,
            capabilities=[
                BotCapability.MESSAGE_REPORT.value,
                BotCapability.CONTEXT_FETCH.value,
                BotCapability.LLM_GENERATE.value,
                BotCapability.CONTEXT_INJECTION.value,
                BotCapability.SEND_MESSAGE.value,
            ],
            streams=self.bridge.get_streams(),
        )
        await self._send(Envelope(type="client.register", payload=registration.to_dict()))

    async def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(15)
            await self._send(Envelope(type="client.heartbeat", payload={"bot_id": self.config.bot_id}))

    async def _handle_raw_message(self, raw: str) -> None:
        try:
            envelope = Envelope.from_dict(loads_json(raw))
        except Exception as exc:
            logger.warning("[NodeSyncClient] 忽略无法解析的服务端消息: %s", exc)
            return

        if envelope.type == "context.request":
            await self._handle_context_request(envelope)
        elif envelope.type == "llm.request":
            await self._handle_llm_request(envelope)
        elif envelope.type == "directive.push":
            await self._handle_directive(envelope)
        elif envelope.type in {"server.ack", "server.error"}:
            return
        else:
            logger.debug("[NodeSyncClient] 忽略未知消息类型: %s", envelope.type)

    async def _handle_context_request(self, envelope: Envelope) -> None:
        request = ContextRequest.from_dict(envelope.payload)
        try:
            if not self._handles_stream(request.stream_id):
                response = ContextResponse(session_id=request.session_id, stream_id=request.stream_id, messages=[])
            else:
                response = await asyncio.wait_for(self.bridge.fetch_context(request), timeout=12)
            if request.include_injection_history:
                response.injection_history = [
                    item.to_dict()
                    for item in self.injections.get_recent(
                        stream_id=request.stream_id,
                        session_id=request.session_id,
                        limit=request.injection_history_limit,
                    )
                ]
        except Exception as exc:
            logger.warning("[NodeSyncClient] 拉取上下文失败: %s", exc, exc_info=True)
            response = ContextResponse(session_id=request.session_id, stream_id=request.stream_id, messages=[])
        await self._send(
            Envelope(type="context.response", request_id=envelope.request_id, payload=response.to_dict())
        )

    async def _handle_llm_request(self, envelope: Envelope) -> None:
        request = LLMRequest.from_dict(envelope.payload)
        try:
            response = await asyncio.wait_for(self.bridge.generate_llm(request), timeout=45)
        except Exception as exc:
            logger.warning("[NodeSyncClient] LLM 委托失败: %s", exc, exc_info=True)
            response = LLMResponse(request_id=request.request_id, ok=False, error=str(exc))
        await self._send(Envelope(type="llm.response", request_id=envelope.request_id, payload=response.to_dict()))

    async def _handle_directive(self, envelope: Envelope) -> None:
        directive = Directive.from_dict(envelope.payload)
        reject_reason = self._directive_reject_reason(directive)
        if reject_reason:
            result = DirectiveResult(directive_id=directive.directive_id, ok=False, message=reject_reason)
            self.directive_results.record(result)
            await self._send(Envelope(type="directive.result", request_id=envelope.request_id, payload=result.to_dict()))
            return

        await self._send(
            Envelope(
                type="directive.ack",
                request_id=envelope.request_id,
                payload={"directive_id": directive.directive_id, "bot_id": self.config.bot_id},
            )
        )
        previous = self.directive_results.get(directive.directive_id)
        if previous is not None:
            await self._send(
                Envelope(type="directive.result", request_id=envelope.request_id, payload=previous.to_dict())
            )
            return

        ok = False
        message = ""
        try:
            if directive.directive_type == DirectiveType.ALIGN_CONTEXT.value:
                ok = await self._apply_alignment(directive)
                message = "context injection applied" if ok else "context injection failed"
            elif directive.directive_type == DirectiveType.ADVANCE_DIALOGUE.value:
                rendered = await self._render_advance_message(directive)
                ok = await asyncio.wait_for(self.bridge.send_message(rendered), timeout=20)
                message = "advance message sent" if ok else "advance message failed"
            elif directive.directive_type == DirectiveType.NO_OP.value:
                ok = True
                message = "no_op"
        except Exception as exc:
            logger.warning("[NodeSyncClient] 执行指令失败: %s", exc, exc_info=True)
            message = str(exc)

        result = DirectiveResult(directive_id=directive.directive_id, ok=ok, message=message)
        self.directive_results.record(result)
        await self._send(Envelope(type="directive.result", request_id=envelope.request_id, payload=result.to_dict()))

    def _directive_reject_reason(self, directive: Directive) -> str:
        if directive.target_bot_id and directive.target_bot_id != self.config.bot_id:
            return "directive target_bot_id mismatch"
        if directive.expires_at and directive.expires_at < now_ts():
            return "directive expired"
        if not self._handles_stream(directive.stream_id):
            return "directive stream not allowed"
        return ""

    def _handles_stream(self, stream_id: str) -> bool:
        return not self.config.streams or stream_id in self.config.streams

    async def _apply_alignment(self, directive: Directive) -> bool:
        # align_context 必须先落盘，再调用 MaiBot 侧的私有上下文注入。
        record = self.injections.record_pending(directive)
        try:
            ok = await asyncio.wait_for(self.bridge.apply_context_injection(directive, record), timeout=15)
        except Exception:
            logger.warning("[NodeSyncClient] 上下文注入执行异常", exc_info=True)
            ok = False
        if ok:
            self.injections.mark_applied(record.injection_id)
        else:
            self.injections.mark_status(record.injection_id, InjectionStatus.SUPERSEDED.value)
        return ok

    async def _render_advance_message(self, directive: Directive) -> Directive:
        # advance_dialogue 的 content 是服务端内部推进意图；群内最终文本由目标 bot 侧落笔。
        renderer = getattr(self.bridge, "render_advance_message", None)
        content = ""
        if renderer is not None:
            try:
                content = await asyncio.wait_for(
                    renderer(directive),
                    timeout=max(20, self.config.llm_generation_timeout_seconds),
                )
            except Exception:
                logger.warning("[NodeSyncClient] 推进消息渲染失败，使用保底群聊文本", exc_info=True)
        if content.strip():
            content = clean_advance_reply_content(content, directive.source_scene_snapshot)
        else:
            content = build_safe_advance_reply_fallback(directive.source_scene_snapshot)
        return Directive(
            directive_id=directive.directive_id,
            session_id=directive.session_id,
            stream_id=directive.stream_id,
            target_bot_id=directive.target_bot_id,
            directive_type=directive.directive_type,
            content=content.strip()[:500],
            source_scene_snapshot=directive.source_scene_snapshot,
            idempotency_key=directive.idempotency_key,
            attempts=directive.attempts,
            last_error=directive.last_error,
            priority=directive.priority,
            created_at=directive.created_at,
            expires_at=directive.expires_at,
        )

    async def _send(self, envelope: Envelope) -> bool:
        if self._ws is None or self._ws.closed:
            return False
        async with self._send_lock:
            try:
                await asyncio.wait_for(self._ws.send_str(dumps_json(envelope.to_dict())), timeout=8)
                return True
            except Exception as exc:
                logger.warning("[NodeSyncClient] 发送消息失败: %s", exc)
                return False
