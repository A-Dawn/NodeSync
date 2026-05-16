"""MaiBot 0.12.2 NodeSync 插件入口。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
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

try:  # pragma: no cover - 只在 MaiBot 0.12.2 运行时存在
    from src.plugin_system.apis import llm_api, message_api, send_api
    from src.plugin_system.apis.plugin_register_api import register_plugin
    from src.plugin_system.base.base_events_handler import BaseEventHandler
    from src.plugin_system.base.base_plugin import BasePlugin
    from src.plugin_system.base.component_types import ComponentInfo, EventType, MaiMessages
    from src.plugin_system.base.config_types import ConfigField
except Exception:  # pragma: no cover - 让本仓库可独立 compile/test
    llm_api = None
    message_api = None
    send_api = None
    ComponentInfo = Any
    MaiMessages = Any

    def register_plugin(cls: type[Any]) -> type[Any]:
        return cls

    class EventType:
        ON_START = "on_start"
        ON_STOP = "on_stop"
        ON_MESSAGE = "on_message"
        POST_LLM = "post_llm"

    class ConfigField:
        def __init__(self, **_kwargs: Any) -> None:
            pass

    class BaseEventHandler:
        event_type: str = ""
        handler_name: str = ""
        handler_description: str = ""
        intercept_message: bool = False
        weight: int = 0
        plugin_config: dict[str, Any] | None = None

        @classmethod
        def get_handler_info(cls) -> Any:
            return None

        def get_config(self, key: str, default: Any = None) -> Any:
            current: Any = self.plugin_config or {}
            for part in key.split("."):
                if not isinstance(current, dict) or part not in current:
                    return default
                current = current[part]
            return current

    class BasePlugin:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.config: dict[str, Any] = {}

        def get_config(self, key: str, default: Any = None) -> Any:
            current: Any = self.config
            for part in key.split("."):
                if not isinstance(current, dict) or part not in current:
                    return default
                current = current[part]
            return current


_runtime: NodeSyncRuntimeManager | None = None
_bridge: NodeSync012Bridge | None = None
_config: NodeSyncConfig | None = None
_local_flow_hub: LocalFlowHub | None = None
_local_flow_server: LocalFlowInputServer | None = None

logger = get_logger("adapters.maibot_012")
PLUGIN_DIR = Path(__file__).resolve().parent


class NodeSync012Bridge(NodeSyncBridge):
    """把 MaiBot 0.12.2 API 适配成 NodeSync client 能力。"""

    def __init__(self, config: NodeSyncConfig, local_flow_hub: LocalFlowHub | None = None):
        self.config = config
        self.local_flow_hub = local_flow_hub
        self.active_injections: dict[str, InjectionRecord] = {}

    def get_streams(self) -> list[str]:
        # 配置为空时注册为通配，表示此 client 可处理任意 stream。
        return list(self.config.streams)

    async def fetch_context(self, request: ContextRequest) -> ContextResponse:
        events = []
        if message_api is not None:
            try:
                raw_messages = await asyncio.to_thread(
                    message_api.get_recent_messages,
                    chat_id=request.stream_id,
                    limit=request.limit,
                    filter_mai=False,
                )
                events = []
                for item in raw_messages:
                    event = object_to_chat_event(item, default_stream_id=request.stream_id, bot_id=self.config.bot_id)
                    record_message_diagnostic(self.config, "maibot_012.context_fetch", item, event)
                    events.append(event)
            except Exception as exc:
                logger.warning("[NodeSync012] 读取 MaiBot 上下文失败，继续使用本地构造流: %s", exc)
        local_events = self.local_flow_hub.get_recent(request.stream_id, request.limit) if self.local_flow_hub else []
        return ContextResponse(
            session_id=request.session_id,
            stream_id=request.stream_id,
            messages=merge_chat_events(events, local_events, request.limit),
        )

    async def generate_llm(self, request: LLMRequest) -> LLMResponse:
        if llm_api is None:
            return LLMResponse(request_id=request.request_id, ok=False, error="MaiBot 0.12 llm_api 不可用")
        models = llm_api.get_available_models()
        model_config = models.get(request.model_name) if request.model_name else next(iter(models.values()), None)
        if model_config is None:
            return LLMResponse(request_id=request.request_id, ok=False, error="没有可用 LLM 模型")
        ok, response, _reasoning, _model_name = await llm_api.generate_with_model(
            prompt=request.prompt,
            model_config=model_config,
            request_type="nodesync.generate",
            temperature=request.temperature,
            max_tokens=request.max_tokens,
        )
        return LLMResponse(request_id=request.request_id, ok=bool(ok), content=str(response))

    async def apply_context_injection(self, directive: Directive, record: InjectionRecord) -> bool:
        self.active_injections[directive.stream_id] = record
        return True

    async def send_message(self, directive: Directive) -> bool:
        if self.local_flow_hub is not None and self.local_flow_hub.config.capture_outbound:
            return await self.local_flow_hub.capture_outbound(directive)
        if send_api is None:
            return False
        return bool(
            await send_api.text_to_stream(
                text=directive.content,
                stream_id=directive.stream_id,
                typing=True,
                storage_message=True,
            )
        )

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


class NodeSync012StartHandler(BaseEventHandler):
    """启动 NodeSync 运行时。"""

    event_type = EventType.ON_START
    handler_name = "nodesync_start"
    handler_description = "启动 NodeSync server/client 运行时"
    intercept_message = False

    async def execute(self, _message: MaiMessages | None):
        global _bridge, _config, _local_flow_hub, _local_flow_server, _runtime
        _config = _build_config(self.plugin_config or {})
        if _config.mode == "disabled":
            return True, True, "NodeSync 已禁用", None, None
        local_flow_config = _build_local_flow_config(self.plugin_config or {}, _config)
        _local_flow_hub = (
            LocalFlowHub(local_flow_config, bot_id=_config.bot_id, bot_name=_config.bot_name)
            if local_flow_config.enabled
            else None
        )
        _bridge = NodeSync012Bridge(_config, _local_flow_hub)
        _runtime = NodeSyncRuntimeManager(_config, _bridge)
        if _local_flow_hub is not None:
            _local_flow_hub.bind_runtime(_runtime)
            _local_flow_server = LocalFlowInputServer(local_flow_config, _local_flow_hub)
        try:
            await _runtime.start()
            if _local_flow_server is not None:
                await _local_flow_server.start()
        except Exception:
            await _runtime.stop()
            _runtime = None
            _bridge = None
            _local_flow_hub = None
            _local_flow_server = None
            raise
        return True, True, "NodeSync 已启动", None, None


class NodeSync012StopHandler(BaseEventHandler):
    """停止 NodeSync 运行时。"""

    event_type = EventType.ON_STOP
    handler_name = "nodesync_stop"
    handler_description = "停止 NodeSync 运行时"
    intercept_message = False

    async def execute(self, _message: MaiMessages | None):
        global _bridge, _config, _local_flow_hub, _local_flow_server, _runtime
        if _local_flow_server is not None:
            await _local_flow_server.stop()
        if _runtime is not None:
            await _runtime.stop()
        _runtime = None
        _bridge = None
        _config = None
        _local_flow_hub = None
        _local_flow_server = None
        return True, True, "NodeSync 已停止", None, None


class NodeSync012MessageHandler(BaseEventHandler):
    """采集群聊消息并上报服务端。"""

    event_type = EventType.ON_MESSAGE
    handler_name = "nodesync_message_report"
    handler_description = "上报群聊消息到 NodeSync server"
    intercept_message = False

    async def execute(self, message: MaiMessages | None):
        if _runtime is None or _config is None or message is None:
            return True, True, None, None, None
        event = object_to_chat_event(message, bot_id=_config.bot_id)
        record_message_diagnostic(_config, "maibot_012.on_message", message, event)
        await _runtime.report_message(event)
        return True, True, None, None, None


class NodeSync012PromptInjectionHandler(BaseEventHandler):
    """在 POST_LLM 阶段向 prompt 注入私有对齐内容。"""

    event_type = EventType.POST_LLM
    handler_name = "nodesync_prompt_injection"
    handler_description = "应用 NodeSync 私有上下文注入"
    intercept_message = True
    weight = 20

    async def execute(self, message: MaiMessages | None):
        if _runtime is None or _bridge is None or message is None:
            return True, True, None, None, None
        stream_id = str(getattr(message, "stream_id", "") or "")
        if not stream_id:
            return True, True, None, None, None
        record = _bridge.current_injection(stream_id) or _runtime.get_active_injection(stream_id)
        if record is None:
            return True, True, None, None, None
        prompt = build_alignment_prompt(str(getattr(message, "llm_prompt", "") or ""), record)
        message.modify_llm_prompt(prompt, suppress_warning=True)
        return True, True, "NodeSync 已注入上下文", None, message


@register_plugin
class NodeSync012Plugin(BasePlugin):
    """MaiBot 0.12.2 插件声明。"""

    plugin_name = "nodesync"
    enable_plugin = True
    dependencies: list[str] = []
    python_dependencies: list[Any] = []
    config_file_name = "config.toml"

    config_section_descriptions = {
        "plugin": "插件基本信息",
        "nodesync": "NodeSync 运行配置",
        "policy": "推进策略配置",
        "prompts": "提示词文件配置",
        "diagnostics": "受控诊断配置",
        "local_flow": "本地构造消息流调试配置",
    }

    config_schema: dict[str, Any] = {
        "plugin": {
            "name": ConfigField(type=str, default="nodesync", description="插件名称", required=True),
            "version": ConfigField(type=str, default="0.1.0", description="插件版本"),
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
            "description": ConfigField(type=str, default="多 Bot 协作与按需推进插件", description="插件描述"),
        },
        "nodesync": {
            "mode": ConfigField(type=str, default="server", description="server/client/disabled"),
            "bot_id": ConfigField(type=str, default="maibot-012", description="当前 bot 的 NodeSync ID"),
            "bot_name": ConfigField(type=str, default="MaiBot 0.12", description="当前 bot 显示名"),
            "reply_persona": ConfigField(type=str, default="", description="推进发言时注入的人设/说话方式"),
            "auth_token": ConfigField(type=str, default="change-me", description="服务端鉴权 token"),
            "allow_query_token_auth": ConfigField(type=bool, default=False, description="是否允许 URL query token 鉴权"),
            "server_host": ConfigField(type=str, default="127.0.0.1", description="server 监听地址"),
            "server_port": ConfigField(type=int, default=8765, description="server 监听端口"),
            "server_url": ConfigField(type=str, default="http://127.0.0.1:8765", description="client 连接地址"),
            "http_client_max_bytes": ConfigField(type=int, default=1048576, description="HTTP 请求体最大字节数"),
            "ws_max_msg_bytes": ConfigField(type=int, default=1048576, description="WebSocket 单消息最大字节数"),
            "data_dir": ConfigField(type=str, default="data/nodesync", description="NodeSync 数据目录"),
            "prompts_dir": ConfigField(type=str, default="", description="兼容旧配置：请优先使用 prompts.directory"),
            "streams": ConfigField(type=list, default=[], description="限定处理的 stream_id 列表；空列表表示全部"),
        },
        "prompts": {
            "directory": ConfigField(type=str, default="prompts", description="用户可编辑 prompt 模板目录；相对路径按插件目录解析"),
        },
        "diagnostics": {
            "enabled": ConfigField(type=bool, default=False, description="是否启用脱敏消息字段诊断"),
            "output_dir": ConfigField(type=str, default="", description="诊断输出目录；留空使用 data_dir/diagnostics"),
            "include_hashes": ConfigField(type=bool, default=True, description="是否记录脱敏值 hash，便于比对但不暴露原文"),
            "max_depth": ConfigField(type=int, default=4, description="诊断字段展开深度"),
            "max_items": ConfigField(type=int, default=40, description="每层最多记录字段或列表项数量"),
        },
        "policy": {
            "enable_llm_decision": ConfigField(type=bool, default=True, description="是否启用 LLM 主判定"),
            "enable_llm_advance_generation": ConfigField(type=bool, default=True, description="是否启用 LLM 生成推进消息"),
            "context_limit": ConfigField(type=int, default=40, description="分析上下文消息条数"),
            "injection_history_limit": ConfigField(type=int, default=5, description="注入历史返回条数"),
            "llm_worker_bot_id": ConfigField(type=str, default="", description="指定负责 LLM 委托的 bot_id；留空自动选择"),
            "llm_decision_timeout_seconds": ConfigField(type=int, default=20, description="LLM 判定超时秒数"),
            "llm_generation_timeout_seconds": ConfigField(type=int, default=30, description="LLM 推进生成超时秒数"),
            "intervention_cooldown_seconds": ConfigField(type=int, default=300, description="介入冷却秒数"),
            "directive_ttl_seconds": ConfigField(type=int, default=900, description="指令有效期秒数"),
            "directive_retry_interval_seconds": ConfigField(type=int, default=20, description="指令重试间隔秒数"),
            "directive_max_attempts": ConfigField(type=int, default=3, description="指令最大尝试次数"),
            "scene_idle_close_seconds": ConfigField(type=int, default=3600, description="场景空闲自动关闭秒数"),
            "analysis_min_messages": ConfigField(type=int, default=4, description="触发分析的最小消息数"),
            "analysis_window_messages": ConfigField(type=int, default=8, description="自动分析窗口消息数"),
        },
        "local_flow": {
            "enabled": ConfigField(type=bool, default=False, description="是否启用本地构造消息流入口"),
            "host": ConfigField(type=str, default="127.0.0.1", description="本地构造消息流监听地址"),
            "port": ConfigField(type=int, default=8788, description="本地构造消息流监听端口"),
            "auth_token": ConfigField(type=str, default="", description="本地入口 token；留空复用 nodesync.auth_token"),
            "stream_id": ConfigField(type=str, default="local-flow", description="默认构造 stream_id"),
            "capture_outbound": ConfigField(type=bool, default=True, description="是否捕获推进消息而不外发真实平台"),
            "context_limit": ConfigField(type=int, default=80, description="本地上下文返回条数"),
            "max_messages": ConfigField(type=int, default=500, description="本地最多缓存消息数"),
        },
    }

    def get_plugin_components(self):
        return [
            (NodeSync012StartHandler.get_handler_info(), NodeSync012StartHandler),
            (NodeSync012StopHandler.get_handler_info(), NodeSync012StopHandler),
            (NodeSync012MessageHandler.get_handler_info(), NodeSync012MessageHandler),
            (NodeSync012PromptInjectionHandler.get_handler_info(), NodeSync012PromptInjectionHandler),
        ]


def _build_config(plugin_config: dict[str, Any]) -> NodeSyncConfig:
    nodesync = dict(plugin_config.get("nodesync") or {})
    policy = dict(plugin_config.get("policy") or {})
    prompts = dict(plugin_config.get("prompts") or {})
    diagnostics = dict(plugin_config.get("diagnostics") or {})
    merged = {**nodesync, **policy}
    prompt_value = str(prompts.get("directory") or "")
    if not prompt_value and str(nodesync.get("prompts_dir", "")).strip():
        prompt_value = str(nodesync.get("prompts_dir", ""))
    merged["prompts_dir"] = _resolve_plugin_path(prompt_value, "prompts")
    if diagnostics:
        merged.update(
            {
                "diagnostics_enabled": diagnostics.get("enabled", False),
                "diagnostics_dir": diagnostics.get("output_dir", ""),
                "diagnostics_include_hashes": diagnostics.get("include_hashes", True),
                "diagnostics_max_depth": diagnostics.get("max_depth", 4),
                "diagnostics_max_items": diagnostics.get("max_items", 40),
            }
        )
    if not str(merged.get("reply_persona", "")).strip():
        merged["reply_persona"] = _host_reply_persona()
    if plugin_config.get("plugin", {}).get("enabled") is False:
        merged["mode"] = "disabled"
    data_dir = Path(str(merged.get("data_dir", "data/nodesync")))
    merged["data_dir"] = data_dir
    merged["maibot_version"] = "0.12.2"
    merged["adapter_version"] = "nodesync-maibot-012-0.1.0"
    return NodeSyncConfig.from_mapping(merged)


def _resolve_plugin_path(raw_value: str, default_name: str) -> Path:
    """把相对路径解析到当前插件入口目录。"""

    raw = str(raw_value or "").strip() or default_name
    path = Path(raw)
    return path if path.is_absolute() else PLUGIN_DIR / path


def _host_reply_persona() -> str:
    """尽量从 MaiBot 宿主配置读取人设。"""

    try:
        from src.config.config import global_config  # type: ignore[import-not-found]

        return str(getattr(getattr(global_config, "personality", None), "personality", "") or "")
    except Exception:
        return ""


def _build_local_flow_config(plugin_config: dict[str, Any], runtime_config: NodeSyncConfig) -> LocalFlowConfig:
    """读取本地构造消息流配置。"""

    return LocalFlowConfig.from_mapping(
        dict(plugin_config.get("local_flow") or {}),
        default_auth_token=runtime_config.auth_token,
    )
