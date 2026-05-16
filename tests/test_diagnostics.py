from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nodesync.adapters.common import object_to_chat_event, record_message_diagnostic
from nodesync.core.runtime_config import NodeSyncConfig
from nodesync.shared.jsonio import iter_jsonl


class DiagnosticsTest(unittest.TestCase):
    def test_message_schema_snapshot_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = NodeSyncConfig.from_mapping(
                {
                    "data_dir": str(Path(tmp) / "data"),
                    "diagnostics_enabled": True,
                    "diagnostics_dir": str(Path(tmp) / "diagnostics"),
                    "diagnostics_include_hashes": True,
                }
            )
            raw = {
                "message_id": "msg-secret",
                "stream_id": "group-secret",
                "user_id": "user-secret",
                "processed_plain_text": "这是一段不能落盘的真实群聊文本",
                "auth_token": "should-not-appear",
                "nested": {"api_key": "also-secret", "content": "也不能出现"},
            }
            event = object_to_chat_event(raw)

            record_message_diagnostic(config, "unit.on_message", raw, event)

            payloads = list(iter_jsonl(config.diagnostics_dir / "message_schema.jsonl"))
            self.assertEqual(len(payloads), 1)
            serialized = str(payloads[0])
            self.assertIn("unit.on_message", serialized)
            self.assertIn("processed_plain_text", serialized)
            self.assertIn("sha256_12", serialized)
            self.assertNotIn("这是一段不能落盘", serialized)
            self.assertNotIn("should-not-appear", serialized)
            self.assertNotIn("also-secret", serialized)
            self.assertNotIn("group-secret", serialized)

    def test_disabled_diagnostics_do_not_write_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = NodeSyncConfig.from_mapping({"data_dir": str(Path(tmp) / "data")})
            record_message_diagnostic(config, "unit.on_message", {"plain_text": "hello"})

            self.assertFalse((config.diagnostics_dir / "message_schema.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
