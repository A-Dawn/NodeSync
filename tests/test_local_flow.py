from __future__ import annotations

import socket
import unittest

from aiohttp import ClientSession, ClientTimeout

from nodesync.adapters.local_flow import LocalFlowConfig, LocalFlowHub, LocalFlowInputServer, merge_chat_events
from nodesync.shared.models import ChatMessageEvent, Directive, DirectiveType
from nodesync.shared.time_utils import now_ts


class LocalFlowTest(unittest.IsolatedAsyncioTestCase):
    async def test_http_message_entry_reports_to_runtime_and_keeps_history(self) -> None:
        port = _free_port()
        config = LocalFlowConfig(enabled=True, port=port, auth_token="local-flow-token")
        runtime = _RuntimeStub()
        hub = LocalFlowHub(config, bot_id="bot-a", bot_name="Bot A")
        hub.bind_runtime(runtime)
        server = LocalFlowInputServer(config, hub)
        await server.start()
        try:
            async with ClientSession(timeout=ClientTimeout(total=5)) as session:
                response = await session.post(
                    f"http://127.0.0.1:{port}/messages",
                    headers={"Authorization": "Bearer local-flow-token"},
                    json={
                        "stream_id": "group-local",
                        "sender_id": "u1",
                        "sender_name": "测试用户",
                        "plain_text": "RP 场景：我们来到门前。",
                    },
                )
                try:
                    self.assertEqual(response.status, 200)
                    payload = await response.json()
                finally:
                    response.release()

                response = await session.get(
                    f"http://127.0.0.1:{port}/messages/group-local",
                    headers={"Authorization": "Bearer local-flow-token"},
                )
                try:
                    self.assertEqual(response.status, 200)
                    history = await response.json()
                finally:
                    response.release()

            self.assertTrue(payload["reported"])
            self.assertEqual(payload["event"]["plain_text"], "RP 场景：我们来到门前。")
            self.assertEqual(runtime.events[0].stream_id, "group-local")
            self.assertEqual(history["messages"][0]["sender_name"], "测试用户")
        finally:
            await server.stop()

    async def test_http_message_entry_requires_auth_when_token_is_set(self) -> None:
        port = _free_port()
        config = LocalFlowConfig(enabled=True, port=port, auth_token="local-flow-token")
        hub = LocalFlowHub(config, bot_id="bot-a", bot_name="Bot A")
        server = LocalFlowInputServer(config, hub)
        await server.start()
        try:
            async with (
                ClientSession(timeout=ClientTimeout(total=5)) as session,
                session.post(
                    f"http://127.0.0.1:{port}/messages",
                    json={"plain_text": "这条消息不应通过。"},
                ) as response,
            ):
                self.assertEqual(response.status, 401)
        finally:
            await server.stop()

    async def test_http_message_entry_can_remember_without_reporting(self) -> None:
        port = _free_port()
        config = LocalFlowConfig(enabled=True, port=port, auth_token="local-flow-token")
        runtime = _RuntimeStub()
        hub = LocalFlowHub(config, bot_id="bot-a", bot_name="Bot A")
        hub.bind_runtime(runtime)
        server = LocalFlowInputServer(config, hub)
        await server.start()
        try:
            async with ClientSession(timeout=ClientTimeout(total=5)) as session:
                response = await session.post(
                    f"http://127.0.0.1:{port}/messages",
                    headers={"Authorization": "Bearer local-flow-token"},
                    json={
                        "stream_id": "group-local",
                        "plain_text": "这条消息只进入本地视野。",
                        "report": False,
                    },
                )
                try:
                    self.assertEqual(response.status, 200)
                    payload = await response.json()
                finally:
                    response.release()

            self.assertFalse(payload["reported"])
            self.assertEqual(runtime.events, [])
            self.assertEqual(hub.get_recent("group-local", 1)[0].plain_text, "这条消息只进入本地视野。")
        finally:
            await server.stop()

    async def test_capture_outbound_reenters_local_message_flow(self) -> None:
        config = LocalFlowConfig(enabled=True, auth_token="local-flow-token")
        runtime = _RuntimeStub()
        hub = LocalFlowHub(config, bot_id="bot-a", bot_name="Bot A")
        hub.bind_runtime(runtime)
        directive = Directive(
            directive_id="directive-1",
            session_id="scene-1",
            stream_id="group-local",
            target_bot_id="bot-a",
            directive_type=DirectiveType.ADVANCE_DIALOGUE.value,
            content="我们先决定谁去推门。",
            source_scene_snapshot={},
            created_at=now_ts(),
            expires_at=now_ts() + 300,
        )

        self.assertTrue(await hub.capture_outbound(directive))

        self.assertEqual(runtime.events[0].plain_text, "我们先决定谁去推门。")
        self.assertTrue(runtime.events[0].is_bot)
        self.assertEqual(hub.get_recent("group-local", 1)[0].sender_id, "bot-a")

    def test_merge_chat_events_deduplicates_and_orders_messages(self) -> None:
        older = ChatMessageEvent(
            stream_id="group-local",
            message_id="m1",
            sender_id="u1",
            sender_name="用户1",
            plain_text="旧消息",
            timestamp=1.0,
        )
        newer = ChatMessageEvent(
            stream_id="group-local",
            message_id="m2",
            sender_id="u2",
            sender_name="用户2",
            plain_text="新消息",
            timestamp=2.0,
        )
        duplicate = ChatMessageEvent(
            stream_id="group-local",
            message_id="m1",
            sender_id="u1",
            sender_name="用户1",
            plain_text="旧消息覆盖",
            timestamp=1.5,
        )

        merged = merge_chat_events([older], [duplicate, newer], limit=5)

        self.assertEqual([item["message_id"] for item in merged], ["m1", "m2"])
        self.assertEqual(merged[0]["plain_text"], "旧消息覆盖")


class _RuntimeStub:
    def __init__(self) -> None:
        self.events: list[ChatMessageEvent] = []

    async def report_message(self, event: ChatMessageEvent) -> bool:
        self.events.append(event)
        return True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    unittest.main()
