from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

REAL_HOST_TESTS_ENABLED = os.environ.get("NODESYNC_REAL_MAIBOT_TESTS") == "1"
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
NODESYNC_ROOT = WORKSPACE_ROOT / "NodeSync"
MAIBOT_012_ROOT = WORKSPACE_ROOT / "MaiBot-0.12.2"
MAIBOT_10_ROOT = WORKSPACE_ROOT / "MaiBot-1.0-latest"


@unittest.skipUnless(REAL_HOST_TESTS_ENABLED, "设置 NODESYNC_REAL_MAIBOT_TESTS=1 才执行真实 MaiBot 宿主烟测")
class RealMaiBotHostSmokeTest(unittest.TestCase):
    def test_maibot_012_plugin_starts_and_basic_features_work(self) -> None:
        self.assertTrue(MAIBOT_012_ROOT.exists(), f"缺少 MaiBot 0.12.2 源码目录: {MAIBOT_012_ROOT}")
        script = r"""
import asyncio
import importlib.util
import shutil
import socket
import sys
import tempfile
from pathlib import Path

node_root = Path(r"%NODESYNC_ROOT%")
host_root = Path(r"%MAIBOT_012_ROOT%")
sys.path.insert(0, str(node_root))
sys.path.insert(0, str(host_root))

from nodesync.shared.models import Directive, DirectiveType, InjectionRecord, InjectionStatus
from nodesync.shared.time_utils import now_ts
from src.plugin_system.base.base_events_handler import BaseEventHandler
from src.plugin_system.base.base_plugin import BasePlugin
from src.plugin_system.base.component_types import EventType, MaiMessages


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def wait_until(predicate, max_wait: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + max_wait
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("等待条件超时")


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        plugin_dir = tmp_path / "nodesync_012"
        shutil.copytree(node_root / "nodesync" / "adapters" / "maibot_012", plugin_dir)
        spec = importlib.util.spec_from_file_location("nodesync_real_maibot012", plugin_dir / "plugin.py")
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        assert issubclass(module.NodeSync012Plugin, BasePlugin)
        assert issubclass(module.NodeSync012StartHandler, BaseEventHandler)

        plugin = module.NodeSync012Plugin(plugin_dir=str(plugin_dir))
        components = plugin.get_plugin_components()
        names = {info.name for info, _handler in components}
        event_types = {info.name: info.event_type for info, _handler in components}
        assert names == {
            "nodesync_start",
            "nodesync_stop",
            "nodesync_message_report",
            "nodesync_prompt_injection",
        }
        assert event_types["nodesync_start"] == EventType.ON_START
        assert event_types["nodesync_stop"] == EventType.ON_STOP
        assert event_types["nodesync_message_report"] == EventType.ON_MESSAGE
        assert event_types["nodesync_prompt_injection"] == EventType.POST_LLM

        port = free_port()
        config = {
            "plugin": {"enabled": True},
            "nodesync": {
                "mode": "server",
                "bot_id": "real-012",
                "bot_name": "Real 0.12 Smoke",
                "auth_token": "real-test-token",
                "server_host": "127.0.0.1",
                "server_port": port,
                "server_url": f"http://127.0.0.1:{port}",
                "data_dir": str(tmp_path / "data"),
                "streams": ["group-real"],
            },
            "policy": {
                "enable_llm_decision": False,
                "enable_llm_advance_generation": False,
                "intervention_cooldown_seconds": 0,
                "analysis_min_messages": 8,
            },
        }

        start_handler = module.NodeSync012StartHandler()
        start_handler.plugin_config = config
        ok, _continue, _message, _custom, _modified = await start_handler.execute(None)
        assert ok
        try:
            assert module._runtime is not None
            await wait_until(lambda: module._runtime.server and "real-012" in module._runtime.server._clients)

            message_handler = module.NodeSync012MessageHandler()
            message = MaiMessages(
                plain_text="RP 场景：我们在门口。",
                stream_id="group-real",
                message_base_info={"user_info": {"user_id": "u1", "user_nickname": "测试用户"}},
            )
            ok, *_rest = await message_handler.execute(message)
            assert ok
            await wait_until(lambda: bool(module._runtime.server.storage.get_recent_messages("group-real", 5)))

            record = InjectionRecord(
                injection_id="real-injection-012",
                session_id="scene-real",
                stream_id="group-real",
                target_bot_id="real-012",
                directive_type=DirectiveType.ALIGN_CONTEXT.value,
                content="继续留在当前 RP 场景，把下一步行动说具体。",
                source_scene_snapshot={"topic": "门口停滞"},
                created_at=now_ts(),
                expires_at=now_ts() + 300,
                status=InjectionStatus.APPLIED.value,
            )
            directive = Directive(
                directive_id=record.injection_id,
                session_id=record.session_id,
                stream_id=record.stream_id,
                target_bot_id=record.target_bot_id,
                directive_type=record.directive_type,
                content=record.content,
                source_scene_snapshot=record.source_scene_snapshot,
                created_at=record.created_at,
                expires_at=record.expires_at,
            )
            await module._bridge.apply_context_injection(directive, record)

            prompt_handler = module.NodeSync012PromptInjectionHandler()
            prompt_message = MaiMessages(stream_id="group-real", llm_prompt="原始 prompt")
            ok, _continue, _message, _custom, modified = await prompt_handler.execute(prompt_message)
            assert ok
            assert modified is prompt_message
            assert "NodeSync 私有上下文对齐" in (prompt_message.llm_prompt or "")
        finally:
            stop_handler = module.NodeSync012StopHandler()
            await stop_handler.execute(None)

    print("MAIBOT_012_SMOKE_OK")


asyncio.run(main())
"""
        completed = _run_python_script(
            command=[sys.executable],
            script=_fill_paths(script),
            cwd=MAIBOT_012_ROOT,
            timeout_seconds=120,
        )
        self.assertIn("MAIBOT_012_SMOKE_OK", completed.stdout)

    def test_maibot_10_plugin_loader_startup_and_basic_features_work(self) -> None:
        self.assertTrue(MAIBOT_10_ROOT.exists(), f"缺少 MaiBot 1.0 源码目录: {MAIBOT_10_ROOT}")
        uv_path = shutil.which("uv")
        self.assertIsNotNone(uv_path, "缺少 uv，无法按 MaiBot 1.0 项目依赖环境执行真实宿主烟测")
        script = r"""
import asyncio
import shutil
import socket
import sys
import tempfile
from pathlib import Path

node_root = Path(r"%NODESYNC_ROOT%")
sys.path.insert(0, str(node_root))

from nodesync.shared.models import Directive, DirectiveType, InjectionRecord, InjectionStatus
from nodesync.shared.time_utils import now_ts
from src.maisaka.chat_loop_service import register_maisaka_hook_specs
from src.plugin_runtime.host.component_registry import ComponentRegistry
from src.plugin_runtime.host.hook_spec_registry import HookSpecRegistry
from src.plugin_runtime.runner.plugin_loader import PluginLoader


class FakeMessageCapability:
    async def get_recent(self, stream_id: str, limit: int = 40):
        del limit
        return [
            {
                "stream_id": stream_id,
                "message_id": "m1",
                "sender_id": "u1",
                "sender_name": "测试用户",
                "plain_text": "RP 场景：我们在门口。",
                "timestamp": now_ts(),
            }
        ]


class FakeLLMCapability:
    async def generate(self, **kwargs):
        del kwargs
        return {"success": False, "response": "", "error": "测试桩未启用 LLM"}


class FakeSendCapability:
    def __init__(self) -> None:
        self.messages = []

    async def text(self, content: str, stream_id: str):
        self.messages.append((content, stream_id))
        return True


class FakeContext:
    def __init__(self) -> None:
        self.message = FakeMessageCapability()
        self.llm = FakeLLMCapability()
        self.send = FakeSendCapability()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def wait_until(predicate, max_wait: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + max_wait
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("等待条件超时")


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        plugin_root = tmp_path / "plugins"
        plugin_dir = plugin_root / "nodesync_coordinator"
        shutil.copytree(node_root / "nodesync" / "adapters" / "maibot_10", plugin_dir)

        loader = PluginLoader(host_version="1.0.0-pre.19")
        loaded = loader.discover_and_load([str(plugin_root)])
        assert len(loaded) == 1, loader.failed_plugins
        meta = loaded[0]
        instance = meta.instance
        module = sys.modules[meta.module_name]

        components = instance.get_components()
        component_types = {str(item["type"]).lower() for item in components}
        assert "hook_handler" in component_types
        assert "event_handler" in component_types
        assert "workflow_step" not in component_types

        hook_specs = HookSpecRegistry()
        register_maisaka_hook_specs(hook_specs)
        registry = ComponentRegistry(hook_specs)
        declarations = [
            {"name": item["name"], "component_type": item["type"], "metadata": item["metadata"]}
            for item in components
        ]
        assert registry.register_plugin_components(meta.plugin_id, declarations) == len(components)
        hook_handlers = registry.get_hook_handlers("maisaka.planner.before_request")
        assert len(hook_handlers) == 1
        assert hook_handlers[0].name == "nodesync_maisaka_before_request"

        port = free_port()
        config = module.NodeSync10PluginConfig.model_validate(
            {
                "plugin": {"enabled": True, "config_version": "0.1.0"},
                "nodesync": {
                    "mode": "server",
                    "bot_id": "real-10",
                    "bot_name": "Real 1.0 Smoke",
                    "auth_token": "real-test-token",
                    "server_host": "127.0.0.1",
                    "server_port": port,
                    "server_url": f"http://127.0.0.1:{port}",
                    "data_dir": str(tmp_path / "data"),
                    "streams": ["group-real"],
                },
                "policy": {
                    "enable_llm_decision": False,
                    "enable_llm_advance_generation": False,
                    "intervention_cooldown_seconds": 0,
                    "analysis_min_messages": 8,
                },
            }
        )
        if hasattr(instance, "set_plugin_config"):
            instance.set_plugin_config(config.model_dump(mode="json"))
        else:
            instance.config = config
        fake_context = FakeContext()
        instance._set_context(fake_context)

        await instance.on_load()
        try:
            assert instance._runtime is not None
            await wait_until(lambda: instance._runtime.server and "real-10" in instance._runtime.server._clients)

            result = await instance.handle_message(
                message={
                    "stream_id": "group-real",
                    "message_id": "m-real",
                    "sender_id": "u1",
                    "sender_name": "测试用户",
                    "plain_text": "RP 场景：我们在门口。",
                    "timestamp": now_ts(),
                },
                stream_id="group-real",
            )
            assert result["continue_processing"] is True
            await wait_until(lambda: bool(instance._runtime.server.storage.get_recent_messages("group-real", 5)))

            record = InjectionRecord(
                injection_id="real-injection-10",
                session_id="group-real",
                stream_id="group-real",
                target_bot_id="real-10",
                directive_type=DirectiveType.ALIGN_CONTEXT.value,
                content="继续留在当前 RP 场景，把下一步行动说具体。",
                source_scene_snapshot={"topic": "门口停滞"},
                created_at=now_ts(),
                expires_at=now_ts() + 300,
                status=InjectionStatus.APPLIED.value,
            )
            directive = Directive(
                directive_id=record.injection_id,
                session_id=record.session_id,
                stream_id=record.stream_id,
                target_bot_id=record.target_bot_id,
                directive_type=record.directive_type,
                content=record.content,
                source_scene_snapshot=record.source_scene_snapshot,
                created_at=record.created_at,
                expires_at=record.expires_at,
            )
            await instance._bridge.apply_context_injection(directive, record)
            hook_result = await instance.handle_maisaka_before_request(
                messages=[{"role": "user", "content": "大家仍然停在门口。"}],
                tool_definitions=[],
                selected_history_count=1,
                built_message_count=1,
                selection_reason="smoke",
                session_id="group-real",
            )
            modified_messages = hook_result["modified_kwargs"]["messages"]
            assert modified_messages[0]["role"] == "system"
            assert "NodeSync 私有上下文对齐" in modified_messages[0]["content"]

            send_directive = Directive(
                directive_id="real-send-10",
                session_id="group-real",
                stream_id="group-real",
                target_bot_id="real-10",
                directive_type=DirectiveType.ADVANCE_DIALOGUE.value,
                content="我们先把下一步行动定下来。",
                source_scene_snapshot={"topic": "门口停滞"},
                created_at=now_ts(),
                expires_at=now_ts() + 300,
            )
            assert await instance._bridge.send_message(send_directive)
            assert fake_context.send.messages == [("我们先把下一步行动定下来。", "group-real")]
        finally:
            await instance.on_unload()

    print("MAIBOT_10_SMOKE_OK")


asyncio.run(main())
"""
        completed = _run_python_script(
            command=[uv_path, "run", "python"],
            script=_fill_paths(script),
            cwd=MAIBOT_10_ROOT,
            timeout_seconds=300,
        )
        self.assertIn("MAIBOT_10_SMOKE_OK", completed.stdout)


def _fill_paths(script: str) -> str:
    return (
        textwrap.dedent(script)
        .replace("%NODESYNC_ROOT%", str(NODESYNC_ROOT))
        .replace("%MAIBOT_012_ROOT%", str(MAIBOT_012_ROOT))
    )


def _run_python_script(
    command: list[str],
    script: str,
    cwd: Path,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    python_path_entries = [str(NODESYNC_ROOT), str(cwd)]
    if env.get("PYTHONPATH"):
        python_path_entries.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_path_entries)
    completed = subprocess.run(
        [*command, "-c", script],
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "真实 MaiBot 宿主烟测失败\n"
            f"command: {' '.join(command)}\n"
            f"cwd: {cwd}\n"
            f"returncode: {completed.returncode}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    return completed


if __name__ == "__main__":
    unittest.main()
