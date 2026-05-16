"""NodeSync 协议模型。

核心 payload 使用 dataclass，避免在业务层裸传 dict。所有 dict 转换都在边界显式完成。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any
from uuid import uuid4

from .time_utils import now_ts


class WireValue(str, Enum):
    """可稳定序列化为字符串的枚举基类。"""

    def __str__(self) -> str:
        return self.value


class BotCapability(WireValue):
    MESSAGE_REPORT = "message_report"
    CONTEXT_FETCH = "context_fetch"
    LLM_GENERATE = "llm_generate"
    CONTEXT_INJECTION = "context_injection"
    SEND_MESSAGE = "send_message"


class DirectiveType(WireValue):
    ALIGN_CONTEXT = "align_context"
    ADVANCE_DIALOGUE = "advance_dialogue"
    NO_OP = "no_op"


class DecisionKind(WireValue):
    NO_OP = "no_op"
    ALIGN_CONTEXT = "align_context"
    ADVANCE_DIALOGUE = "advance_dialogue"


class InjectionStatus(WireValue):
    PENDING = "pending"
    APPLIED = "applied"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class SceneMode(WireValue):
    DISCUSSION = "discussion"
    ROLEPLAY = "roleplay"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class Envelope:
    """WebSocket 消息信封。"""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "request_id": self.request_id, "payload": self.payload}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Envelope:
        return cls(
            type=str(data.get("type", "")),
            request_id=str(data.get("request_id", "") or uuid4().hex),
            payload=dict(data.get("payload") or {}),
        )


@dataclass(slots=True)
class ClientRegistration:
    bot_id: str
    bot_name: str
    maibot_version: str
    adapter_version: str
    capabilities: list[str]
    streams: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _to_wire_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClientRegistration:
        return cls(
            bot_id=str(data.get("bot_id", "")),
            bot_name=str(data.get("bot_name", "")),
            maibot_version=str(data.get("maibot_version", "")),
            adapter_version=str(data.get("adapter_version", "")),
            capabilities=[str(item) for item in data.get("capabilities", [])],
            streams=[str(item) for item in data.get("streams", [])],
        )


@dataclass(slots=True)
class ChatMessageEvent:
    stream_id: str
    message_id: str
    sender_id: str
    sender_name: str
    plain_text: str
    timestamp: float = field(default_factory=now_ts)
    is_bot: bool = False

    def to_dict(self) -> dict[str, Any]:
        return _to_wire_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatMessageEvent:
        return cls(
            stream_id=str(data.get("stream_id", "")),
            message_id=str(data.get("message_id", "")),
            sender_id=str(data.get("sender_id", "")),
            sender_name=str(data.get("sender_name", "")),
            plain_text=str(data.get("plain_text", "")),
            timestamp=float(data.get("timestamp") or now_ts()),
            is_bot=bool(data.get("is_bot", False)),
        )


@dataclass(slots=True)
class ContextRequest:
    session_id: str
    stream_id: str
    limit: int = 40
    include_injection_history: bool = True
    injection_history_limit: int = 5

    def to_dict(self) -> dict[str, Any]:
        return _to_wire_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContextRequest:
        return cls(
            session_id=str(data.get("session_id", "")),
            stream_id=str(data.get("stream_id", "")),
            limit=int(data.get("limit", 40)),
            include_injection_history=bool(data.get("include_injection_history", True)),
            injection_history_limit=int(data.get("injection_history_limit", 5)),
        )


@dataclass(slots=True)
class ContextResponse:
    session_id: str
    stream_id: str
    messages: list[dict[str, Any]]
    injection_history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _to_wire_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ContextResponse:
        return cls(
            session_id=str(data.get("session_id", "")),
            stream_id=str(data.get("stream_id", "")),
            messages=[dict(item) for item in data.get("messages", [])],
            injection_history=[dict(item) for item in data.get("injection_history", [])],
        )


@dataclass(slots=True)
class LLMRequest:
    request_id: str
    prompt: str
    model_name: str = ""
    temperature: float = 0.2
    max_tokens: int = 1200

    def to_dict(self) -> dict[str, Any]:
        return _to_wire_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LLMRequest:
        return cls(
            request_id=str(data.get("request_id", "") or uuid4().hex),
            prompt=str(data.get("prompt", "")),
            model_name=str(data.get("model_name", "")),
            temperature=float(data.get("temperature", 0.2)),
            max_tokens=int(data.get("max_tokens", 1200)),
        )


@dataclass(slots=True)
class LLMResponse:
    request_id: str
    ok: bool
    content: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _to_wire_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LLMResponse:
        return cls(
            request_id=str(data.get("request_id", "")),
            ok=bool(data.get("ok", False)),
            content=str(data.get("content", "")),
            error=str(data.get("error", "")),
        )


@dataclass(slots=True)
class SceneState:
    session_id: str
    stream_id: str
    mode: str = SceneMode.UNKNOWN.value
    topic: str = ""
    stage: str = "opening"
    goal: str = ""
    known_facts: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    participants: list[str] = field(default_factory=list)
    current_loop: str = ""
    progress_delta: str = ""
    stagnation_score: float = 0.0
    last_progress_at: float = field(default_factory=now_ts)
    last_intervention_at: float = 0.0
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)

    def to_dict(self) -> dict[str, Any]:
        return _to_wire_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SceneState:
        return cls(
            session_id=str(data.get("session_id", "")),
            stream_id=str(data.get("stream_id", "")),
            mode=str(data.get("mode", SceneMode.UNKNOWN.value)),
            topic=str(data.get("topic", "")),
            stage=str(data.get("stage", "opening")),
            goal=str(data.get("goal", "")),
            known_facts=[str(item) for item in data.get("known_facts", [])],
            open_questions=[str(item) for item in data.get("open_questions", [])],
            participants=[str(item) for item in data.get("participants", [])],
            current_loop=str(data.get("current_loop", "")),
            progress_delta=str(data.get("progress_delta", "")),
            stagnation_score=float(data.get("stagnation_score", 0.0)),
            last_progress_at=float(data.get("last_progress_at") or now_ts()),
            last_intervention_at=float(data.get("last_intervention_at", 0.0)),
            created_at=float(data.get("created_at") or now_ts()),
            updated_at=float(data.get("updated_at") or now_ts()),
        )


@dataclass(slots=True)
class Directive:
    directive_id: str
    session_id: str
    stream_id: str
    target_bot_id: str
    directive_type: str
    content: str
    source_scene_snapshot: dict[str, Any]
    idempotency_key: str = ""
    attempts: int = 0
    last_error: str = ""
    priority: int = 3
    created_at: float = field(default_factory=now_ts)
    expires_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return _to_wire_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Directive:
        return cls(
            directive_id=str(data.get("directive_id", "") or uuid4().hex),
            session_id=str(data.get("session_id", "")),
            stream_id=str(data.get("stream_id", "")),
            target_bot_id=str(data.get("target_bot_id", "")),
            directive_type=str(data.get("directive_type", "")),
            content=str(data.get("content", "")),
            source_scene_snapshot=dict(data.get("source_scene_snapshot") or {}),
            idempotency_key=str(data.get("idempotency_key", "")),
            attempts=int(data.get("attempts", 0)),
            last_error=str(data.get("last_error", "")),
            priority=int(data.get("priority", 3)),
            created_at=float(data.get("created_at") or now_ts()),
            expires_at=float(data.get("expires_at", 0.0)),
        )


@dataclass(slots=True)
class DirectiveResult:
    directive_id: str
    ok: bool
    message: str = ""
    applied_at: float = field(default_factory=now_ts)

    def to_dict(self) -> dict[str, Any]:
        return _to_wire_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DirectiveResult:
        return cls(
            directive_id=str(data.get("directive_id", "")),
            ok=bool(data.get("ok", False)),
            message=str(data.get("message", "")),
            applied_at=float(data.get("applied_at") or now_ts()),
        )


@dataclass(slots=True)
class InjectionRecord:
    injection_id: str
    session_id: str
    stream_id: str
    target_bot_id: str
    directive_type: str
    content: str
    source_scene_snapshot: dict[str, Any]
    created_at: float
    expires_at: float
    applied_at: float = 0.0
    status: str = InjectionStatus.PENDING.value

    def to_dict(self) -> dict[str, Any]:
        return _to_wire_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InjectionRecord:
        return cls(
            injection_id=str(data.get("injection_id", "")),
            session_id=str(data.get("session_id", "")),
            stream_id=str(data.get("stream_id", "")),
            target_bot_id=str(data.get("target_bot_id", "")),
            directive_type=str(data.get("directive_type", "")),
            content=str(data.get("content", "")),
            source_scene_snapshot=dict(data.get("source_scene_snapshot") or {}),
            created_at=float(data.get("created_at") or now_ts()),
            expires_at=float(data.get("expires_at", 0.0)),
            applied_at=float(data.get("applied_at", 0.0)),
            status=str(data.get("status", InjectionStatus.PENDING.value)),
        )


def _to_wire_dict(value: Any) -> dict[str, Any]:
    raw = asdict(value) if is_dataclass(value) else dict(value)
    return _convert_enums(raw)


def _convert_enums(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [_convert_enums(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _convert_enums(item) for key, item in value.items()}
    return value
