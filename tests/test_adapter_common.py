from __future__ import annotations

import unittest

from nodesync.adapters.common import object_to_chat_event


class AdapterCommonTest(unittest.TestCase):
    def test_flattened_database_message_fields(self) -> None:
        raw = {
            "message_id": "m1",
            "time": 123.0,
            "chat_info_stream_id": "stream-from-chat-info",
            "processed_plain_text": "处理后的文本",
            "user_id": "u1",
            "user_cardname": "群名片",
        }

        event = object_to_chat_event(raw, default_stream_id="fallback")

        self.assertEqual(event.stream_id, "stream-from-chat-info")
        self.assertEqual(event.sender_id, "u1")
        self.assertEqual(event.sender_name, "群名片")
        self.assertEqual(event.plain_text, "处理后的文本")
        self.assertEqual(event.timestamp, 123.0)


if __name__ == "__main__":
    unittest.main()
