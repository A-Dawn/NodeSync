"""本地构造消息流调试入口。

该模块只服务于本地联调：真实 MaiBot 进程加载真实 NodeSync adapter，
外部脚本通过本机 HTTP 把构造出来的群聊消息交给 adapter。
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from aiohttp import web

from nodesync.shared.logging_utils import get_logger
from nodesync.shared.models import ChatMessageEvent, Directive
from nodesync.shared.time_utils import now_ts

logger = get_logger("adapters.local_flow")

WEAK_LOCAL_FLOW_TOKENS = {"", "change-me", "changeme", "change-this-token", "test", "token", "password"}


class MessageReporter(Protocol):
    """NodeSyncRuntimeManager 暴露的消息上报能力。"""

    async def report_message(self, event: ChatMessageEvent) -> bool:
        """上报一条构造消息。"""


@dataclass(frozen=True, slots=True)
class LocalFlowConfig:
    """本地构造消息流入口配置。"""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8788
    auth_token: str = ""
    stream_id: str = "local-flow"
    capture_outbound: bool = True
    context_limit: int = 80
    max_messages: int = 500

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None, default_auth_token: str = "") -> LocalFlowConfig:
        """从插件配置段构造本地消息流配置。"""

        data = dict(raw or {})
        token = str(data.get("auth_token", "")).strip() or default_auth_token
        return cls(
            enabled=_parse_bool(data.get("enabled", False)),
            host=str(data.get("host", "127.0.0.1")),
            port=int(data.get("port", 8788)),
            auth_token=token,
            stream_id=str(data.get("stream_id", "local-flow")),
            capture_outbound=_parse_bool(data.get("capture_outbound", True)),
            context_limit=int(data.get("context_limit", 80)),
            max_messages=max(int(data.get("max_messages", 500)), 1),
        )


class LocalFlowHub:
    """维护构造消息流的本地上下文，并可把消息重新上报给 NodeSync。"""

    def __init__(self, config: LocalFlowConfig, bot_id: str, bot_name: str) -> None:
        self.config = config
        self.bot_id = bot_id
        self.bot_name = bot_name
        self._messages: deque[ChatMessageEvent] = deque(maxlen=config.max_messages)
        self._runtime: MessageReporter | None = None
        self._lock = asyncio.Lock()

    def bind_runtime(self, runtime: MessageReporter) -> None:
        """绑定真实 NodeSync 运行时。"""

        self._runtime = runtime

    async def ingest(self, event: ChatMessageEvent) -> bool:
        """接收一条构造消息，先写入本地上下文，再交给真实 client 上报。"""

        await self.remember(event)
        if self._runtime is None:
            return False
        return await self._runtime.report_message(event)

    async def remember(self, event: ChatMessageEvent) -> None:
        """只写入本地上下文，不上报 NodeSync。"""

        async with self._lock:
            self._messages.append(event)

    async def capture_outbound(self, directive: Directive) -> bool:
        """把推进消息作为本地 bot 发言捕获，并重新进入 NodeSync 消息流。"""

        event = ChatMessageEvent(
            stream_id=directive.stream_id or self.config.stream_id,
            message_id=f"local-flow-out-{directive.directive_id or uuid4().hex}",
            sender_id=self.bot_id,
            sender_name=self.bot_name,
            plain_text=directive.content,
            timestamp=now_ts(),
            is_bot=True,
        )
        return await self.ingest(event)

    def get_recent(self, stream_id: str, limit: int | None = None) -> list[ChatMessageEvent]:
        """读取指定 stream 最近的构造消息。"""

        target_stream = stream_id or self.config.stream_id
        max_count = limit if limit is not None else self.config.context_limit
        messages = [item for item in self._messages if item.stream_id == target_stream]
        return messages[-max(max_count, 0) :]


class LocalFlowInputServer:
    """仅用于本地联调的 HTTP 构造消息入口。"""

    def __init__(self, config: LocalFlowConfig, hub: LocalFlowHub) -> None:
        self.config = config
        self.hub = hub
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def start(self) -> None:
        """启动本机 HTTP 输入口。"""

        if not self.config.enabled or self._runner is not None:
            return
        self._validate_security_config()
        app = web.Application(client_max_size=256 * 1024)
        app.add_routes(
            [
                web.get("/health", self._handle_health),
                web.post("/messages", self._handle_post_message),
                web.post("/batch", self._handle_post_batch),
                web.get("/messages/{stream_id}", self._handle_get_messages),
            ]
        )
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.config.host, self.config.port)
        await self._site.start()
        logger.info("[NodeSyncLocalFlow] 已启动: http://%s:%s", self.config.host, self.config.port)

    async def stop(self) -> None:
        """停止本机 HTTP 输入口。"""

        if self._runner is not None:
            with contextlib.suppress(Exception):
                await self._runner.cleanup()
        self._runner = None
        self._site = None

    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "time": now_ts()})

    async def _handle_post_message(self, request: web.Request) -> web.Response:
        if not self._auth_ok(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        payload = await self._load_payload(request)
        event = self._event_from_payload(payload)
        should_report = _parse_bool(payload.get("report", True))
        if should_report:
            reported = await self.hub.ingest(event)
        else:
            await self.hub.remember(event)
            reported = False
        return web.json_response({"ok": True, "reported": reported, "event": event.to_dict()})

    async def _handle_post_batch(self, request: web.Request) -> web.Response:
        if not self._auth_ok(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        payload = await self._load_payload(request)
        raw_messages = payload.get("messages", [])
        if not isinstance(raw_messages, list):
            return web.json_response({"error": "messages 必须是数组"}, status=400)

        events = []
        for raw in raw_messages[:50]:
            if not isinstance(raw, dict):
                continue
            event = self._event_from_payload(raw)
            should_report = _parse_bool(raw.get("report", True))
            if should_report:
                reported = await self.hub.ingest(event)
            else:
                await self.hub.remember(event)
                reported = False
            events.append({"reported": reported, "event": event.to_dict()})
        return web.json_response({"ok": True, "count": len(events), "events": events})

    async def _handle_get_messages(self, request: web.Request) -> web.Response:
        if not self._auth_ok(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        stream_id = request.match_info["stream_id"]
        try:
            limit = int(request.query.get("limit", str(self.config.context_limit)))
        except ValueError:
            limit = self.config.context_limit
        messages = [item.to_dict() for item in self.hub.get_recent(stream_id, limit)]
        return web.json_response({"ok": True, "stream_id": stream_id, "messages": messages})

    async def _load_payload(self, request: web.Request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(text="请求体必须是 JSON 对象") from exc
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="请求体必须是 JSON 对象")
        return dict(payload)

    def _event_from_payload(self, payload: dict[str, Any]) -> ChatMessageEvent:
        text = str(payload.get("plain_text") or payload.get("text") or payload.get("content") or "").strip()
        if not text:
            raise web.HTTPBadRequest(text="plain_text 不能为空")
        timestamp = payload.get("timestamp")
        try:
            created_at = float(timestamp if timestamp is not None else now_ts())
        except (TypeError, ValueError):
            created_at = now_ts()
        return ChatMessageEvent(
            stream_id=str(payload.get("stream_id") or self.config.stream_id),
            message_id=str(payload.get("message_id") or uuid4().hex),
            sender_id=str(payload.get("sender_id") or "local-user"),
            sender_name=str(payload.get("sender_name") or payload.get("name") or "本地用户"),
            plain_text=text,
            timestamp=created_at,
            is_bot=_parse_bool(payload.get("is_bot", False)),
        )

    def _auth_ok(self, request: web.Request) -> bool:
        token = self.config.auth_token
        if not token:
            return True
        auth_header = request.headers.get("Authorization", "")
        prefix = "Bearer "
        return auth_header.startswith(prefix) and hmac.compare_digest(auth_header[len(prefix) :], token)

    def _validate_security_config(self) -> None:
        token = self.config.auth_token.strip()
        if _is_exposed_bind_host(self.config.host) and token in WEAK_LOCAL_FLOW_TOKENS:
            raise RuntimeError(
                "NodeSync local_flow 暴露到非本机地址时必须配置强 auth_token，"
                "不要使用空值、change-me 或示例 token。"
            )
        if token in WEAK_LOCAL_FLOW_TOKENS:
            logger.warning("[NodeSyncLocalFlow] auth_token 使用默认或弱值，仅建议本机临时联调用")


def merge_chat_events(
    primary_events: list[ChatMessageEvent],
    local_events: list[ChatMessageEvent],
    limit: int,
) -> list[dict[str, Any]]:
    """合并真实宿主上下文和本地构造上下文，按 message_id 去重。"""

    by_id: dict[str, ChatMessageEvent] = {}
    for event in [*primary_events, *local_events]:
        key = event.message_id or f"{event.sender_id}:{event.timestamp}:{event.plain_text[:20]}"
        by_id[key] = event
    merged = sorted(by_id.values(), key=lambda item: item.timestamp)
    return [item.to_dict() for item in merged[-max(limit, 0) :]]


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "启用", "是"}
    return bool(value)


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
