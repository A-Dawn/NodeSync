from __future__ import annotations

import unittest

from nodesync.adapters.maibot_10.plugin import NodeSync10Plugin
from nodesync.shared.models import InjectionRecord
from nodesync.shared.time_utils import now_ts


class _BridgeStub:
    def __init__(self, record: InjectionRecord | None) -> None:
        self.record = record

    def current_injection(self, _stream_id: str) -> InjectionRecord | None:
        return self.record


class _RuntimeStub:
    def get_active_injection(self, _stream_id: str) -> InjectionRecord | None:
        return None


class MaiBot10AdapterTest(unittest.TestCase):
    def test_declares_host_supported_hook_handler(self) -> None:
        plugin = NodeSync10Plugin()
        if not hasattr(plugin, "get_components"):
            self.skipTest("当前环境未安装 maibot_sdk，无法验证 SDK 组件收集")

        components = plugin.get_components()
        component_types = {item["type"] for item in components}
        hook_component = next(item for item in components if item["name"] == "nodesync_maisaka_before_request")

        self.assertIn("hook_handler", component_types)
        self.assertNotIn("workflow_step", component_types)
        self.assertEqual(hook_component["metadata"]["hook"], "maisaka.planner.before_request")
        self.assertEqual(hook_component["metadata"]["mode"], "blocking")
        self.assertEqual(hook_component["metadata"]["order"], "early")

    def test_before_request_injection_prepends_private_system_message(self) -> None:
        plugin = NodeSync10Plugin()
        record = InjectionRecord(
            injection_id="inj-1",
            session_id="stream-1",
            stream_id="stream-1",
            target_bot_id="bot-1",
            directive_type="align_context",
            content="把当前 RP 场景推进到选择行动。",
            source_scene_snapshot={"topic": "遗迹门口的抉择"},
            created_at=now_ts(),
            expires_at=now_ts() + 300,
            status="applied",
        )
        plugin._runtime = _RuntimeStub()  # type: ignore[assignment]
        plugin._bridge = _BridgeStub(record)  # type: ignore[assignment]

        messages = [{"role": "user", "content": "现在大家还在门口犹豫。"}]
        modified = plugin._inject_into_prompt_messages(messages, "stream-1")

        self.assertIsNotNone(modified)
        self.assertEqual(modified[0]["role"], "system")
        self.assertIn("NodeSync 私有上下文对齐", modified[0]["content"])
        self.assertIn("把当前 RP 场景推进到选择行动", modified[0]["content"])
        self.assertEqual(modified[1], messages[0])


if __name__ == "__main__":
    unittest.main()
