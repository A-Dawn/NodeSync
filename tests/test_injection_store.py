from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nodesync.core.injection_store import InjectionStore
from nodesync.shared.models import Directive, DirectiveType, InjectionStatus
from nodesync.shared.time_utils import now_ts


class InjectionStoreTest(unittest.TestCase):
    def test_record_pending_then_mark_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = InjectionStore(Path(tmp) / "context_injections.jsonl")
            directive = _directive()

            record = store.record_pending(directive)
            self.assertEqual(record.status, InjectionStatus.PENDING.value)

            applied = store.mark_applied(directive.directive_id)
            self.assertIsNotNone(applied)
            active = store.get_active(stream_id=directive.stream_id, session_id=directive.session_id)
            self.assertIsNotNone(active)
            self.assertEqual(active.status, InjectionStatus.APPLIED.value)

    def test_broken_jsonl_line_does_not_break_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context_injections.jsonl"
            store = InjectionStore(path)
            directive = _directive()
            store.record_pending(directive)
            with path.open("a", encoding="utf-8") as handle:
                handle.write("{broken json}\n")

            recent = store.get_recent(stream_id=directive.stream_id, limit=5)
            self.assertEqual(len(recent), 1)
            self.assertEqual(recent[0].injection_id, directive.directive_id)

    def test_expired_record_is_not_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = InjectionStore(Path(tmp) / "context_injections.jsonl")
            directive = _directive(expires_at=now_ts() - 1)
            store.record_pending(directive)

            self.assertIsNone(store.get_active(stream_id=directive.stream_id, session_id=directive.session_id))
            self.assertEqual(store.expire_old_records(), 1)


def _directive(expires_at: float | None = None) -> Directive:
    return Directive(
        directive_id="inj-1",
        session_id="scene-1",
        stream_id="group-1",
        target_bot_id="bot-a",
        directive_type=DirectiveType.ALIGN_CONTEXT.value,
        content="推进当前话题到下一步。",
        source_scene_snapshot={"topic": "测试话题"},
        expires_at=expires_at if expires_at is not None else now_ts() + 60,
    )


if __name__ == "__main__":
    unittest.main()
