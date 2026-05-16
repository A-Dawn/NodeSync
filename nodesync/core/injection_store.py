"""本地持久化上下文注入历史。"""

from __future__ import annotations

from pathlib import Path

from nodesync.shared.jsonio import append_jsonl, iter_jsonl
from nodesync.shared.models import Directive, DirectiveType, InjectionRecord, InjectionStatus
from nodesync.shared.time_utils import now_ts


class InjectionStore:
    """基于 JSONL 的本地注入存储。

    JSONL 只追加不原地改写。状态变化会追加同一 injection_id 的新快照；
    读取时以后写入的记录为准。
    """

    def __init__(self, path: Path):
        self.path = path

    def record_pending(self, directive: Directive) -> InjectionRecord:
        """在执行注入前先持久化指令。"""

        record = InjectionRecord(
            injection_id=directive.directive_id,
            session_id=directive.session_id,
            stream_id=directive.stream_id,
            target_bot_id=directive.target_bot_id,
            directive_type=directive.directive_type,
            content=directive.content,
            source_scene_snapshot=directive.source_scene_snapshot,
            created_at=directive.created_at,
            expires_at=directive.expires_at,
            status=InjectionStatus.PENDING.value,
        )
        append_jsonl(self.path, record.to_dict())
        return record

    def mark_status(self, injection_id: str, status: str, applied_at: float | None = None) -> InjectionRecord | None:
        """为某条注入追加状态更新。"""

        records = self._latest_by_id()
        record = records.get(injection_id)
        if record is None:
            return None
        updated = InjectionRecord(
            injection_id=record.injection_id,
            session_id=record.session_id,
            stream_id=record.stream_id,
            target_bot_id=record.target_bot_id,
            directive_type=record.directive_type,
            content=record.content,
            source_scene_snapshot=record.source_scene_snapshot,
            created_at=record.created_at,
            expires_at=record.expires_at,
            applied_at=applied_at if applied_at is not None else record.applied_at,
            status=status,
        )
        append_jsonl(self.path, updated.to_dict())
        return updated

    def mark_applied(self, injection_id: str) -> InjectionRecord | None:
        """标记注入已应用。"""

        return self.mark_status(injection_id, InjectionStatus.APPLIED.value, now_ts())

    def expire_old_records(self, now: float | None = None) -> int:
        """把已过期的活跃记录标记为 expired。"""

        current = now if now is not None else now_ts()
        count = 0
        for record in self._latest_by_id().values():
            if record.expires_at and record.expires_at < current and record.status in {
                InjectionStatus.PENDING.value,
                InjectionStatus.APPLIED.value,
            }:
                self.mark_status(record.injection_id, InjectionStatus.EXPIRED.value)
                count += 1
        return count

    def get_active(self, stream_id: str, session_id: str = "", now: float | None = None) -> InjectionRecord | None:
        """返回匹配 stream/session 的最新未过期注入。"""

        current = now if now is not None else now_ts()
        candidates = []
        for record in self._latest_by_id().values():
            if record.stream_id != stream_id:
                continue
            if session_id and record.session_id != session_id:
                continue
            if record.directive_type != DirectiveType.ALIGN_CONTEXT.value:
                continue
            if record.status not in {InjectionStatus.PENDING.value, InjectionStatus.APPLIED.value}:
                continue
            if record.expires_at and record.expires_at < current:
                continue
            candidates.append(record)
        candidates.sort(key=lambda item: item.created_at, reverse=True)
        return candidates[0] if candidates else None

    def get_recent(self, stream_id: str = "", session_id: str = "", limit: int = 5) -> list[InjectionRecord]:
        """返回最近的注入记录。"""

        records = list(self._latest_by_id().values())
        filtered = []
        for record in records:
            if stream_id and record.stream_id != stream_id:
                continue
            if session_id and record.session_id != session_id:
                continue
            filtered.append(record)
        filtered.sort(key=lambda item: item.created_at, reverse=True)
        return filtered[:limit]

    def _latest_by_id(self) -> dict[str, InjectionRecord]:
        latest: dict[str, InjectionRecord] = {}
        for raw in iter_jsonl(self.path):
            try:
                record = InjectionRecord.from_dict(raw)
            except Exception:
                continue
            if not record.injection_id:
                continue
            latest[record.injection_id] = record
        return latest
