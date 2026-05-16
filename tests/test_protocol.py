from __future__ import annotations

import unittest

from nodesync.shared.models import (
    BotCapability,
    ChatMessageEvent,
    ClientRegistration,
    ContextRequest,
    ContextResponse,
    Envelope,
)


class ProtocolModelTest(unittest.TestCase):
    def test_envelope_round_trip(self) -> None:
        envelope = Envelope(type="client.heartbeat", payload={"bot_id": "bot-a"}, request_id="req-1")
        restored = Envelope.from_dict(envelope.to_dict())

        self.assertEqual(restored.type, "client.heartbeat")
        self.assertEqual(restored.request_id, "req-1")
        self.assertEqual(restored.payload["bot_id"], "bot-a")

    def test_registration_round_trip(self) -> None:
        registration = ClientRegistration(
            bot_id="bot-a",
            bot_name="Bot A",
            maibot_version="0.12.2",
            adapter_version="nodesync-0.1.0",
            capabilities=[BotCapability.SEND_MESSAGE.value],
            streams=["group-1"],
        )
        restored = ClientRegistration.from_dict(registration.to_dict())

        self.assertEqual(restored.bot_id, "bot-a")
        self.assertEqual(restored.capabilities, [BotCapability.SEND_MESSAGE.value])

    def test_context_payload_keeps_messages(self) -> None:
        request = ContextRequest(session_id="scene-1", stream_id="group-1", limit=10)
        event = ChatMessageEvent(
            stream_id=request.stream_id,
            message_id="m1",
            sender_id="u1",
            sender_name="User",
            plain_text="hello",
        )
        response = ContextResponse(
            session_id=request.session_id,
            stream_id=request.stream_id,
            messages=[event.to_dict()],
        )
        restored = ContextResponse.from_dict(response.to_dict())

        self.assertEqual(restored.messages[0]["plain_text"], "hello")


if __name__ == "__main__":
    unittest.main()
