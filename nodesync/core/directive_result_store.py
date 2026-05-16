"""客户端本地指令执行结果缓存。"""

from __future__ import annotations

from pathlib import Path

from nodesync.shared.jsonio import append_jsonl, iter_jsonl
from nodesync.shared.models import DirectiveResult


class DirectiveResultStore:
    """基于 JSONL 的指令结果缓存，用于重复指令幂等返回。"""

    def __init__(self, path: Path):
        self.path = path

    def record(self, result: DirectiveResult) -> None:
        """追加保存一次指令执行结果。"""

        append_jsonl(self.path, result.to_dict())

    def get(self, directive_id: str) -> DirectiveResult | None:
        """读取某个指令最近一次执行结果。"""

        latest: DirectiveResult | None = None
        for raw in iter_jsonl(self.path):
            try:
                result = DirectiveResult.from_dict(raw)
            except Exception:
                continue
            if result.directive_id == directive_id:
                latest = result
        return latest
