from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nodesync.core.directive_result_store import DirectiveResultStore
from nodesync.core.storage import NodeSyncStorage
from nodesync.shared.models import Directive, DirectiveResult, DirectiveType
from nodesync.shared.time_utils import now_ts


class DirectiveLifecycleTest(unittest.TestCase):
    def test_find_open_directive_by_idempotency_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = NodeSyncStorage(Path(tmp) / "nodesync.sqlite3")
            directive = _directive(idempotency_key="same-key")
            storage.add_directive(directive, status="pushed")

            found = storage.find_open_directive_by_key("same-key")

            self.assertIsNotNone(found)
            self.assertEqual(found.directive_id, directive.directive_id)

    def test_retryable_directive_excludes_exhausted_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = NodeSyncStorage(Path(tmp) / "nodesync.sqlite3")
            directive = _directive(idempotency_key="retry-key")
            directive.attempts = 3
            directive.created_at = now_ts() - 100
            storage.add_directive(directive, status="offline")

            retryable = storage.list_retryable_directives(
                retry_statuses={"offline"},
                retry_after_seconds=1,
                max_attempts=3,
            )

            self.assertEqual(retryable, [])

    def test_directive_result_store_returns_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = DirectiveResultStore(Path(tmp) / "directive_results.jsonl")
            store.record(DirectiveResult(directive_id="d1", ok=False, message="failed"))
            store.record(DirectiveResult(directive_id="d1", ok=True, message="ok"))

            result = store.get("d1")

            self.assertIsNotNone(result)
            self.assertTrue(result.ok)
            self.assertEqual(result.message, "ok")


def _directive(idempotency_key: str) -> Directive:
    return Directive(
        directive_id="d1",
        session_id="scene-1",
        stream_id="group-1",
        target_bot_id="bot-a",
        directive_type=DirectiveType.ADVANCE_DIALOGUE.value,
        content="继续推进",
        source_scene_snapshot={"topic": "测试"},
        idempotency_key=idempotency_key,
        expires_at=now_ts() + 60,
    )


if __name__ == "__main__":
    unittest.main()
