from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path

REAL_MODEL_TESTS_ENABLED = os.environ.get("NODESYNC_REAL_MODEL_TESTS") == "1"
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
NODESYNC_ROOT = WORKSPACE_ROOT / "NodeSync"
MAIBOT_012_ROOT = WORKSPACE_ROOT / "MaiBot-0.12.2"
MAIBOT_10_ROOT = WORKSPACE_ROOT / "MaiBot-1.0-latest"
ROOT_BOT_CONFIG = WORKSPACE_ROOT / "bot_config.toml"
ROOT_MODEL_CONFIG = WORKSPACE_ROOT / "model_config.toml"

AUTH_TOKEN = "nodesync-real-model-test-token"
SERVER_BOT_ID = "nodesync-real-model-012-server"
CLIENT_BOT_ID = "nodesync-real-model-10-client"
ADVANCE_STREAM_ID = "group-real-model-advance"


@unittest.skipUnless(REAL_MODEL_TESTS_ENABLED, "设置 NODESYNC_REAL_MODEL_TESTS=1 才执行真实模型集成测试")
class RealModelIntegrationTest(unittest.TestCase):
    def test_real_model_generates_advance_dialogue_for_two_bot_runtime(self) -> None:
        self.assertTrue(MAIBOT_012_ROOT.exists(), f"缺少 MaiBot 0.12.2 源码目录: {MAIBOT_012_ROOT}")
        self.assertTrue(MAIBOT_10_ROOT.exists(), f"缺少 MaiBot 1.0 源码目录: {MAIBOT_10_ROOT}")
        self.assertTrue(ROOT_BOT_CONFIG.exists(), f"缺少根目录 bot_config.toml: {ROOT_BOT_CONFIG}")
        self.assertTrue(ROOT_MODEL_CONFIG.exists(), f"缺少根目录 model_config.toml: {ROOT_MODEL_CONFIG}")
        uv_path = shutil.which("uv")
        self.assertIsNotNone(uv_path, "缺少 uv，无法按 MaiBot 1.0 项目依赖环境调用真实模型")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            port = _free_port()
            stop_file = tmp_path / "stop.flag"
            server_ready = tmp_path / "server.ready.json"
            client_ready = tmp_path / "client.ready.json"
            llm_log = tmp_path / "llm_calls.jsonl"
            send_log = tmp_path / "client_send.jsonl"
            context_log = tmp_path / "context_requests.jsonl"
            server_data_dir = tmp_path / "server_data"
            server_log_path = tmp_path / "server.log"
            client_log_path = tmp_path / "client.log"

            server_script = tmp_path / "server_bot_012.py"
            client_script = tmp_path / "client_bot_10_real_llm.py"
            server_script.write_text(_fill_paths(_SERVER_BOT_SCRIPT, tmp_path, port), encoding="utf-8")
            client_script.write_text(_fill_paths(_CLIENT_BOT_SCRIPT, tmp_path, port), encoding="utf-8")

            server_log_handle = server_log_path.open("w", encoding="utf-8")
            client_log_handle = client_log_path.open("w", encoding="utf-8")
            server_proc: subprocess.Popen[str] | None = None
            client_proc: subprocess.Popen[str] | None = None
            try:
                server_proc = subprocess.Popen(
                    [sys.executable, "-u", str(server_script)],
                    cwd=str(MAIBOT_012_ROOT),
                    env=_python_env(MAIBOT_012_ROOT),
                    stdout=server_log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                _wait_until(
                    server_ready.exists,
                    "0.12 server 模式启动",
                    processes=[server_proc],
                    log_paths=[server_log_path],
                    timeout_seconds=30,
                )

                client_proc = subprocess.Popen(
                    [str(uv_path), "run", "python", "-u", str(client_script)],
                    cwd=str(MAIBOT_10_ROOT),
                    env=_python_env(MAIBOT_10_ROOT),
                    stdout=client_log_handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                processes = [server_proc, client_proc]
                logs = [server_log_path, client_log_path]
                _wait_until(
                    client_ready.exists,
                    "1.0 client 模式启动并连接",
                    processes=processes,
                    log_paths=logs,
                    timeout_seconds=45,
                )
                _wait_until(
                    lambda: CLIENT_BOT_ID
                    in _http_json(f"http://127.0.0.1:{port}/bots", token=AUTH_TOKEN).get("live", []),
                    "server 看到真实模型 client 在线",
                    processes=processes,
                    log_paths=logs,
                    timeout_seconds=20,
                )

                _post_analyze(port, ADVANCE_STREAM_ID)
                _wait_until(
                    lambda: _jsonl_has(llm_log, lambda item: item.get("success") is True and bool(item.get("model_name"))),
                    "client 使用真实模型完成 LLM 生成",
                    processes=processes,
                    log_paths=logs,
                    timeout_seconds=120,
                )
                _wait_until(
                    lambda: _has_directive(
                        server_data_dir / "nodesync.sqlite3",
                        stream_id=ADVANCE_STREAM_ID,
                        directive_type="advance_dialogue",
                        status="applied",
                    ),
                    "advance_dialogue 指令使用真实模型生成后完成",
                    processes=processes,
                    log_paths=logs,
                    timeout_seconds=30,
                )
                _wait_until(
                    lambda: _jsonl_has(send_log, lambda item: item.get("stream_id") == ADVANCE_STREAM_ID),
                    "真实模型生成的推进消息进入 send capability",
                    processes=processes,
                    log_paths=logs,
                    timeout_seconds=30,
                )

                llm_calls = _read_jsonl(llm_log)
                sent_messages = _read_jsonl(send_log)
                context_streams = {item.get("stream_id") for item in _read_jsonl(context_log)}
                self.assertIn(ADVANCE_STREAM_ID, context_streams)
                self.assertTrue(llm_calls[0]["response_excerpt"])
                self.assertTrue(sent_messages[0]["content"])
                self.assertTrue(_has_message(server_data_dir / "nodesync.sqlite3", ADVANCE_STREAM_ID))
            finally:
                stop_file.write_text("stop", encoding="utf-8")
                _stop_processes([proc for proc in [client_proc, server_proc] if proc is not None])
                server_log_handle.close()
                client_log_handle.close()
                # Windows 下 SQLite/WAL 文件句柄释放可能略晚。
                time.sleep(0.5)


def _fill_paths(script: str, tmp_path: Path, port: int) -> str:
    return (
        textwrap.dedent(script)
        .replace("%NODESYNC_ROOT%", str(NODESYNC_ROOT))
        .replace("%MAIBOT_012_ROOT%", str(MAIBOT_012_ROOT))
        .replace("%ROOT_BOT_CONFIG%", str(ROOT_BOT_CONFIG))
        .replace("%ROOT_MODEL_CONFIG%", str(ROOT_MODEL_CONFIG))
        .replace("%TMP_DIR%", str(tmp_path))
        .replace("%PORT%", str(port))
        .replace("%AUTH_TOKEN%", AUTH_TOKEN)
        .replace("%SERVER_BOT_ID%", SERVER_BOT_ID)
        .replace("%CLIENT_BOT_ID%", CLIENT_BOT_ID)
        .replace("%ADVANCE_STREAM_ID%", ADVANCE_STREAM_ID)
    )


def _python_env(host_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    python_path_entries = [str(NODESYNC_ROOT), str(host_root)]
    if env.get("PYTHONPATH"):
        python_path_entries.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_path_entries)
    return env


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until(
    predicate: Callable[[], bool],
    label: str,
    *,
    processes: Iterable[subprocess.Popen[str]],
    log_paths: Iterable[Path],
    timeout_seconds: float = 30,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    process_list = list(processes)
    log_path_list = list(log_paths)
    while time.monotonic() < deadline:
        for process in process_list:
            if process.poll() is not None:
                raise AssertionError(_process_failure_message(label, process_list, log_path_list))
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"等待 {label} 超时\n{_logs_tail(log_path_list)}")


def _process_failure_message(
    label: str,
    processes: Iterable[subprocess.Popen[str]],
    log_paths: Iterable[Path],
) -> str:
    statuses = ", ".join(f"pid={proc.pid} returncode={proc.poll()}" for proc in processes)
    return f"等待 {label} 时子进程提前退出: {statuses}\n{_logs_tail(log_paths)}"


def _logs_tail(log_paths: Iterable[Path], max_chars: int = 6000) -> str:
    parts = []
    for path in log_paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        parts.append(f"--- {path.name} ---\n{text[-max_chars:]}")
    return "\n".join(parts)


def _stop_processes(processes: list[subprocess.Popen[str]]) -> None:
    for process in processes:
        try:
            process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _http_json(url: str, token: str = "") -> dict[str, object]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - 本地测试服务
        return dict(json.loads(response.read().decode("utf-8")))


def _post_analyze(port: int, stream_id: str) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/streams/{stream_id}/analyze",
        data=b"",
        method="POST",
        headers={"Authorization": f"Bearer {AUTH_TOKEN}"},
    )
    with urllib.request.urlopen(request, timeout=150) as response:  # noqa: S310 - 本地测试服务
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise AssertionError(f"analyze 接口返回异常: {payload}")


def _has_directive(db_path: Path, stream_id: str, directive_type: str, status: str) -> bool:
    for row in _directive_rows(db_path):
        payload = row["payload"]
        if (
            payload.get("stream_id") == stream_id
            and payload.get("directive_type") == directive_type
            and row["status"] == status
            and payload.get("target_bot_id") == CLIENT_BOT_ID
        ):
            return True
    return False


def _directive_rows(db_path: Path) -> list[dict[str, object]]:
    if not db_path.exists():
        return []
    with sqlite3.connect(str(db_path), timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        rows = conn.execute("SELECT payload_json, status FROM directives ORDER BY updated_at").fetchall()
    return [{"payload": json.loads(str(row["payload_json"])), "status": str(row["status"])} for row in rows]


def _has_message(db_path: Path, stream_id: str) -> bool:
    if not db_path.exists():
        return False
    with sqlite3.connect(str(db_path), timeout=5) as conn:
        conn.execute("PRAGMA busy_timeout = 5000")
        row = conn.execute("SELECT COUNT(*) FROM messages WHERE stream_id = ?", (stream_id,)).fetchone()
    return bool(row and row[0])


def _jsonl_has(path: Path, predicate: Callable[[dict[str, object]], bool]) -> bool:
    return any(predicate(item) for item in _read_jsonl(path))


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            items.append(dict(json.loads(line)))
        except json.JSONDecodeError:
            continue
    return items


_SERVER_BOT_SCRIPT = r"""
import asyncio
import importlib.util
import json
import shutil
import sys
import traceback
from pathlib import Path

node_root = Path(r"%NODESYNC_ROOT%")
host_root = Path(r"%MAIBOT_012_ROOT%")
tmp_path = Path(r"%TMP_DIR%")
sys.path.insert(0, str(node_root))
sys.path.insert(0, str(host_root))

ready_path = tmp_path / "server.ready.json"
error_path = tmp_path / "server.error.json"
stop_path = tmp_path / "stop.flag"


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


async def main():
    plugin_dir = tmp_path / "plugins_012" / "nodesync"
    shutil.copytree(node_root / "nodesync" / "adapters" / "maibot_012", plugin_dir)
    spec = importlib.util.spec_from_file_location("nodesync_real_model_maibot012", plugin_dir / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    config = {
        "plugin": {"enabled": True},
        "nodesync": {
            "mode": "server",
            "bot_id": "%SERVER_BOT_ID%",
            "bot_name": "NodeSync Real Model 0.12 Server",
            "auth_token": "%AUTH_TOKEN%",
            "server_host": "127.0.0.1",
            "server_port": %PORT%,
            "server_url": "http://127.0.0.1:%PORT%",
            "data_dir": str(tmp_path / "server_data"),
            "streams": ["server-only"],
        },
        "policy": {
            "enable_llm_decision": False,
            "enable_llm_advance_generation": True,
            "llm_worker_bot_id": "%CLIENT_BOT_ID%",
            "llm_generation_timeout_seconds": 90,
            "intervention_cooldown_seconds": 0,
            "directive_retry_interval_seconds": 60,
            "directive_max_attempts": 2,
            "analysis_min_messages": 4,
        },
    }
    handler = module.NodeSync012StartHandler()
    handler.plugin_config = config
    ok, *_rest = await handler.execute(None)
    if not ok:
        raise RuntimeError("NodeSync012StartHandler returned false")
    write_json(ready_path, {"ok": True, "bot_id": "%SERVER_BOT_ID%", "port": %PORT%})
    try:
        while not stop_path.exists():
            await asyncio.sleep(0.1)
    finally:
        await module.NodeSync012StopHandler().execute(None)


try:
    asyncio.run(main())
except Exception as exc:
    write_json(error_path, {"error": repr(exc), "traceback": traceback.format_exc()})
    raise
"""


_CLIENT_BOT_SCRIPT = r"""
import asyncio
import json
import shutil
import sys
import traceback
from pathlib import Path

node_root = Path(r"%NODESYNC_ROOT%")
tmp_path = Path(r"%TMP_DIR%")
root_bot_config = Path(r"%ROOT_BOT_CONFIG%")
root_model_config = Path(r"%ROOT_MODEL_CONFIG%")
sys.path.insert(0, str(node_root))

ready_path = tmp_path / "client.ready.json"
error_path = tmp_path / "client.error.json"
stop_path = tmp_path / "stop.flag"
send_log_path = tmp_path / "client_send.jsonl"
context_log_path = tmp_path / "context_requests.jsonl"
llm_log_path = tmp_path / "llm_calls.jsonl"


def append_jsonl(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def messages_for(stream_id):
    from nodesync.shared.time_utils import now_ts

    base = now_ts()
    texts = [
        "RP 场景：石门前。",
        "我看看。",
        "我看看。",
        "我看看。",
        "我看看。",
        "我看看。",
    ]
    return [
        {
            "stream_id": stream_id,
            "message_id": f"{stream_id}-ctx-{index}",
            "sender_id": f"user-{index % 3}",
            "sender_name": f"测试用户{index % 3}",
            "plain_text": text,
            "timestamp": base + index,
        }
        for index, text in enumerate(texts)
    ]


def install_workspace_model_config():
    from src.config import config as cfg

    temp_config_dir = tmp_path / "workspace_config_copy"
    temp_config_dir.mkdir(parents=True, exist_ok=True)
    bot_copy = temp_config_dir / "bot_config.toml"
    model_copy = temp_config_dir / "model_config.toml"
    shutil.copy2(root_bot_config, bot_copy)
    shutil.copy2(root_model_config, model_copy)
    # 只加载临时复制文件；版本自动升级也只会写入临时目录，避免改写工作区根配置。
    cfg.config_manager.global_config, _ = cfg.load_config_from_file(cfg.Config, bot_copy, cfg.CONFIG_VERSION)
    cfg.config_manager.model_config, _ = cfg.load_config_from_file(
        cfg.ModelConfig,
        model_copy,
        cfg.MODEL_CONFIG_VERSION,
        True,
    )


class FakeMessageCapability:
    async def get_recent(self, stream_id: str, limit: int = 40):
        append_jsonl(context_log_path, {"stream_id": stream_id, "limit": limit})
        return messages_for(stream_id)[-limit:]


class RealLLMCapability:
    def __init__(self):
        self._configured = False

    async def generate(self, **kwargs):
        if not self._configured:
            install_workspace_model_config()
            self._configured = True
        from src.services import llm_service

        prompt = str(kwargs.get("prompt") or "")
        result = await llm_service.generate(
            llm_service.LLMServiceRequest(
                task_name="utils",
                request_type="nodesync.real_model_integration",
                prompt=prompt,
                temperature=kwargs.get("temperature"),
                max_tokens=min(int(kwargs.get("max_tokens") or 180), 180),
            )
        )
        payload = result.to_capability_payload()
        append_jsonl(
            llm_log_path,
            {
                "success": bool(payload.get("success")),
                "model_name": str(payload.get("model_name", "")),
                "response_excerpt": str(payload.get("response", ""))[:240],
                "error_excerpt": str(payload.get("error", ""))[:240] if payload.get("error") else "",
                "total_tokens": int(payload.get("total_tokens") or 0),
            },
        )
        return payload


class FakeSendCapability:
    async def text(self, content: str, stream_id: str):
        append_jsonl(send_log_path, {"content": content, "stream_id": stream_id})
        return True


class FakeContext:
    def __init__(self):
        self.message = FakeMessageCapability()
        self.llm = RealLLMCapability()
        self.send = FakeSendCapability()


async def wait_until(predicate, label: str, timeout: float = 15) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(label)


async def main():
    from nodesync.shared.time_utils import now_ts
    from src.plugin_runtime.runner.plugin_loader import PluginLoader

    plugin_root = tmp_path / "plugins_10"
    plugin_dir = plugin_root / "nodesync_coordinator"
    shutil.copytree(node_root / "nodesync" / "adapters" / "maibot_10", plugin_dir)

    loader = PluginLoader(host_version="1.0.0-pre.19")
    loaded = loader.discover_and_load([str(plugin_root)])
    if len(loaded) != 1:
        raise RuntimeError(f"插件加载失败: {loader.failed_plugins}")
    meta = loaded[0]
    instance = meta.instance
    module = sys.modules[meta.module_name]

    config = module.NodeSync10PluginConfig.model_validate(
        {
            "plugin": {"enabled": True, "config_version": "0.1.0"},
            "nodesync": {
                "mode": "client",
                "bot_id": "%CLIENT_BOT_ID%",
                "bot_name": "NodeSync Real Model 1.0 Client",
                "auth_token": "%AUTH_TOKEN%",
                "server_host": "127.0.0.1",
                "server_port": %PORT%,
                "server_url": "http://127.0.0.1:%PORT%",
                "data_dir": str(tmp_path / "client_data"),
                "streams": ["%ADVANCE_STREAM_ID%"],
            },
            "policy": {
                "enable_llm_decision": False,
                "enable_llm_advance_generation": True,
                "intervention_cooldown_seconds": 0,
                "directive_retry_interval_seconds": 60,
                "directive_max_attempts": 2,
                "analysis_min_messages": 4,
            },
        }
    )
    if hasattr(instance, "set_plugin_config"):
        instance.set_plugin_config(config.model_dump(mode="json"))
    else:
        instance.config = config
    instance._set_context(FakeContext())

    await instance.on_load()
    try:
        await wait_until(
            lambda: instance._runtime is not None
            and instance._runtime.client is not None
            and instance._runtime.client._ws is not None
            and not instance._runtime.client._ws.closed,
            "client websocket connected",
        )
        await instance.handle_message(
            message={
                "stream_id": "%ADVANCE_STREAM_ID%",
                "message_id": "real-model-client-message",
                "sender_id": "real-user",
                "sender_name": "真实测试用户",
                "plain_text": "RP 场景：石门前。",
                "timestamp": now_ts(),
            },
            stream_id="%ADVANCE_STREAM_ID%",
        )
        write_json(ready_path, {"ok": True, "bot_id": "%CLIENT_BOT_ID%"})
        while not stop_path.exists():
            await asyncio.sleep(0.1)
    finally:
        await instance.on_unload()


try:
    asyncio.run(main())
except Exception as exc:
    write_json(error_path, {"error": repr(exc), "traceback": traceback.format_exc()})
    raise
"""


if __name__ == "__main__":
    unittest.main()
