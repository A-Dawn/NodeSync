"""受控诊断快照。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from nodesync.shared.jsonio import append_jsonl
from nodesync.shared.logging_utils import get_logger
from nodesync.shared.models import ChatMessageEvent
from nodesync.shared.time_utils import now_ts

logger = get_logger("shared.diagnostics")

SENSITIVE_KEY_PARTS = (
    "authorization",
    "access_token",
    "auth_token",
    "api_key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "cookie",
    "session",
    "credential",
)
TEXT_KEY_PARTS = (
    "plain_text",
    "processed_plain_text",
    "display_message",
    "raw_message",
    "content",
    "message",
    "prompt",
    "text",
)


def record_message_schema_snapshot(
    *,
    output_dir: Path,
    source: str,
    raw: Any,
    converted: ChatMessageEvent | None = None,
    include_hashes: bool = True,
    max_depth: int = 4,
    max_items: int = 40,
) -> None:
    """记录一条脱敏消息字段结构快照。

    诊断文件只描述字段名、类型、长度和可选 hash，不写入原始文本、token 或 cookie。
    """

    try:
        payload = {
            "created_at": now_ts(),
            "source": source,
            "raw_type": _type_name(raw),
            "raw_forms": _raw_forms(raw, include_hashes=include_hashes, max_depth=max_depth, max_items=max_items),
        }
        if converted is not None:
            payload["converted_event"] = _converted_event_summary(converted, include_hashes)
        append_jsonl(output_dir / "message_schema.jsonl", payload)
    except Exception as exc:
        logger.warning("[NodeSyncDiagnostics] 写入消息诊断快照失败: %s", exc)


def _raw_forms(raw: Any, *, include_hashes: bool, max_depth: int, max_items: int) -> dict[str, Any]:
    forms: dict[str, Any] = {"direct": _describe_value(raw, "", include_hashes, max_depth, max_items)}
    for method_name in ("flatten", "to_dict", "model_dump", "to_rpc_dict"):
        method = getattr(raw, method_name, None)
        if not callable(method):
            continue
        try:
            data = method()
        except Exception as exc:
            forms[method_name] = {"error": type(exc).__name__}
            continue
        forms[method_name] = _describe_value(data, method_name, include_hashes, max_depth, max_items)
    return forms


def _describe_value(value: Any, key: str, include_hashes: bool, depth: int, max_items: int) -> dict[str, Any]:
    type_name = _type_name(value)
    if _is_sensitive_key(key):
        return {"type": type_name, "redacted": True}
    if depth <= 0:
        return {"type": type_name, "truncated": "max_depth"}
    if isinstance(value, Mapping):
        items = list(value.items())
        described = {
            str(item_key): _describe_value(item_value, str(item_key), include_hashes, depth - 1, max_items)
            for item_key, item_value in items[:max_items]
        }
        return {
            "type": type_name,
            "size": len(items),
            "fields": described,
            "truncated": len(items) > max_items,
        }
    if isinstance(value, list | tuple | set):
        items = list(value)
        return {
            "type": type_name,
            "size": len(items),
            "items": [_describe_value(item, key, include_hashes, depth - 1, max_items) for item in items[:max_items]],
            "truncated": len(items) > max_items,
        }
    if isinstance(value, str):
        summary: dict[str, Any] = {"type": "str", "length": len(value)}
        if include_hashes:
            summary["sha256_12"] = _hash_text(value)
        if _is_text_key(key):
            summary["text_redacted"] = True
        return summary
    if isinstance(value, int | float | bool) or value is None:
        return {"type": type_name}
    mapping = _object_mapping(value)
    if mapping:
        return {
            "type": type_name,
            "object_fields": _describe_value(mapping, key, include_hashes, depth - 1, max_items),
        }
    return {"type": type_name}


def _converted_event_summary(event: ChatMessageEvent, include_hashes: bool) -> dict[str, Any]:
    return {
        "stream_id": _redacted_identifier(event.stream_id, include_hashes),
        "message_id": _redacted_identifier(event.message_id, include_hashes),
        "sender_id": _redacted_identifier(event.sender_id, include_hashes),
        "sender_name": _redacted_identifier(event.sender_name, include_hashes),
        "plain_text": {"length": len(event.plain_text), "sha256_12": _hash_text(event.plain_text) if include_hashes else ""},
        "timestamp": event.timestamp,
        "is_bot": event.is_bot,
    }


def _redacted_identifier(value: str, include_hashes: bool) -> dict[str, Any]:
    return {
        "length": len(value),
        "sha256_12": _hash_text(value) if include_hashes else "",
    }


def _object_mapping(value: Any) -> dict[str, Any]:
    if hasattr(value, "__dict__"):
        try:
            return dict(vars(value))
        except Exception:
            return {}
    return {}


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def _is_text_key(key: str) -> bool:
    normalized = key.lower()
    return any(part in normalized for part in TEXT_KEY_PARTS)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _type_name(value: Any) -> str:
    return f"{type(value).__module__}.{type(value).__qualname__}"
