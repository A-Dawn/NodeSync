"""运行时配置对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class NodeSyncConfig:
    """server/client 模式共享的 NodeSync 运行配置。"""

    mode: str = "server"
    bot_id: str = "nodesync-default"
    bot_name: str = "NodeSync"
    reply_persona: str = ""
    maibot_version: str = "unknown"
    adapter_version: str = "nodesync-0.1.0"
    auth_token: str = "change-me"
    allow_query_token_auth: bool = False
    server_host: str = "127.0.0.1"
    server_port: int = 8765
    server_url: str = "http://127.0.0.1:8765"
    http_client_max_bytes: int = 1024 * 1024
    ws_max_msg_bytes: int = 1024 * 1024
    data_dir: Path = field(default_factory=lambda: Path("data") / "nodesync")
    prompts_dir: Path = field(default_factory=lambda: Path("data") / "nodesync" / "prompts")
    streams: list[str] = field(default_factory=list)
    diagnostics_enabled: bool = False
    diagnostics_dir: Path = field(default_factory=lambda: Path("data") / "nodesync" / "diagnostics")
    diagnostics_include_hashes: bool = True
    diagnostics_max_depth: int = 4
    diagnostics_max_items: int = 40
    llm_worker_bot_id: str = ""
    enable_llm_decision: bool = True
    enable_llm_advance_generation: bool = True
    llm_decision_timeout_seconds: float = 20.0
    llm_generation_timeout_seconds: float = 30.0
    context_limit: int = 40
    injection_history_limit: int = 5
    intervention_cooldown_seconds: int = 300
    directive_ttl_seconds: int = 900
    directive_retry_interval_seconds: int = 20
    directive_max_attempts: int = 3
    scene_idle_close_seconds: int = 3600
    analysis_min_messages: int = 4
    analysis_window_messages: int = 8

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> NodeSyncConfig:
        data = dict(raw or {})
        data_dir = data.get("data_dir", Path("data") / "nodesync")
        prompts_dir = data.get("prompts_dir") or Path(str(data_dir)) / "prompts"
        diagnostics_dir = data.get("diagnostics_dir") or Path(str(data_dir)) / "diagnostics"
        return cls(
            mode=str(data.get("mode", "server")),
            bot_id=str(data.get("bot_id", "nodesync-default")),
            bot_name=str(data.get("bot_name", "NodeSync")),
            reply_persona=str(data.get("reply_persona", "")),
            maibot_version=str(data.get("maibot_version", "unknown")),
            adapter_version=str(data.get("adapter_version", "nodesync-0.1.0")),
            auth_token=str(data.get("auth_token", "change-me")),
            allow_query_token_auth=_parse_bool(data.get("allow_query_token_auth", False)),
            server_host=str(data.get("server_host", data.get("host", "127.0.0.1"))),
            server_port=int(data.get("server_port", data.get("port", 8765))),
            server_url=str(data.get("server_url", "http://127.0.0.1:8765")),
            http_client_max_bytes=int(data.get("http_client_max_bytes", 1024 * 1024)),
            ws_max_msg_bytes=int(data.get("ws_max_msg_bytes", 1024 * 1024)),
            data_dir=Path(str(data_dir)),
            prompts_dir=Path(str(prompts_dir)),
            streams=_parse_streams(data.get("streams", [])),
            diagnostics_enabled=_parse_bool(data.get("diagnostics_enabled", False)),
            diagnostics_dir=Path(str(diagnostics_dir)),
            diagnostics_include_hashes=_parse_bool(data.get("diagnostics_include_hashes", True)),
            diagnostics_max_depth=int(data.get("diagnostics_max_depth", 4)),
            diagnostics_max_items=int(data.get("diagnostics_max_items", 40)),
            llm_worker_bot_id=str(data.get("llm_worker_bot_id", "")),
            enable_llm_decision=_parse_bool(data.get("enable_llm_decision", True)),
            enable_llm_advance_generation=_parse_bool(data.get("enable_llm_advance_generation", True)),
            llm_decision_timeout_seconds=float(data.get("llm_decision_timeout_seconds", 20.0)),
            llm_generation_timeout_seconds=float(data.get("llm_generation_timeout_seconds", 30.0)),
            context_limit=int(data.get("context_limit", 40)),
            injection_history_limit=int(data.get("injection_history_limit", 5)),
            intervention_cooldown_seconds=int(data.get("intervention_cooldown_seconds", 300)),
            directive_ttl_seconds=int(data.get("directive_ttl_seconds", 900)),
            directive_retry_interval_seconds=int(data.get("directive_retry_interval_seconds", 20)),
            directive_max_attempts=int(data.get("directive_max_attempts", 3)),
            scene_idle_close_seconds=int(data.get("scene_idle_close_seconds", 3600)),
            analysis_min_messages=int(data.get("analysis_min_messages", 4)),
            analysis_window_messages=int(data.get("analysis_window_messages", 8)),
        )

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "nodesync.sqlite3"

    @property
    def injection_path(self) -> Path:
        return self.data_dir / "context_injections.jsonl"

    @property
    def directive_result_path(self) -> Path:
        return self.data_dir / "directive_results.jsonl"

    @property
    def ws_url(self) -> str:
        base = self.server_url.rstrip("/")
        if base.startswith("https://"):
            return "wss://" + base[len("https://") :] + "/ws"
        if base.startswith("http://"):
            return "ws://" + base[len("http://") :] + "/ws"
        return base + "/ws"


def _parse_streams(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list | tuple | set):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "启用", "是"}
    return bool(value)
