"""MaiBot 1.0 SDK NodeSync 插件入口。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from nodesync.adapters.common import build_alignment_prompt, object_to_chat_event, record_message_diagnostic
from nodesync.adapters.local_flow import LocalFlowConfig, LocalFlowHub, LocalFlowInputServer, merge_chat_events
from nodesync.client import NodeSyncBridge
from nodesync.core.runtime_config import NodeSyncConfig
from nodesync.runtime import NodeSyncRuntimeManager
from nodesync.shared.logging_utils import get_logger
from nodesync.shared.models import (
    ChatMessageEvent,
    ContextRequest,
    ContextResponse,
    Directive,
    InjectionRecord,
    LLMRequest,
    LLMResponse,
)
from nodesync.shared.texts import build_client_advance_reply_prompt
from nodesync.shared.time_utils import now_ts

try:  # pragma: no cover - 目标 MaiBot 1.0 运行时提供
    from maibot_sdk import EventHandler, MaiBotPlugin
    from maibot_sdk.types import EventType

    try:
        from maibot_sdk import Field, PluginConfigBase
    except Exception:
        from pydantic import BaseModel as PluginConfigBase
        from pydantic import Field
except Exception:  # pragma: no cover - 让本仓库可独立 compile/test
    from pydantic import BaseModel as PluginConfigBase
    from pydantic import Field

    class MaiBotPlugin:
        def __init__(self) -> None:
            self.config = None

    def EventHandler(*_args: Any, **_kwargs: Any):
        def decorator(func: Any) -> Any:
            return func

        return decorator

    class EventType:
        ON_MESSAGE = "on_message"
        POST_LLM = "post_llm"


_COMPONENT_INFO_ATTR = "__maibot_component_info__"

logger = get_logger("adapters.maibot_10")
PLUGIN_DIR = Path(__file__).resolve().parent


@dataclass(slots=True)
class _HookComponentType:
    value: str = "hook_handler"


@dataclass(slots=True)
class _HookHandlerInfo:
    """适配当前 1.0 宿主支持的 hook_handler 组件声明。"""

    name: str
    hook: str
    description: str = ""
    mode: str = "blocking"
    order: str = "normal"
    timeout_ms: int = 1000
    error_policy: str = "skip"
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    type: _HookComponentType = field(default_factory=_HookComponentType)

    def model_dump(self, exclude: set[str] | None = None, **_kwargs: Any) -> dict[str, Any]:
        """模拟 SDK 组件元数据对象的 model_dump 接口。"""

        payload = {
            "name": self.name,
            "type": self.type,
            "description": self.description,
            "enabled": self.enabled,
            "metadata": self.metadata,
            "hook": self.hook,
            "mode": self.mode,
            "order": self.order,
            "timeout_ms": self.timeout_ms,
            "error_policy": self.error_policy,
        }
        for key in exclude or set():
            payload.pop(key, None)
        return payload


def HookHandler(
    name: str,
    hook: str,
    description: str = "",
    mode: str = "blocking",
    order: str = "normal",
    timeout_ms: int = 1000,
    error_policy: str = "skip",
    **metadata: Any,
):
    """声明宿主支持的命名 Hook 处理器。

    当前安装的 maibot_sdk 尚未提供 HookHandler 装饰器，但 SDK 的
    collect_components 会读取 __maibot_component_info__。这里仅声明宿主
    已暴露的 hook_handler 组件元数据，不绕过宿主校验。
    """

    def decorator(func: Any) -> Any:
        setattr(
            func,
            _COMPONENT_INFO_ATTR,
            _HookHandlerInfo(
                name=name,
                hook=hook,
                description=description,
                mode=mode,
                order=order,
                timeout_ms=timeout_ms,
                error_policy=error_policy,
                metadata=metadata,
            ),
        )
        return func

    return decorator


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="0.1.0", description="配置版本")


class NodeSyncSectionConfig(PluginConfigBase):
    """NodeSync 连接与身份配置。"""

    __ui_label__ = "NodeSync"
    __ui_icon__ = "network"
    __ui_order__ = 1

    mode: Literal["server", "client", "disabled"] = Field(
        default="server",
        description="server/client/disabled",
        json_schema_extra={"x-widget": "select", "x-icon": "network"},
    )
    bot_id: str = Field(default="maibot-10", description="当前 bot 的 NodeSync ID", json_schema_extra={"x-icon": "fingerprint"})
    bot_name: str = Field(default="MaiBot 1.0", description="当前 bot 显示名", json_schema_extra={"x-icon": "bot"})
    reply_persona: str = Field(
        default="",
        description="推进发言时注入的人设/说话方式",
        json_schema_extra={"x-widget": "textarea", "x-icon": "user-round"},
    )
    auth_token: str = Field(
        default="change-me",
        description="服务端鉴权 token",
        json_schema_extra={"x-widget": "password", "x-icon": "key-round"},
    )
    allow_query_token_auth: bool = Field(
        default=False,
        description="是否允许 URL query token 鉴权",
        json_schema_extra={"x-widget": "switch", "x-icon": "shield-alert", "advanced": True},
    )
    server_host: str = Field(default="127.0.0.1", description="server 监听地址", json_schema_extra={"x-icon": "server"})
    server_port: int = Field(default=8765, description="server 监听端口", json_schema_extra={"x-widget": "input", "x-icon": "hash"})
    server_url: str = Field(default="http://127.0.0.1:8765", description="client 连接地址", json_schema_extra={"x-icon": "link"})
    http_client_max_bytes: int = Field(
        default=1048576,
        description="HTTP 请求体最大字节数",
        json_schema_extra={"x-widget": "input", "x-icon": "file-json", "advanced": True},
    )
    ws_max_msg_bytes: int = Field(
        default=1048576,
        description="WebSocket 单消息最大字节数",
        json_schema_extra={"x-widget": "input", "x-icon": "file-json", "advanced": True},
    )
    data_dir: str = Field(default="data/nodesync", description="NodeSync 数据目录", json_schema_extra={"x-icon": "database"})
    prompts_dir: str = Field(
        default="",
        description="兼容旧配置：请优先使用“提示词.directory”。留空时忽略此字段。",
        json_schema_extra={"x-widget": "input", "x-icon": "file-text", "advanced": True},
    )
    streams: list[str] = Field(default_factory=list, description="限定处理的 stream_id 列表；空列表表示全部")


class PolicyConfig(PluginConfigBase):
    """推进策略配置。"""

    __ui_label__ = "策略"
    __ui_icon__ = "settings"
    __ui_order__ = 2

    context_limit: int = Field(default=40, description="分析上下文消息条数")
    injection_history_limit: int = Field(default=5, description="注入历史返回条数")
    enable_llm_decision: bool = Field(default=True, description="是否启用 LLM 主判定")
    enable_llm_advance_generation: bool = Field(default=True, description="是否启用 LLM 生成推进消息")
    llm_worker_bot_id: str = Field(default="", description="指定负责 LLM 委托的 bot_id；留空自动选择")
    llm_decision_timeout_seconds: int = Field(default=20, description="LLM 判定超时秒数")
    llm_generation_timeout_seconds: int = Field(default=30, description="LLM 推进生成超时秒数")
    intervention_cooldown_seconds: int = Field(default=300, description="介入冷却秒数")
    directive_ttl_seconds: int = Field(default=900, description="指令有效期秒数")
    directive_retry_interval_seconds: int = Field(default=20, description="指令重试间隔秒数")
    directive_max_attempts: int = Field(default=3, description="指令最大尝试次数")
    scene_idle_close_seconds: int = Field(default=3600, description="场景空闲自动关闭秒数")
    analysis_min_messages: int = Field(default=4, description="触发分析的最小消息数")
    analysis_window_messages: int = Field(default=8, description="自动分析窗口消息数")


class PromptSectionConfig(PluginConfigBase):
    """提示词文件配置。"""

    __ui_label__ = "提示词"
    __ui_icon__ = "file-text"
    __ui_order__ = 3

    directory: str = Field(
        default="prompts",
        description="用户可编辑 prompt 模板目录；相对路径按插件目录解析",
        json_schema_extra={"x-widget": "input", "x-icon": "folder-edit"},
    )


class LocalFlowSectionConfig(PluginConfigBase):
    """本地构造消息流调试配置。"""

    __ui_label__ = "本地消息流"
    __ui_icon__ = "radio"
    __ui_order__ = 4

    enabled: bool = Field(default=False, description="是否启用本地构造消息流入口")
    host: str = Field(default="127.0.0.1", description="本地构造消息流监听地址")
    port: int = Field(default=8788, description="本地构造消息流监听端口")
    auth_token: str = Field(default="", description="本地入口 token；留空复用 nodesync.auth_token")
    stream_id: str = Field(default="local-flow", description="默认构造 stream_id")
    capture_outbound: bool = Field(default=True, description="是否捕获推进消息而不外发真实平台")
    context_limit: int = Field(default=80, description="本地上下文返回条数")
    max_messages: int = Field(default=500, description="本地最多缓存消息数")


class DiagnosticsSectionConfig(PluginConfigBase):
    """受控诊断配置。"""

    __ui_label__ = "诊断"
    __ui_icon__ = "activity"
    __ui_order__ = 5

    enabled: bool = Field(
        default=False,
        description="是否启用脱敏消息字段诊断",
        json_schema_extra={"x-widget": "switch", "x-icon": "activity"},
    )
    output_dir: str = Field(
        default="",
        description="诊断输出目录；留空使用 data_dir/diagnostics",
        json_schema_extra={"x-widget": "input", "x-icon": "folder-search"},
    )
    include_hashes: bool = Field(
        default=True,
        description="是否记录脱敏值 hash，便于比对但不暴露原文",
        json_schema_extra={"x-widget": "switch", "x-icon": "hash"},
    )
    max_depth: int = Field(
        default=4,
        description="诊断字段展开深度",
        json_schema_extra={"x-widget": "input", "x-icon": "layers", "advanced": True},
    )
    max_items: int = Field(
        default=40,
        description="每层最多记录字段或列表项数量",
        json_schema_extra={"x-widget": "input", "x-icon": "list", "advanced": True},
    )


class NodeSync10PluginConfig(PluginConfigBase):
    """NodeSync 1.0 插件配置模型。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    nodesync: NodeSyncSectionConfig = Field(default_factory=NodeSyncSectionConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    prompts: PromptSectionConfig = Field(default_factory=PromptSectionConfig)
    local_flow: LocalFlowSectionConfig = Field(default_factory=LocalFlowSectionConfig)
    diagnostics: DiagnosticsSectionConfig = Field(default_factory=DiagnosticsSectionConfig)


class NodeSync10Bridge(NodeSyncBridge):
    """把 MaiBot 1.0 SDK 能力适配成 NodeSync client 能力。"""

    def __init__(
        self,
        plugin: NodeSync10Plugin,
        config: NodeSyncConfig,
        local_flow_hub: LocalFlowHub | None = None,
    ):
        self.plugin = plugin
        self.config = config
        self.local_flow_hub = local_flow_hub
        self.active_injections: dict[str, InjectionRecord] = {}

    def get_streams(self) -> list[str]:
        # 配置为空时注册为通配，表示此 client 可处理任意 stream。
        return list(self.config.streams)

    async def fetch_context(self, request: ContextRequest) -> ContextResponse:
        events = []
        try:
            messages = await self.plugin.ctx.message.get_recent(request.stream_id, limit=request.limit)
            events = []
            for item in messages or []:
                event = object_to_chat_event(item, default_stream_id=request.stream_id, bot_id=self.config.bot_id)
                record_message_diagnostic(self.config, "maibot_10.context_fetch", item, event)
                events.append(event)
        except Exception as exc:
            logger.warning("[NodeSync10] 读取 MaiBot 上下文失败，继续使用本地构造流: %s", exc)
        local_events = self.local_flow_hub.get_recent(request.stream_id, request.limit) if self.local_flow_hub else []
        return ContextResponse(
            session_id=request.session_id,
            stream_id=request.stream_id,
            messages=merge_chat_events(events, local_events, request.limit),
        )

    async def generate_llm(self, request: LLMRequest) -> LLMResponse:
        result = await self.plugin.ctx.llm.generate(
            prompt=request.prompt,
            model=request.model_name,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
        )
        return LLMResponse(
            request_id=request.request_id,
            ok=bool(result.get("success", False)),
            content=str(result.get("response", "")),
            error=str(result.get("error", "")),
        )

    async def apply_context_injection(self, directive: Directive, record: InjectionRecord) -> bool:
        self.active_injections[directive.stream_id] = record
        return True

    async def send_message(self, directive: Directive) -> bool:
        if self.local_flow_hub is not None and self.local_flow_hub.config.capture_outbound:
            return await self.local_flow_hub.capture_outbound(directive)
        result = await self.plugin.ctx.send.text(directive.content, directive.stream_id)
        return bool(result is None or result)

    async def render_advance_message(self, directive: Directive) -> str:
        """用当前 bot 的 LLM 能力把推进意图改写成群内最终发言。"""

        messages = await self._recent_events(directive.stream_id, limit=20)
        prompt = build_client_advance_reply_prompt(
            bot_name=self.config.bot_name,
            reply_persona=self.config.reply_persona,
            directive_content=directive.content,
            scene_snapshot=directive.source_scene_snapshot,
            messages=messages,
        )
        response = await self.generate_llm(
            LLMRequest(
                request_id=uuid4().hex,
                prompt=prompt,
                temperature=0.55,
                max_tokens=220,
            )
        )
        return response.content if response.ok else ""

    def current_injection(self, stream_id: str) -> InjectionRecord | None:
        record = self.active_injections.get(stream_id)
        if record and record.expires_at and record.expires_at < now_ts():
            self.active_injections.pop(stream_id, None)
            return None
        return record

    async def _recent_events(self, stream_id: str, limit: int) -> list[ChatMessageEvent]:
        response = await self.fetch_context(
            ContextRequest(
                session_id="",
                stream_id=stream_id,
                limit=limit,
                include_injection_history=False,
            )
        )
        events = []
        for raw in response.messages:
            try:
                events.append(ChatMessageEvent.from_dict(raw))
            except Exception:
                continue
        return events


class NodeSync10Plugin(MaiBotPlugin):
    """MaiBot 1.0 NodeSync 插件。"""

    config_model = NodeSync10PluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._runtime: NodeSyncRuntimeManager | None = None
        self._bridge: NodeSync10Bridge | None = None
        self._config: NodeSyncConfig | None = None
        self._local_flow_hub: LocalFlowHub | None = None
        self._local_flow_server: LocalFlowInputServer | None = None

    async def on_load(self) -> None:
        """插件加载时启动运行时。"""

        await self._restart_runtime()

    async def on_unload(self) -> None:
        """插件卸载时停止运行时。"""

        await self._stop_runtime()

    async def on_config_update(self, _scope: str, _config_data: dict[str, object], _version: str) -> None:
        """配置热更新时重启运行时。"""

        await self._restart_runtime()

    @EventHandler("nodesync_message_report", description="上报群聊消息到 NodeSync", event_type=EventType.ON_MESSAGE)
    async def handle_message(self, message: Any = None, stream_id: str = "", **_kwargs: Any):
        """采集消息并上报服务端。"""

        if self._runtime is None or self._config is None or message is None:
            return {"continue_processing": True}
        event = object_to_chat_event(message, default_stream_id=stream_id, bot_id=self._config.bot_id)
        record_message_diagnostic(self._config, "maibot_10.on_message", message, event)
        await self._runtime.report_message(event)
        return {"continue_processing": True}

    @EventHandler(
        "nodesync_post_llm_injection",
        description="在 POST_LLM 阶段应用 NodeSync 私有上下文注入",
        event_type=EventType.POST_LLM,
        intercept_message=True,
        weight=20,
    )
    async def handle_post_llm(self, message: Any = None, stream_id: str = "", **_kwargs: Any):
        """使用 EventHandler POST_LLM 修改 prompt。"""

        modified = self._inject_into_message(message, stream_id)
        if modified is None:
            return {"continue_processing": True}
        return {"continue_processing": True, "modified_message": modified}

    @HookHandler(
        "nodesync_maisaka_before_request",
        hook="maisaka.planner.before_request",
        description="在 Maisaka 规划请求前注入 NodeSync 私有上下文",
        mode="blocking",
        order="early",
        timeout_ms=1000,
        error_policy="skip",
    )
    async def handle_maisaka_before_request(
        self,
        messages: list[Any] | None = None,
        tool_definitions: list[dict[str, Any]] | None = None,
        selected_history_count: int = 0,
        built_message_count: int = 0,
        selection_reason: str = "",
        session_id: str = "",
        **kwargs: Any,
    ):
        """使用宿主命名 Hook 修改 Maisaka planner 的 messages。"""

        modified_messages = self._inject_into_prompt_messages(messages or [], session_id)
        if modified_messages is None:
            return {"action": "continue"}
        # 宿主调用 Hook 时会额外传入 hook_name；返回 modified_kwargs 时只保留业务参数。
        passthrough_kwargs = {key: value for key, value in kwargs.items() if key != "hook_name"}
        modified_kwargs = {
            **passthrough_kwargs,
            "messages": modified_messages,
            "tool_definitions": tool_definitions or [],
            "selected_history_count": selected_history_count,
            "built_message_count": len(modified_messages),
            "selection_reason": selection_reason,
            "session_id": session_id,
        }
        return {"action": "continue", "modified_kwargs": modified_kwargs}

    async def _restart_runtime(self) -> None:
        await self._stop_runtime()
        config_model = _current_config(self)
        self._config = _build_config(config_model)
        if self._config.mode == "disabled":
            return
        local_flow_config = _build_local_flow_config(config_model, self._config)
        self._local_flow_hub = (
            LocalFlowHub(local_flow_config, bot_id=self._config.bot_id, bot_name=self._config.bot_name)
            if local_flow_config.enabled
            else None
        )
        self._bridge = NodeSync10Bridge(self, self._config, self._local_flow_hub)
        self._runtime = NodeSyncRuntimeManager(self._config, self._bridge)
        if self._local_flow_hub is not None:
            self._local_flow_hub.bind_runtime(self._runtime)
            self._local_flow_server = LocalFlowInputServer(local_flow_config, self._local_flow_hub)
        try:
            await self._runtime.start()
            if self._local_flow_server is not None:
                await self._local_flow_server.start()
        except Exception:
            await self._stop_runtime()
            raise

    async def _stop_runtime(self) -> None:
        if self._local_flow_server is not None:
            await self._local_flow_server.stop()
            self._local_flow_server = None
        if self._runtime is not None:
            await self._runtime.stop()
        self._runtime = None
        self._bridge = None
        self._config = None
        self._local_flow_hub = None

    def _inject_into_message(self, message: Any, stream_id: str) -> dict[str, Any] | None:
        if self._runtime is None or self._bridge is None or message is None:
            return None
        payload = dict(message) if isinstance(message, dict) else _model_dump(message)
        target_stream_id = str(payload.get("stream_id") or stream_id or "")
        if not target_stream_id:
            return None
        record = self._bridge.current_injection(target_stream_id) or self._runtime.get_active_injection(target_stream_id)
        if record is None:
            return None
        payload["llm_prompt"] = build_alignment_prompt(str(payload.get("llm_prompt") or ""), record)
        return payload

    def _inject_into_prompt_messages(
        self,
        messages: list[Any],
        session_id: str,
    ) -> list[Any] | None:
        if self._runtime is None or self._bridge is None:
            return None
        if not isinstance(messages, list):
            return None
        stream_id = str(session_id or "")
        if not stream_id:
            return None
        record = self._bridge.current_injection(stream_id) or self._runtime.get_active_injection(stream_id)
        if record is None:
            return None
        injected = [{"role": "system", "content": build_alignment_prompt("", record)}]
        injected.extend(dict(item) if isinstance(item, dict) else item for item in messages)
        return injected


def create_plugin() -> NodeSync10Plugin:
    """创建 MaiBot 1.0 插件实例。"""

    return NodeSync10Plugin()


def _current_config(plugin: NodeSync10Plugin) -> NodeSync10PluginConfig:
    config = getattr(plugin, "config", None)
    if config is None:
        return NodeSync10PluginConfig()
    if isinstance(config, NodeSync10PluginConfig):
        return config
    if isinstance(config, dict):
        return NodeSync10PluginConfig.model_validate(config)
    return config


def _build_config(config_model: NodeSync10PluginConfig) -> NodeSyncConfig:
    prompt_value = config_model.prompts.directory
    if config_model.nodesync.prompts_dir.strip() and prompt_value.strip() in {"", "prompts"}:
        prompt_value = config_model.nodesync.prompts_dir
    prompt_dir = _resolve_plugin_path(prompt_value, "prompts")
    merged = {
        "mode": config_model.nodesync.mode,
        "bot_id": config_model.nodesync.bot_id,
        "bot_name": config_model.nodesync.bot_name,
        "reply_persona": config_model.nodesync.reply_persona.strip() or _host_reply_persona(),
        "maibot_version": "1.0",
        "adapter_version": "nodesync-maibot-10-0.1.0",
        "auth_token": config_model.nodesync.auth_token,
        "allow_query_token_auth": config_model.nodesync.allow_query_token_auth,
        "server_host": config_model.nodesync.server_host,
        "server_port": config_model.nodesync.server_port,
        "server_url": config_model.nodesync.server_url,
        "http_client_max_bytes": config_model.nodesync.http_client_max_bytes,
        "ws_max_msg_bytes": config_model.nodesync.ws_max_msg_bytes,
        "data_dir": Path(config_model.nodesync.data_dir),
        "prompts_dir": prompt_dir,
        "streams": config_model.nodesync.streams,
        "diagnostics_enabled": config_model.diagnostics.enabled,
        "diagnostics_dir": config_model.diagnostics.output_dir,
        "diagnostics_include_hashes": config_model.diagnostics.include_hashes,
        "diagnostics_max_depth": config_model.diagnostics.max_depth,
        "diagnostics_max_items": config_model.diagnostics.max_items,
        "enable_llm_decision": config_model.policy.enable_llm_decision,
        "enable_llm_advance_generation": config_model.policy.enable_llm_advance_generation,
        "llm_worker_bot_id": config_model.policy.llm_worker_bot_id,
        "llm_decision_timeout_seconds": config_model.policy.llm_decision_timeout_seconds,
        "llm_generation_timeout_seconds": config_model.policy.llm_generation_timeout_seconds,
        "context_limit": config_model.policy.context_limit,
        "injection_history_limit": config_model.policy.injection_history_limit,
        "intervention_cooldown_seconds": config_model.policy.intervention_cooldown_seconds,
        "directive_ttl_seconds": config_model.policy.directive_ttl_seconds,
        "directive_retry_interval_seconds": config_model.policy.directive_retry_interval_seconds,
        "directive_max_attempts": config_model.policy.directive_max_attempts,
        "scene_idle_close_seconds": config_model.policy.scene_idle_close_seconds,
        "analysis_min_messages": config_model.policy.analysis_min_messages,
        "analysis_window_messages": config_model.policy.analysis_window_messages,
    }
    if not config_model.plugin.enabled:
        merged["mode"] = "disabled"
    return NodeSyncConfig.from_mapping(merged)


def _build_local_flow_config(config_model: NodeSync10PluginConfig, runtime_config: NodeSyncConfig) -> LocalFlowConfig:
    """读取本地构造消息流配置。"""

    return LocalFlowConfig.from_mapping(
        _model_dump(config_model.local_flow),
        default_auth_token=runtime_config.auth_token,
    )


def _host_reply_persona() -> str:
    """尽量从 MaiBot 宿主配置读取人设。"""

    try:
        from src.config.config import global_config  # type: ignore[import-not-found]

        return str(getattr(getattr(global_config, "personality", None), "personality", "") or "")
    except Exception:
        return ""


def _resolve_plugin_path(raw_value: str, default_name: str) -> Path:
    """把相对路径解析到当前插件入口目录。"""

    raw = str(raw_value or "").strip() or default_name
    path = Path(raw)
    return path if path.is_absolute() else PLUGIN_DIR / path


def _model_dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(mode="json"))
    if hasattr(value, "to_rpc_dict"):
        return dict(value.to_rpc_dict())
    return {}
