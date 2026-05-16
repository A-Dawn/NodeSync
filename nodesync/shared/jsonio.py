"""集中封装 JSON 与 JSONL 文件操作。

所有 JSON 文件访问都应走这里，确保损坏行处理与编码行为一致。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def dumps_json(data: dict[str, Any]) -> str:
    """序列化用于协议传输或 JSONL 写入的对象。"""

    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def loads_json(raw: str) -> dict[str, Any]:
    """反序列化 JSON 对象。"""

    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("JSON payload 必须是对象")
    return value


def append_jsonl(path: Path, data: dict[str, Any]) -> None:
    """向 JSONL 文件追加一行对象。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(dumps_json(data))
        handle.write("\n")


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    """读取有效 JSONL 行，并跳过损坏行。"""

    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            try:
                yield loads_json(raw)
            except Exception:
                continue
