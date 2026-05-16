from __future__ import annotations

import asyncio
import socket
import tempfile
import unittest
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout

from nodesync.client.runtime import NodeSyncClientRuntime
from nodesync.core.injection_store import InjectionStore
from nodesync.core.runtime_config import NodeSyncConfig
from nodesync.server.coordinator import ConnectedClient, NodeSyncCoordinator, PendingClientRequest
from nodesync.shared.jsonio import iter_jsonl
from nodesync.shared.models import (
    ChatMessageEvent,
    ClientRegistration,
    ContextRequest,
    ContextResponse,
    Directive,
    DirectiveType,
    Envelope,
    InjectionStatus,
    LLMRequest,
    LLMResponse,
)
from nodesync.shared.time_utils import now_ts


class RuntimeIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_auto_analysis_waits_for_configured_message_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            config.analysis_min_messages = 2
            config.analysis_window_messages = 3
            server = NodeSyncCoordinator(config)

            self.assertFalse(server._should_run_window_analysis("group-1", 1, force=False))  # noqa: SLF001
            self.assertFalse(server._should_run_window_analysis("group-1", 2, force=False))  # noqa: SLF001
            self.assertTrue(server._should_run_window_analysis("group-1", 3, force=False))  # noqa: SLF001

            server._mark_window_analyzed("group-1", 3)  # noqa: SLF001

            self.assertFalse(server._should_run_window_analysis("group-1", 5, force=False))  # noqa: SLF001
            self.assertTrue(server._should_run_window_analysis("group-1", 6, force=False))  # noqa: SLF001
            self.assertTrue(server._should_run_window_analysis("group-1", 4, force=True))  # noqa: SLF001

    async def test_http_auth_and_bots_after_client_register(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            server = NodeSyncCoordinator(config)
            bridge = _BridgeStub(config.injection_path, _stagnant_messages())
            client = NodeSyncClientRuntime(config, bridge)
            await server.start()
            try:
                await client.start()
                await _wait_until(lambda: _server_has_live_client(server, config.bot_id))

                async with ClientSession(timeout=ClientTimeout(total=5)) as session:
                    async with session.get(f"{config.server_url}/health") as response:
                        self.assertEqual(response.status, 200)
                        health = await response.json()
                    async with session.get(f"{config.server_url}/bots") as response:
                        self.assertEqual(response.status, 401)
                    async with session.get(
                        f"{config.server_url}/bots",
                        headers={"Authorization": f"Bearer {config.auth_token}"},
                    ) as response:
                        self.assertEqual(response.status, 200)
                        bots = await response.json()
                    async with session.post(f"{config.server_url}/streams/group-1/analyze") as response:
                        self.assertEqual(response.status, 401)
                    async with session.post(f"{config.server_url}/streams/group-1/analyze?token={config.auth_token}") as response:
                        self.assertEqual(response.status, 401)

                self.assertTrue(health["ok"])
                self.assertNotIn("clients", health)
                self.assertIn(config.bot_id, bots["live"])
                self.assertEqual(bots["stored"][0]["bot_id"], config.bot_id)
            finally:
                await client.stop()
                await server.stop()

    async def test_analyze_requests_context_and_pushes_alignment_directive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            server = NodeSyncCoordinator(config)
            bridge = _BridgeStub(config.injection_path, _stagnant_messages())
            client = NodeSyncClientRuntime(config, bridge)
            await server.start()
            try:
                await client.start()
                await _wait_until(lambda: _server_has_live_client(server, config.bot_id))

                async with (
                    ClientSession(timeout=ClientTimeout(total=10)) as session,
                    session.post(
                        f"{config.server_url}/streams/group-1/analyze",
                        headers={"Authorization": f"Bearer {config.auth_token}"},
                    ) as response,
                ):
                    self.assertEqual(response.status, 200)
                    payload = await response.json()

                await _wait_until(lambda: bridge.applied_records and _server_has_status(server, "applied"))

                active = InjectionStore(config.injection_path).get_active(stream_id="group-1")
                self.assertEqual(payload["stream_id"], "group-1")
                self.assertEqual(bridge.context_requests[0].stream_id, "group-1")
                self.assertTrue(bridge.context_requests[0].include_injection_history)
                self.assertTrue(bridge.pending_seen_before_apply)
                self.assertIsNotNone(active)
                self.assertEqual(active.status, InjectionStatus.APPLIED.value)
                self.assertEqual(_directive_statuses(server), ["applied"])
            finally:
                await client.stop()
                await server.stop()

    async def test_context_request_response_includes_injection_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            bridge = _BridgeStub(config.injection_path, _stagnant_messages())
            runtime = NodeSyncClientRuntime(config, bridge)
            old_directive = _directive("old-injection", DirectiveType.ALIGN_CONTEXT.value)
            runtime.injections.record_pending(old_directive)
            runtime.injections.mark_applied(old_directive.directive_id)
            sent: list[Envelope] = []

            async def fake_send(envelope: Envelope) -> bool:
                sent.append(envelope)
                return True

            runtime._send = fake_send  # type: ignore[method-assign]
            request = ContextRequest(
                session_id="scene-1",
                stream_id="group-1",
                limit=8,
                include_injection_history=True,
                injection_history_limit=3,
            )

            await runtime._handle_context_request(
                Envelope(type="context.request", request_id="req-ctx", payload=request.to_dict())
            )

            response = ContextResponse.from_dict(sent[0].payload)
            self.assertEqual(sent[0].type, "context.response")
            self.assertEqual(sent[0].request_id, "req-ctx")
            self.assertEqual(len(response.messages), len(_stagnant_messages()))
            self.assertEqual(response.injection_history[0]["injection_id"], "old-injection")
            self.assertEqual(response.injection_history[0]["status"], InjectionStatus.APPLIED.value)

    async def test_duplicate_directive_push_reuses_cached_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            bridge = _BridgeStub(config.injection_path, _stagnant_messages())
            runtime = NodeSyncClientRuntime(config, bridge)
            directive = _directive("advance-1", DirectiveType.ADVANCE_DIALOGUE.value)
            sent: list[Envelope] = []

            async def fake_send(envelope: Envelope) -> bool:
                sent.append(envelope)
                return True

            runtime._send = fake_send  # type: ignore[method-assign]
            envelope = Envelope(type="directive.push", request_id="req-dir", payload=directive.to_dict())

            await runtime._handle_directive(envelope)
            await runtime._handle_directive(envelope)

            result_envelopes = [item for item in sent if item.type == "directive.result"]
            self.assertEqual(bridge.sent_messages, ["我先把这个点落到一个小问题上：我们下一步最该确认哪一个条件？"])
            self.assertEqual(len(result_envelopes), 2)
            self.assertTrue(all(item.payload["ok"] for item in result_envelopes))

    async def test_advance_dialogue_renders_intent_before_sending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            bridge = _RenderingBridgeStub(config.injection_path, _stagnant_messages())
            runtime = NodeSyncClientRuntime(config, bridge)
            directive = _directive("advance-render-1", DirectiveType.ADVANCE_DIALOGUE.value)
            directive.content = "内部推进意图：让角色触碰最近的机关。"
            sent: list[Envelope] = []

            async def fake_send(envelope: Envelope) -> bool:
                sent.append(envelope)
                return True

            runtime._send = fake_send  # type: ignore[method-assign]

            await runtime._handle_directive(
                Envelope(type="directive.push", request_id="req-dir", payload=directive.to_dict())
            )

            self.assertEqual(bridge.rendered_intents, [directive.content])
            self.assertEqual(bridge.sent_messages, ["我伸手碰了碰最近的符文，看它有没有反应。"])
            result = [item for item in sent if item.type == "directive.result"][-1]
            self.assertTrue(result.payload["ok"])

    async def test_client_rejects_directive_for_other_bot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            bridge = _BridgeStub(config.injection_path, _stagnant_messages())
            runtime = NodeSyncClientRuntime(config, bridge)
            directive = Directive(
                directive_id="wrong-target",
                session_id="scene-1",
                stream_id="group-1",
                target_bot_id="other-bot",
                directive_type=DirectiveType.ADVANCE_DIALOGUE.value,
                content="这条指令不应执行。",
                source_scene_snapshot={},
                created_at=now_ts(),
                expires_at=now_ts() + 300,
            )
            sent: list[Envelope] = []

            async def fake_send(envelope: Envelope) -> bool:
                sent.append(envelope)
                return True

            runtime._send = fake_send  # type: ignore[method-assign]

            await runtime._handle_directive(Envelope(type="directive.push", request_id="req-dir", payload=directive.to_dict()))

            self.assertEqual(bridge.sent_messages, [])
            self.assertEqual(sent[0].type, "directive.result")
            self.assertFalse(sent[0].payload["ok"])
            self.assertEqual(sent[0].payload["message"], "directive target_bot_id mismatch")

    async def test_pending_response_from_wrong_bot_does_not_consume_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            server = NodeSyncCoordinator(config)
            future: asyncio.Future[ContextResponse] = asyncio.get_running_loop().create_future()
            server._pending_context["req-ctx"] = PendingClientRequest(bot_id=config.bot_id, future=future)
            payload = ContextResponse(session_id="scene-1", stream_id="group-1", messages=[]).to_dict()

            await server._handle_envelope(
                _connected_client("other-bot"),
                Envelope(type="context.response", request_id="req-ctx", payload=payload),
            )

            self.assertIn("req-ctx", server._pending_context)
            self.assertFalse(future.done())

            await server._handle_envelope(
                _connected_client(config.bot_id),
                Envelope(type="context.response", request_id="req-ctx", payload=payload),
            )

            self.assertNotIn("req-ctx", server._pending_context)
            self.assertTrue(future.done())

    async def test_pending_llm_response_from_wrong_bot_does_not_consume_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(Path(tmp))
            server = NodeSyncCoordinator(config)
            future: asyncio.Future[LLMResponse] = asyncio.get_running_loop().create_future()
            server._pending_llm["req-llm"] = PendingClientRequest(bot_id=config.bot_id, future=future)
            payload = LLMResponse(request_id="req-llm", ok=True, content="ok").to_dict()

            await server._handle_envelope(
                _connected_client("other-bot"),
                Envelope(type="llm.response", request_id="req-llm", payload=payload),
            )

            self.assertIn("req-llm", server._pending_llm)
            self.assertFalse(future.done())

            await server._handle_envelope(
                _connected_client(config.bot_id),
                Envelope(type="llm.response", request_id="req-llm", payload=payload),
            )

            self.assertNotIn("req-llm", server._pending_llm)
            self.assertTrue(future.done())


class _BridgeStub:
    def __init__(self, injection_path: Path, context_messages: list[ChatMessageEvent]) -> None:
        self.injection_path = injection_path
        self.context_messages = context_messages
        self.context_requests: list[ContextRequest] = []
        self.applied_records: list[str] = []
        self.sent_messages: list[str] = []
        self.llm_requests: list[str] = []
        self.pending_seen_before_apply = False

    def get_streams(self) -> list[str]:
        return ["group-1"]

    async def fetch_context(self, request: ContextRequest) -> ContextResponse:
        self.context_requests.append(request)
        return ContextResponse(
            session_id=request.session_id,
            stream_id=request.stream_id,
            messages=[item.to_dict() for item in self.context_messages],
        )

    async def generate_llm(self, request: LLMRequest) -> LLMResponse:
        self.llm_requests.append(request.prompt)
        return LLMResponse(request_id=request.request_id, ok=False, error="测试桩未启用 LLM")

    async def apply_context_injection(self, directive: Directive, _record: Any) -> bool:
        # 验证 client runtime 在调用 MaiBot 侧注入前，已经把 pending 记录写入 JSONL。
        for raw in iter_jsonl(self.injection_path):
            if raw.get("injection_id") == directive.directive_id and raw.get("status") == InjectionStatus.PENDING.value:
                self.pending_seen_before_apply = True
        self.applied_records.append(directive.directive_id)
        return True

    async def send_message(self, directive: Directive) -> bool:
        self.sent_messages.append(directive.content)
        return True


class _RenderingBridgeStub(_BridgeStub):
    def __init__(self, injection_path: Path, context_messages: list[ChatMessageEvent]) -> None:
        super().__init__(injection_path, context_messages)
        self.rendered_intents: list[str] = []

    async def render_advance_message(self, directive: Directive) -> str:
        self.rendered_intents.append(directive.content)
        return "我伸手碰了碰最近的符文，看它有没有反应。"


def _config(data_dir: Path) -> NodeSyncConfig:
    port = _free_port()
    return NodeSyncConfig(
        mode="server",
        bot_id="bot-integration",
        bot_name="Integration Bot",
        maibot_version="test",
        adapter_version="nodesync-test",
        auth_token="test-token",
        server_host="127.0.0.1",
        server_port=port,
        server_url=f"http://127.0.0.1:{port}",
        data_dir=data_dir,
        enable_llm_decision=False,
        enable_llm_advance_generation=False,
        intervention_cooldown_seconds=0,
        directive_retry_interval_seconds=60,
        analysis_min_messages=4,
    )


def _stagnant_messages() -> list[ChatMessageEvent]:
    texts = [
        "RP 场景：我们在门口。",
        "我看看。",
        "我也看看。",
        "继续看看。",
        "还是看看。",
    ]
    return [
        ChatMessageEvent(
            stream_id="group-1",
            message_id=f"msg-{index}",
            sender_id=f"u{index % 3}",
            sender_name=f"u{index % 3}",
            plain_text=text,
            timestamp=now_ts() + index,
        )
        for index, text in enumerate(texts)
    ]


def _directive(directive_id: str, directive_type: str) -> Directive:
    return Directive(
        directive_id=directive_id,
        session_id="scene-1",
        stream_id="group-1",
        target_bot_id="bot-integration",
        directive_type=directive_type,
        content="请推进当前场景到下一步。",
        source_scene_snapshot={"topic": "遗迹门口的抉择"},
        created_at=now_ts(),
        expires_at=now_ts() + 300,
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_until(predicate: Any, max_wait: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + max_wait
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("等待条件超时")


def _server_has_live_client(server: NodeSyncCoordinator, bot_id: str) -> bool:
    return bot_id in server._clients  # noqa: SLF001


def _server_has_status(server: NodeSyncCoordinator, status: str) -> bool:
    return status in _directive_statuses(server)


def _directive_statuses(server: NodeSyncCoordinator) -> list[str]:
    with server.storage.connect() as conn:
        rows = conn.execute("SELECT status FROM directives ORDER BY updated_at").fetchall()
    return [str(row["status"]) for row in rows]


def _connected_client(bot_id: str) -> ConnectedClient:
    registration = ClientRegistration(
        bot_id=bot_id,
        bot_name=bot_id,
        maibot_version="test",
        adapter_version="test",
        capabilities=[],
    )
    return ConnectedClient(ws=_WsStub(), registration=registration, connected_at=now_ts(), last_seen_at=now_ts())


class _WsStub:
    pass


if __name__ == "__main__":
    unittest.main()
