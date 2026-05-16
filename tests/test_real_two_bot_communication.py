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

REAL_TWO_BOT_TESTS_ENABLED = os.environ.get("NODESYNC_REAL_TWO_BOT_TESTS") == "1"
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
NODESYNC_ROOT = WORKSPACE_ROOT / "NodeSync"
MAIBOT_012_ROOT = WORKSPACE_ROOT / "MaiBot-0.12.2"
MAIBOT_10_ROOT = WORKSPACE_ROOT / "MaiBot-1.0-latest"
ROOT_BOT_CONFIG = WORKSPACE_ROOT / "bot_config.toml"
ROOT_MODEL_CONFIG = WORKSPACE_ROOT / "model_config.toml"

AUTH_TOKEN = "nodesync-two-bot-test-token"
SERVER_BOT_ID = "nodesync-012-server"
CLIENT_BOT_ID = "nodesync-10-client"
ALIGN_STREAM_ID = "group-align-real"
ADVANCE_STREAM_ID = "group-advance-real"


@unittest.skipUnless(REAL_TWO_BOT_TESTS_ENABLED, "设置 NODESYNC_REAL_TWO_BOT_TESTS=1 才执行双 Bot 通信烟测")
class RealMaiBotTwoBotCommunicationTest(unittest.TestCase):
    def test_maibot_012_server_and_maibot_10_client_exchange_context_and_directives(self) -> None:
        self.assertTrue(MAIBOT_012_ROOT.exists(), f"缺少 MaiBot 0.12.2 源码目录: {MAIBOT_012_ROOT}")
        self.assertTrue(MAIBOT_10_ROOT.exists(), f"缺少 MaiBot 1.0 源码目录: {MAIBOT_10_ROOT}")
        self.assertTrue(ROOT_BOT_CONFIG.exists(), f"缺少根目录 bot_config.toml: {ROOT_BOT_CONFIG}")
        self.assertTrue(ROOT_MODEL_CONFIG.exists(), f"缺少根目录 model_config.toml: {ROOT_MODEL_CONFIG}")
        uv_path = shutil.which("uv")
        self.assertIsNotNone(uv_path, "缺少 uv，无法按 MaiBot 1.0 项目依赖环境启动 client 实例")

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            port = _free_port()
            stop_file = tmp_path / "stop.flag"
            server_ready = tmp_path / "server.ready.json"
            client_ready = tmp_path / "client.ready.json"
            client_hook = tmp_path / "client_hook.json"
            client_send_log = tmp_path / "client_send.jsonl"
            context_request_log = tmp_path / "context_requests.jsonl"
            server_data_dir = tmp_path / "server_data"
            client_data_dir = tmp_path / "client_data"
            server_log_path = tmp_path / "server.log"
            client_log_path = tmp_path / "client.log"

            server_script = tmp_path / "server_bot_012.py"
            client_script = tmp_path / "client_bot_10.py"
            server_script.write_text(_fill_common_paths(_SERVER_BOT_SCRIPT, tmp_path, port), encoding="utf-8")
            client_script.write_text(_fill_common_paths(_CLIENT_BOT_SCRIPT, tmp_path, port), encoding="utf-8")

            env_012 = _python_env(MAIBOT_012_ROOT)
            env_10 = _python_env(MAIBOT_10_ROOT)
            server_log = server_log_path.open("w", encoding="utf-8")
            client_log = client_log_path.open("w", encoding="utf-8")
            server_proc: subprocess.Popen[str] | None = None
            client_proc: subprocess.Popen[str] | None = None
            try:
                server_proc = subprocess.Popen(
                    [sys.executable, "-u", str(server_script)],
                    cwd=str(MAIBOT_012_ROOT),
                    env=env_012,
                    stdout=server_log,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                _wait_until(
                    server_ready.exists,
                    "MaiBot 0.12.2 server 实例启动",
                    processes=[server_proc],
                    log_paths=[server_log_path],
                )
                _wait_until(
                    lambda: bool(_http_json(f"http://127.0.0.1:{port}/health").get("ok")),
                    "NodeSync server /health 可访问",
                    processes=[server_proc],
                    log_paths=[server_log_path],
                )

                client_proc = subprocess.Popen(
                    [str(uv_path), "run", "python", "-u", str(client_script)],
                    cwd=str(MAIBOT_10_ROOT),
                    env=env_10,
                    stdout=client_log,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                processes = [server_proc, client_proc]
                logs = [server_log_path, client_log_path]
                _wait_until(
                    client_ready.exists,
                    "MaiBot 1.0 client 实例启动并发送首条消息",
                    processes=processes,
                    log_paths=logs,
                    timeout_seconds=45,
                )
                _wait_until(
                    lambda: CLIENT_BOT_ID
                    in _http_json(f"http://127.0.0.1:{port}/bots", token=AUTH_TOKEN).get("live", []),
                    "client 出现在 server 在线 bot 列表",
                    processes=processes,
                    log_paths=logs,
                )

                _post_analyze(port, ALIGN_STREAM_ID)
                _wait_until(
                    lambda: _has_directive(
                        server_data_dir / "nodesync.sqlite3",
                        stream_id=ALIGN_STREAM_ID,
                        directive_type="align_context",
                        status="applied",
                    ),
                    "align_context 指令完成 ACK/RESULT 回传",
                    processes=processes,
                    log_paths=logs,
                    timeout_seconds=20,
                )
                _wait_until(
                    lambda: _jsonl_has(
                        client_data_dir / "context_injections.jsonl",
                        lambda item: item.get("stream_id") == ALIGN_STREAM_ID and item.get("status") == "applied",
                    ),
                    "client 本地注入 JSONL 写入 applied 记录",
                    processes=processes,
                    log_paths=logs,
                    timeout_seconds=20,
                )
                _wait_until(
                    lambda: _json_file_has(client_hook, lambda item: item.get("has_private_alignment") is True),
                    "1.0 before_request hook 读取到跨进程下发的注入",
                    processes=processes,
                    log_paths=logs,
                    timeout_seconds=20,
                )

                _post_analyze(port, ADVANCE_STREAM_ID)
                _wait_until(
                    lambda: _has_directive(
                        server_data_dir / "nodesync.sqlite3",
                        stream_id=ADVANCE_STREAM_ID,
                        directive_type="advance_dialogue",
                        status="applied",
                    ),
                    "advance_dialogue 指令完成 ACK/RESULT 回传",
                    processes=processes,
                    log_paths=logs,
                    timeout_seconds=20,
                )
                _wait_until(
                    lambda: _jsonl_has(
                        client_send_log,
                        lambda item: item.get("stream_id") == ADVANCE_STREAM_ID and bool(item.get("content")),
                    ),
                    "1.0 send capability 收到群内推进消息",
                    processes=processes,
                    log_paths=logs,
                    timeout_seconds=20,
                )

                context_streams = {
                    item.get("stream_id") for item in _read_jsonl(context_request_log) if item.get("type") == "get_recent"
                }
                self.assertIn(ALIGN_STREAM_ID, context_streams)
                self.assertIn(ADVANCE_STREAM_ID, context_streams)
                self.assertTrue(
                    _has_message(server_data_dir / "nodesync.sqlite3", ALIGN_STREAM_ID),
                    "server SQLite 应记录 client 上报的 chat.event",
                )
            finally:
                stop_file.write_text("stop", encoding="utf-8")
                _stop_processes([proc for proc in [client_proc, server_proc] if proc is not None])
                server_log.close()
                client_log.close()
                # Windows 下 SQLite/WAL 文件句柄释放有时会略晚于进程退出。
                time.sleep(0.5)


def _fill_common_paths(script: str, tmp_path: Path, port: int) -> str:
    return (
        textwrap.dedent(script)
        .replace("%NODESYNC_ROOT%", str(NODESYNC_ROOT))
        .replace("%MAIBOT_012_ROOT%", str(MAIBOT_012_ROOT))
        .replace("%TMP_DIR%", str(tmp_path))
        .replace("%PORT%", str(port))
        .replace("%AUTH_TOKEN%", AUTH_TOKEN)
        .replace("%SERVER_BOT_ID%", SERVER_BOT_ID)
        .replace("%CLIENT_BOT_ID%", CLIENT_BOT_ID)
        .replace("%ALIGN_STREAM_ID%", ALIGN_STREAM_ID)
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
    raise AssertionError(_timeout_message(label, log_path_list))


def _process_failure_message(
    label: str,
    processes: Iterable[subprocess.Popen[str]],
    log_paths: Iterable[Path],
) -> str:
    statuses = ", ".join(f"pid={proc.pid} returncode={proc.poll()}" for proc in processes)
    return f"等待 {label} 时子进程提前退出: {statuses}\n{_logs_tail(log_paths)}"


def _timeout_message(label: str, log_paths: Iterable[Path]) -> str:
    return f"等待 {label} 超时\n{_logs_tail(log_paths)}"


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
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - 本地测试服务
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


def _json_file_has(path: Path, predicate: Callable[[dict[str, object]], bool]) -> bool:
    if not path.exists():
        return False
    return predicate(dict(json.loads(path.read_text(encoding="utf-8"))))


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
    spec = importlib.util.spec_from_file_location("nodesync_two_bot_maibot012", plugin_dir / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    config = {
        "plugin": {"enabled": True},
        "nodesync": {
            "mode": "server",
            "bot_id": "%SERVER_BOT_ID%",
            "bot_name": "NodeSync 0.12 Server Bot",
            "auth_token": "%AUTH_TOKEN%",
            "server_host": "127.0.0.1",
            "server_port": %PORT%,
            "server_url": "http://127.0.0.1:%PORT%",
            "data_dir": str(tmp_path / "server_data"),
            # server 模式也会启动本机 client；这里限定 stream，确保被测 stream 只由远端 1.0 client 执行。
            "streams": ["server-only"],
        },
        "policy": {
            "enable_llm_decision": False,
            "enable_llm_advance_generation": False,
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
sys.path.insert(0, str(node_root))

from nodesync.shared.time_utils import now_ts
from src.plugin_runtime.runner.plugin_loader import PluginLoader

ready_path = tmp_path / "client.ready.json"
error_path = tmp_path / "client.error.json"
stop_path = tmp_path / "stop.flag"
send_log_path = tmp_path / "client_send.jsonl"
context_request_log_path = tmp_path / "context_requests.jsonl"
hook_path = tmp_path / "client_hook.json"


def append_jsonl(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def messages_for(stream_id):
    base = now_ts()
    if stream_id == "%ADVANCE_STREAM_ID%":
        texts = [
            "RP 场景：门口。",
            "我看看。",
            "我看看。",
            "我看看。",
            "我看看。",
            "我看看。",
        ]
    else:
        texts = [
            "RP 场景：我们在门口。",
            "我看看。",
            "我也看看。",
            "继续看看。",
            "还是看看。",
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


class FakeMessageCapability:
    async def get_recent(self, stream_id: str, limit: int = 40):
        append_jsonl(context_request_log_path, {"type": "get_recent", "stream_id": stream_id, "limit": limit})
        return messages_for(stream_id)[-limit:]


class FakeLLMCapability:
    async def generate(self, **kwargs):
        append_jsonl(context_request_log_path, {"type": "llm_generate", "keys": sorted(kwargs)})
        return {"success": False, "response": "", "error": "双 Bot 通信烟测关闭真实 LLM"}


class FakeSendCapability:
    async def text(self, content: str, stream_id: str):
        append_jsonl(send_log_path, {"content": content, "stream_id": stream_id})
        return True


class FakeContext:
    def __init__(self):
        self.message = FakeMessageCapability()
        self.llm = FakeLLMCapability()
        self.send = FakeSendCapability()


async def wait_until(predicate, label: str, timeout: float = 15) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(label)


async def main():
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
                "bot_name": "NodeSync 1.0 Client Bot",
                "auth_token": "%AUTH_TOKEN%",
                "server_host": "127.0.0.1",
                "server_port": %PORT%,
                "server_url": "http://127.0.0.1:%PORT%",
                "data_dir": str(tmp_path / "client_data"),
                "streams": ["%ALIGN_STREAM_ID%", "%ADVANCE_STREAM_ID%"],
            },
            "policy": {
                "enable_llm_decision": False,
                "enable_llm_advance_generation": False,
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
                "stream_id": "%ALIGN_STREAM_ID%",
                "message_id": "client-reported-message",
                "sender_id": "real-user",
                "sender_name": "真实测试用户",
                "plain_text": "RP 场景：我们在门口。",
                "timestamp": now_ts(),
            },
            stream_id="%ALIGN_STREAM_ID%",
        )
        write_json(ready_path, {"ok": True, "bot_id": "%CLIENT_BOT_ID%"})

        hook_written = False
        while not stop_path.exists():
            if (
                not hook_written
                and instance._runtime is not None
                and instance._runtime.get_active_injection("%ALIGN_STREAM_ID%") is not None
            ):
                result = await instance.handle_maisaka_before_request(
                    messages=[{"role": "user", "content": "大家还停在门口。"}],
                    tool_definitions=[],
                    selected_history_count=1,
                    built_message_count=1,
                    selection_reason="two-bot-smoke",
                    session_id="%ALIGN_STREAM_ID%",
                )
                modified = result.get("modified_kwargs", {}).get("messages", [])
                first = modified[0] if modified else {}
                write_json(
                    hook_path,
                    {
                        "has_private_alignment": "NodeSync 私有上下文对齐" in str(first.get("content", "")),
                        "message_count": len(modified),
                    },
                )
                hook_written = True
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
