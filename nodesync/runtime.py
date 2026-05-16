"""NodeSync 通用运行时编排。"""

from __future__ import annotations

from nodesync.client import NodeSyncBridge, NodeSyncClientRuntime
from nodesync.core.runtime_config import NodeSyncConfig
from nodesync.server import NodeSyncCoordinator
from nodesync.shared.models import ChatMessageEvent, InjectionRecord
from nodesync.shared.prompt_templates import configure_prompt_templates


class NodeSyncRuntimeManager:
    """按配置启动 server/client/disabled 三种模式。"""

    def __init__(self, config: NodeSyncConfig, bridge: NodeSyncBridge):
        self.config = config
        self.bridge = bridge
        self.server: NodeSyncCoordinator | None = None
        self.client: NodeSyncClientRuntime | None = None

    async def start(self) -> None:
        """启动当前模式需要的运行时。"""

        if self.config.mode == "disabled":
            return
        configure_prompt_templates(self.config.prompts_dir)
        if self.config.mode == "server" and self.server is None:
            self.server = NodeSyncCoordinator(self.config)
            await self.server.start()
        if self.client is None:
            self.client = NodeSyncClientRuntime(self.config, self.bridge)
            await self.client.start()

    async def stop(self) -> None:
        """停止所有已启动的运行时。"""

        if self.client is not None:
            await self.client.stop()
            self.client = None
        if self.server is not None:
            await self.server.stop()
            self.server = None

    async def report_message(self, event: ChatMessageEvent) -> bool:
        """向 NodeSync server 上报消息。"""

        if self.client is None:
            return False
        return await self.client.report_message(event)

    def get_active_injection(self, stream_id: str, session_id: str = "") -> InjectionRecord | None:
        """读取当前可用的上下文注入。"""

        if self.client is None:
            return None
        return self.client.get_active_injection(stream_id, session_id)
