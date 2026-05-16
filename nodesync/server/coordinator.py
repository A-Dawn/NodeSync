"""NodeSync 内置 HTTP + WebSocket 协调服务。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from aiohttp import WSMsgType, web

from nodesync.core.llm_decision import LLMSceneDecision, merge_llm_decision
from nodesync.core.runtime_config import NodeSyncConfig
from nodesync.core.scene import ProgressDecision, SceneAnalyzer
from nodesync.core.storage import NodeSyncStorage
from nodesync.shared.jsonio import dumps_json, loads_json
from nodesync.shared.logging_utils import get_logger
from nodesync.shared.models import (
    BotCapability,
    ChatMessageEvent,
    ClientRegistration,
    ContextRequest,
    ContextResponse,
    DecisionKind,
    Directive,
    DirectiveResult,
    DirectiveType,
    Envelope,
    LLMRequest,
    LLMResponse,
    SceneState,
)
from nodesync.shared.texts import build_advance_generation_prompt, build_scene_decision_prompt
from nodesync.shared.time_utils import now_ts

logger = get_logger("server.coordinator")

WEAK_AUTH_TOKENS = {"", "change-me", "changeme", "change-this-token", "test", "token", "password"}
KNOWN_CAPABILITIES = {item.value for item in BotCapability}


@dataclass(slots=True)
class ConnectedClient:
    """已连接客户端的运行时状态。"""

    ws: web.WebSocketResponse
    registration: ClientRegistration | None = None
    connected_at: float = 0.0
    last_seen_at: float = 0.0


@dataclass(slots=True)
class PendingClientRequest:
    """等待指定 client 响应的请求。"""

    bot_id: str
    future: asyncio.Future[Any]


class NodeSyncCoordinator:
    """负责收集上下文、识别停滞，并向 client 下发推进指令。"""

    def __init__(self, config: NodeSyncConfig, analyzer: SceneAnalyzer | None = None):
        self.config = config
        self.analyzer = analyzer or SceneAnalyzer()
        self.storage = NodeSyncStorage(config.sqlite_path)
        self._clients: dict[str, ConnectedClient] = {}
        self._pending_context: dict[str, PendingClientRequest] = {}
        self._pending_llm: dict[str, PendingClientRequest] = {}
        self._analysis_locks: dict[str, asyncio.Lock] = {}
        self._analysis_tasks: set[asyncio.Task[None]] = set()
        self._last_window_analysis_counts: dict[str, int] = {}
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._retry_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """启动内置 HTTP + WebSocket 服务。"""

        if self._runner is not None:
            return
        self._validate_security_config()
        app = web.Application(client_max_size=max(self.config.http_client_max_bytes, 1024))
        app.add_routes(
            [
                web.get("/health", self._handle_health),
                web.get("/bots", self._handle_bots),
                web.get("/ws", self._handle_ws),
                web.post("/streams/{stream_id}/analyze", self._handle_analyze_stream),
                web.post("/sessions/{session_id}/close", self._handle_close_session),
            ]
        )
        self._app = app
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.config.server_host, self.config.server_port)
        await self._site.start()
        self._retry_task = asyncio.create_task(self._retry_loop(), name="nodesync-directive-retry")
        logger.info(
            "[NodeSyncServer] 已启动: http://%s:%s",
            self.config.server_host,
            self.config.server_port,
        )

    async def stop(self) -> None:
        """停止服务并关闭所有客户端连接。"""

        if self._retry_task and not self._retry_task.done():
            self._retry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._retry_task
        self._retry_task = None
        for task in list(self._analysis_tasks):
            if not task.done():
                task.cancel()
        for task in list(self._analysis_tasks):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._analysis_tasks.clear()
        for connection in list(self._clients.values()):
            with contextlib.suppress(Exception):
                await connection.ws.close()
        self._clients.clear()
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None
        self._app = None

    async def analyze_stream(self, stream_id: str, *, force: bool = True) -> None:
        """分析指定群聊流，并按需下发指令。"""

        if not stream_id:
            return
        lock = self._analysis_locks.setdefault(stream_id, asyncio.Lock())
        async with lock:
            stored_count = self.storage.count_messages(stream_id)
            if not self._should_run_window_analysis(stream_id, stored_count, force=force):
                return

            local_messages = self.storage.get_recent_messages(stream_id, self.config.context_limit)
            scene = self.storage.get_active_scene_for_stream(stream_id)
            session_id = scene.session_id if scene else ""
            pulled_messages = await self._pull_context(stream_id=stream_id, session_id=session_id)
            messages = self._merge_messages(local_messages, pulled_messages)
            if len(messages) < self.config.analysis_min_messages:
                return
            self._mark_window_analyzed(stream_id, stored_count)

            if scene is not None:
                close_reason = self.analyzer.close_reason(scene, messages, self.config.scene_idle_close_seconds)
                if close_reason == "completed":
                    self.storage.close_scene(scene.session_id)
                    return
                if close_reason in {"new_scene", "idle_timeout"}:
                    self.storage.close_scene(scene.session_id)
                    scene = None

            scene = self.analyzer.recognize_or_update(stream_id, messages, scene)
            self.storage.upsert_scene(scene)

            align_candidates = self._candidate_bot_ids(stream_id, BotCapability.CONTEXT_INJECTION.value)
            advance_candidates = self._candidate_bot_ids(stream_id, BotCapability.SEND_MESSAGE.value)
            candidate_ids = sorted(set(align_candidates) | set(advance_candidates))
            heuristic = self.analyzer.decide(scene, messages, candidate_ids)
            decision = await self._decide_with_llm(scene, messages, heuristic, candidate_ids)
            self.storage.upsert_scene(scene)

            if decision.metadata.get("completed") == "true":
                self.storage.close_scene(scene.session_id)
                return
            if decision.kind == DecisionKind.NO_OP.value:
                return
            if now_ts() - scene.last_intervention_at < self.config.intervention_cooldown_seconds:
                logger.debug("[NodeSyncServer] stream=%s 仍在冷却期，跳过介入", stream_id)
                return

            target_bot_id = self._pick_target_for_decision(decision.kind, decision.target_bot_id, stream_id)
            if not target_bot_id:
                logger.warning("[NodeSyncServer] 没有可执行 %s 的客户端", decision.kind)
                return
            if decision.kind == DecisionKind.ADVANCE_DIALOGUE.value:
                decision.content = await self._build_advance_content(scene, messages, decision.content)

            idempotency_key = self._directive_key(scene, decision, target_bot_id)
            existing = self.storage.find_open_directive_by_key(idempotency_key)
            if existing is not None:
                logger.debug("[NodeSyncServer] 已存在同幂等键指令，跳过重复创建: %s", existing.directive_id)
                return
            directive = Directive(
                directive_id=uuid4().hex,
                session_id=scene.session_id,
                stream_id=stream_id,
                target_bot_id=target_bot_id,
                directive_type=decision.kind,
                content=decision.content,
                source_scene_snapshot=scene.to_dict(),
                idempotency_key=idempotency_key,
                created_at=now_ts(),
                expires_at=now_ts() + self.config.directive_ttl_seconds,
            )
            self.storage.add_directive(directive)
            scene.last_intervention_at = now_ts()
            self.storage.upsert_scene(scene)
            await self._push_directive(directive)

    async def request_llm(
        self,
        prompt: str,
        model_name: str = "",
        request_timeout: float = 45,
        temperature: float = 0.2,
        max_tokens: int = 1200,
    ) -> LLMResponse:
        """向具备 LLM 能力的客户端发起一次委托生成。"""

        target = self.config.llm_worker_bot_id or self._first_client_with_capability(BotCapability.LLM_GENERATE.value)
        if not target:
            return LLMResponse(request_id=uuid4().hex, ok=False, error="no llm client available")
        connection = self._clients.get(target)
        if connection is None:
            return LLMResponse(request_id=uuid4().hex, ok=False, error="llm client offline")

        request = LLMRequest(
            request_id=uuid4().hex,
            prompt=prompt,
            model_name=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        envelope = Envelope(type="llm.request", payload=request.to_dict())
        future: asyncio.Future[LLMResponse] = asyncio.get_running_loop().create_future()
        self._pending_llm[envelope.request_id] = PendingClientRequest(bot_id=target, future=future)
        await self._send(connection.ws, envelope)
        try:
            return await asyncio.wait_for(future, timeout=request_timeout)
        except TimeoutError:
            return LLMResponse(request_id=request.request_id, ok=False, error="llm request timeout")
        finally:
            self._pending_llm.pop(envelope.request_id, None)

    def _should_run_window_analysis(self, stream_id: str, stored_message_count: int, *, force: bool) -> bool:
        """判断是否到达自动 LLM 分析窗口。"""

        if force:
            return True
        if stored_message_count < self.config.analysis_min_messages:
            return False
        window = max(int(self.config.analysis_window_messages), 1)
        previous = self._last_window_analysis_counts.get(stream_id, 0)
        return stored_message_count - previous >= window

    def _mark_window_analyzed(self, stream_id: str, stored_message_count: int) -> None:
        """记录当前 stream 最近一次实际分析的消息位置。"""

        self._last_window_analysis_counts[stream_id] = max(stored_message_count, 0)

    async def _decide_with_llm(
        self,
        scene: SceneState,
        messages: list[ChatMessageEvent],
        heuristic: ProgressDecision,
        candidate_bot_ids: list[str],
    ) -> ProgressDecision:
        """使用 LLM 对当前窗口做主判定，规则结果只作为兜底参考。"""

        if not self.config.enable_llm_decision:
            return heuristic
        if not self._first_client_with_capability(BotCapability.LLM_GENERATE.value):
            logger.warning("[NodeSyncServer] LLM 判定已启用，但没有在线 LLM client，回退规则判断")
            return heuristic
        prompt = build_scene_decision_prompt(scene, messages, heuristic.kind, candidate_bot_ids)
        response = await self.request_llm(
            prompt,
            request_timeout=self.config.llm_decision_timeout_seconds,
            temperature=0.1,
            max_tokens=700,
        )
        llm_decision = LLMSceneDecision.from_response(response)
        return merge_llm_decision(scene, heuristic, llm_decision, candidate_bot_ids)

    async def _build_advance_content(
        self,
        scene: SceneState,
        messages: list[ChatMessageEvent],
        instruction: str,
    ) -> str:
        """用 LLM 生成群内推进消息，失败时回退到规则模板。"""

        fallback = instruction
        if not self.config.enable_llm_advance_generation:
            return fallback
        if not self._first_client_with_capability(BotCapability.LLM_GENERATE.value):
            return fallback
        prompt = build_advance_generation_prompt(scene, messages, instruction)
        response = await self.request_llm(
            prompt,
            request_timeout=self.config.llm_generation_timeout_seconds,
            temperature=0.4,
            max_tokens=220,
        )
        if not response.ok:
            return fallback
        cleaned = self._clean_group_message(response.content)
        return cleaned or fallback

    async def _retry_loop(self) -> None:
        """后台重试未完成指令。"""

        while True:
            await asyncio.sleep(max(self.config.directive_retry_interval_seconds, 1))
            await self._retry_pending_directives()

    async def _retry_pending_directives(self) -> None:
        retryable = self.storage.list_retryable_directives(
            retry_statuses={"queued", "offline", "send_failed", "pushed", "acked", "failed"},
            retry_after_seconds=self.config.directive_retry_interval_seconds,
            max_attempts=self.config.directive_max_attempts,
        )
        for directive in retryable:
            await self._push_directive(directive)

    async def _handle_health(self, request: web.Request) -> web.Response:
        payload: dict[str, Any] = {"ok": True, "time": now_ts()}
        if self._auth_ok(request):
            payload.update({"mode": self.config.mode, "clients": len(self._clients)})
        return web.json_response(payload)

    async def _handle_bots(self, request: web.Request) -> web.Response:
        if not self._auth_ok(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        stored = [item.to_dict() for item in self.storage.list_clients()]
        live = sorted(self._clients.keys())
        return web.json_response({"stored": stored, "live": live})

    async def _handle_analyze_stream(self, request: web.Request) -> web.Response:
        if not self._auth_ok(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        stream_id = request.match_info["stream_id"]
        await self.analyze_stream(stream_id)
        return web.json_response({"ok": True, "stream_id": stream_id})

    async def _handle_close_session(self, request: web.Request) -> web.Response:
        if not self._auth_ok(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        session_id = request.match_info["session_id"]
        self.storage.close_scene(session_id)
        return web.json_response({"ok": True, "session_id": session_id})

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        if not self._auth_ok(request):
            raise web.HTTPUnauthorized(text="unauthorized")

        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=max(self.config.ws_max_msg_bytes, 1024))
        await ws.prepare(request)
        connection = ConnectedClient(ws=ws, connected_at=now_ts(), last_seen_at=now_ts())
        logger.info("[NodeSyncServer] WebSocket 客户端已连接")

        async for message in ws:
            if message.type == WSMsgType.TEXT:
                await self._handle_ws_text(connection, message.data)
            elif message.type in {WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED}:
                break

        self._remove_connection(connection)
        logger.info("[NodeSyncServer] WebSocket 客户端已断开")
        return ws

    async def _handle_ws_text(self, connection: ConnectedClient, raw: str) -> None:
        try:
            envelope = Envelope.from_dict(loads_json(raw))
        except Exception as exc:
            logger.warning("[NodeSyncServer] 忽略无法解析的客户端消息: %s", exc)
            await self._send(connection.ws, Envelope(type="server.error", payload={"error": str(exc)}))
            return

        try:
            await self._handle_envelope(connection, envelope)
        except Exception as exc:
            logger.warning("[NodeSyncServer] 处理消息失败: %s", exc, exc_info=True)
            await self._send(
                connection.ws,
                Envelope(type="server.error", request_id=envelope.request_id, payload={"error": str(exc)}),
            )

    async def _handle_envelope(self, connection: ConnectedClient, envelope: Envelope) -> None:
        connection.last_seen_at = now_ts()
        if envelope.type == "client.register":
            registration = self._normalize_registration(ClientRegistration.from_dict(envelope.payload))
            existing = self._clients.get(registration.bot_id)
            if existing is not None and existing is not connection:
                with contextlib.suppress(Exception):
                    await existing.ws.close(code=1008, message=b"duplicate bot_id")
            connection.registration = registration
            self._clients[registration.bot_id] = connection
            self.storage.upsert_client(registration)
            await self._send(
                connection.ws,
                Envelope(type="server.ack", request_id=envelope.request_id, payload={"registered": registration.bot_id}),
            )
            return

        if connection.registration is None:
            await self._send(
                connection.ws,
                Envelope(
                    type="server.error",
                    request_id=envelope.request_id,
                    payload={"error": "client must register before sending messages"},
                ),
            )
            return

        registration = connection.registration
        if envelope.type == "client.heartbeat":
            self.storage.upsert_client(registration)
            await self._send(connection.ws, Envelope(type="server.ack", request_id=envelope.request_id))
        elif envelope.type == "chat.event":
            event = ChatMessageEvent.from_dict(envelope.payload)
            if not event.stream_id or not self._connection_handles_stream(connection, event.stream_id):
                await self._send(
                    connection.ws,
                    Envelope(type="server.error", request_id=envelope.request_id, payload={"error": "stream not allowed"}),
                )
                return
            if not event.message_id:
                event.message_id = uuid4().hex
            self.storage.add_message(event)
            await self._send(connection.ws, Envelope(type="server.ack", request_id=envelope.request_id))
            task = asyncio.create_task(
                self.analyze_stream(event.stream_id, force=False),
                name=f"nodesync-analyze-{event.stream_id}",
            )
            self._analysis_tasks.add(task)
            task.add_done_callback(self._analysis_tasks.discard)
        elif envelope.type == "context.response":
            response = ContextResponse.from_dict(envelope.payload)
            pending = self._pending_context.get(envelope.request_id)
            if pending is not None and pending.bot_id == registration.bot_id and not pending.future.done():
                self._pending_context.pop(envelope.request_id, None)
                pending.future.set_result(response)
        elif envelope.type == "llm.response":
            response = LLMResponse.from_dict(envelope.payload)
            pending = self._pending_llm.get(envelope.request_id)
            if pending is not None and pending.bot_id == registration.bot_id and not pending.future.done():
                self._pending_llm.pop(envelope.request_id, None)
                pending.future.set_result(response)
        elif envelope.type == "directive.ack":
            directive_id = str(envelope.payload.get("directive_id", ""))
            directive = self.storage.get_directive(directive_id) if directive_id else None
            if directive and directive.target_bot_id == registration.bot_id:
                self.storage.update_directive_status(directive_id, "acked", dict(envelope.payload))
        elif envelope.type == "directive.result":
            result = DirectiveResult.from_dict(envelope.payload)
            directive = self.storage.get_directive(result.directive_id)
            if directive and directive.target_bot_id == registration.bot_id:
                status = "applied" if result.ok else "failed"
                self.storage.update_directive_status(result.directive_id, status, result.to_dict())
        else:
            logger.debug("[NodeSyncServer] 忽略未知消息类型: %s", envelope.type)

    async def _pull_context(self, stream_id: str, session_id: str) -> list[ChatMessageEvent]:
        targets = [
            connection
            for connection in self._clients.values()
            if self._connection_has_capability(connection, BotCapability.CONTEXT_FETCH.value)
            and self._connection_handles_stream(connection, stream_id)
        ]
        if not targets:
            return []

        async def ask(connection: ConnectedClient) -> ContextResponse | None:
            request = ContextRequest(
                session_id=session_id,
                stream_id=stream_id,
                limit=self.config.context_limit,
                include_injection_history=True,
                injection_history_limit=self.config.injection_history_limit,
            )
            envelope = Envelope(type="context.request", payload=request.to_dict())
            future: asyncio.Future[ContextResponse] = asyncio.get_running_loop().create_future()
            bot_id = connection.registration.bot_id if connection.registration else ""
            self._pending_context[envelope.request_id] = PendingClientRequest(bot_id=bot_id, future=future)
            await self._send(connection.ws, envelope)
            try:
                return await asyncio.wait_for(future, timeout=8)
            except TimeoutError:
                logger.warning("[NodeSyncServer] context.request 超时: stream=%s", stream_id)
                return None
            finally:
                self._pending_context.pop(envelope.request_id, None)

        responses = await asyncio.gather(*(ask(connection) for connection in targets), return_exceptions=True)
        messages: list[ChatMessageEvent] = []
        for response in responses:
            if isinstance(response, ContextResponse):
                messages.extend(self._events_from_context(response))
        return messages

    def _events_from_context(self, response: ContextResponse) -> list[ChatMessageEvent]:
        events = []
        for raw in response.messages:
            try:
                event = ChatMessageEvent.from_dict(raw)
            except Exception:
                continue
            if event.stream_id:
                events.append(event)
        return events

    def _merge_messages(
        self,
        first: list[ChatMessageEvent],
        second: list[ChatMessageEvent],
    ) -> list[ChatMessageEvent]:
        by_id: dict[str, ChatMessageEvent] = {}
        for event in first + second:
            key = event.message_id or f"{event.sender_id}:{event.timestamp}:{event.plain_text[:20]}"
            by_id[key] = event
        merged = sorted(by_id.values(), key=lambda item: item.timestamp)
        return merged[-self.config.context_limit :]

    async def _push_directive(self, directive: Directive) -> None:
        updated = self.storage.increment_directive_attempt(directive.directive_id)
        if updated is not None:
            directive = updated
        connection = self._clients.get(directive.target_bot_id)
        if connection is None:
            self.storage.update_directive_status(directive.directive_id, "offline")
            return
        sent = await self._send(connection.ws, Envelope(type="directive.push", payload=directive.to_dict()))
        self.storage.update_directive_status(directive.directive_id, "pushed" if sent else "send_failed")

    async def _send(self, ws: web.WebSocketResponse, envelope: Envelope) -> bool:
        try:
            await asyncio.wait_for(ws.send_str(dumps_json(envelope.to_dict())), timeout=8)
            return True
        except Exception as exc:
            logger.warning("[NodeSyncServer] 发送 WebSocket 消息失败: %s", exc)
            return False

    def _candidate_bot_ids(self, stream_id: str, capability: str) -> list[str]:
        result = []
        for bot_id, connection in self._clients.items():
            if not self._connection_has_capability(connection, capability):
                continue
            if not self._connection_handles_stream(connection, stream_id):
                continue
            result.append(bot_id)
        return result

    def _pick_target_for_decision(self, kind: str, preferred: str, stream_id: str) -> str:
        capability = (
            BotCapability.SEND_MESSAGE.value
            if kind == DirectiveType.ADVANCE_DIALOGUE.value
            else BotCapability.CONTEXT_INJECTION.value
        )
        candidates = self._candidate_bot_ids(stream_id, capability)
        if preferred in candidates:
            return preferred
        return candidates[0] if candidates else ""

    def _first_client_with_capability(self, capability: str) -> str:
        for bot_id, connection in self._clients.items():
            if self._connection_has_capability(connection, capability):
                return bot_id
        return ""

    def _connection_has_capability(self, connection: ConnectedClient, capability: str) -> bool:
        registration = connection.registration
        return registration is not None and capability in registration.capabilities

    def _connection_handles_stream(self, connection: ConnectedClient, stream_id: str) -> bool:
        registration = connection.registration
        if registration is None:
            return False
        return not registration.streams or stream_id in registration.streams

    def _directive_key(self, scene: SceneState, decision: ProgressDecision, target_bot_id: str) -> str:
        raw = "|".join(
            [
                scene.session_id,
                decision.kind,
                target_bot_id,
                scene.current_loop,
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _clean_group_message(self, content: str) -> str:
        text = content.strip()
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.lower().startswith("text"):
                text = text[4:].strip()
        for prefix in ("最终消息：", "消息：", "发言："):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
        return text[:500]

    def _remove_connection(self, connection: ConnectedClient) -> None:
        stale = [bot_id for bot_id, item in self._clients.items() if item is connection]
        for bot_id in stale:
            self._clients.pop(bot_id, None)

    def _normalize_registration(self, registration: ClientRegistration) -> ClientRegistration:
        bot_id = registration.bot_id.strip()
        if not bot_id:
            raise ValueError("client.register 缺少 bot_id")
        capabilities = [item for item in registration.capabilities if item in KNOWN_CAPABILITIES]
        return ClientRegistration(
            bot_id=bot_id,
            bot_name=registration.bot_name.strip(),
            maibot_version=registration.maibot_version.strip(),
            adapter_version=registration.adapter_version.strip(),
            capabilities=capabilities,
            streams=registration.streams,
        )

    def _auth_ok(self, request: web.Request) -> bool:
        token = self.config.auth_token
        if not token:
            return True
        auth_header = request.headers.get("Authorization", "")
        prefix = "Bearer "
        if auth_header.startswith(prefix) and hmac.compare_digest(auth_header[len(prefix) :], token):
            return True
        if self.config.allow_query_token_auth:
            return hmac.compare_digest(request.query.get("token", ""), token)
        return False

    def _validate_security_config(self) -> None:
        token = self.config.auth_token.strip()
        if _is_exposed_bind_host(self.config.server_host) and token in WEAK_AUTH_TOKENS:
            raise RuntimeError(
                "NodeSync server_host 暴露到非本机地址时必须配置强 auth_token，"
                "不要使用空值、change-me 或示例 token。"
            )
        if token in WEAK_AUTH_TOKENS:
            logger.warning("[NodeSyncServer] auth_token 使用默认或弱值，仅建议本机开发使用")


def _is_exposed_bind_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized in {"", "0.0.0.0", "::", "[::]"}:
        return True
    if normalized == "localhost":
        return False
    try:
        return not ipaddress.ip_address(normalized.strip("[]")).is_loopback
    except ValueError:
        return True


def envelope_from_json(raw: str) -> Envelope:
    """测试辅助：把 JSON 字符串转成 Envelope。"""

    return Envelope.from_dict(loads_json(raw))
