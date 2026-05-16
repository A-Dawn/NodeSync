"""适配层通用工具。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from nodesync.shared.diagnostics import record_message_schema_snapshot
from nodesync.shared.models import ChatMessageEvent, InjectionRecord
from nodesync.shared.texts import build_prompt_injection
from nodesync.shared.time_utils import now_ts


def object_to_chat_event(raw: Any, default_stream_id: str = "", bot_id: str = "") -> ChatMessageEvent:
    """把 MaiBot 不同版本的消息对象尽量转换为 NodeSync 事件。"""

    payload = _as_mapping(raw)
    base_info = _as_mapping(_pick(raw, payload, "message_base_info", default={}))
    user_info = _as_mapping(base_info.get("user_info") or base_info.get("user") or getattr(raw, "user_info", {}))
    sender_id = _first_text(
        payload,
        raw,
        ("sender_id", "user_id", "person_id", "sender"),
        default=str(user_info.get("user_id") or user_info.get("platform_id") or ""),
    )
    sender_name = _first_text(
        payload,
        raw,
        ("sender_name", "nickname", "user_cardname", "user_nickname", "name"),
        default=str(
            user_info.get("user_cardname") or user_info.get("user_nickname") or user_info.get("nickname") or sender_id
        ),
    )
    stream_id = _first_text(
        payload,
        raw,
        ("stream_id", "chat_info_stream_id", "chat_id", "group_id"),
        default=default_stream_id,
    )
    text = _first_text(
        payload,
        raw,
        ("plain_text", "processed_plain_text", "display_message", "text", "content", "raw_message"),
        default="",
    )
    timestamp_value = _first_value(payload, raw, ("timestamp", "time", "create_time"), default=None)
    try:
        timestamp = float(timestamp_value if timestamp_value is not None else now_ts())
    except (TypeError, ValueError):
        timestamp = now_ts()
    message_id = _first_text(payload, raw, ("message_id", "id", "message_info_id"), default=uuid4().hex)
    is_bot = bool(payload.get("is_bot") or base_info.get("is_bot") or (bot_id and sender_id == bot_id))
    return ChatMessageEvent(
        stream_id=stream_id,
        message_id=message_id,
        sender_id=sender_id,
        sender_name=sender_name,
        plain_text=text,
        timestamp=timestamp,
        is_bot=is_bot,
    )


def build_alignment_prompt(base_prompt: str, record: InjectionRecord | None) -> str:
    """把有效注入拼接到 LLM prompt 尾部。"""

    return build_prompt_injection(base_prompt, record)


def record_message_diagnostic(config: Any, source: str, raw: Any, event: ChatMessageEvent | None = None) -> None:
    """按配置记录脱敏消息结构诊断。"""

    if not bool(getattr(config, "diagnostics_enabled", False)):
        return
    record_message_schema_snapshot(
        output_dir=config.diagnostics_dir,
        source=source,
        raw=raw,
        converted=event,
        include_hashes=bool(getattr(config, "diagnostics_include_hashes", True)),
        max_depth=int(getattr(config, "diagnostics_max_depth", 4)),
        max_items=int(getattr(config, "diagnostics_max_items", 40)),
    )


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "to_dict"):
        try:
            data = value.to_dict()
            if isinstance(data, Mapping):
                return dict(data)
        except Exception:
            pass
    if hasattr(value, "flatten"):
        try:
            data = value.flatten()
            if isinstance(data, Mapping):
                return dict(data)
        except Exception:
            return {}
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {}


def _pick(raw: Any, payload: dict[str, Any], name: str, default: Any = "") -> Any:
    if name in payload:
        return payload[name]
    return getattr(raw, name, default)


def _first_text(payload: dict[str, Any], raw: Any, names: tuple[str, ...], default: str = "") -> str:
    for name in names:
        value = _pick(raw, payload, name, default="")
        if value is not None and value != "":
            return str(value)
    return default


def _first_value(payload: dict[str, Any], raw: Any, names: tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        value = _pick(raw, payload, name, default=None)
        if value is not None and value != "":
            return value
    return default
