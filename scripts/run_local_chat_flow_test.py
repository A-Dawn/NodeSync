"""拉起两个真实 adapter bot，维护一条本地聊天流并检查 NodeSync 状态。"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiohttp import web

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
NODESYNC_ROOT = WORKSPACE_ROOT / "NodeSync"
MAIBOT_10_ROOT = WORKSPACE_ROOT / "MaiBot-1.0-latest"
ROOT_BOT_CONFIG = WORKSPACE_ROOT / "bot_config.toml"
ROOT_MODEL_CONFIG = WORKSPACE_ROOT / "model_config.toml"

STREAM_ID = "nodesync-local-chat-flow"
TERMINAL_DIRECTIVE_STATUSES = {"applied", "failed", "expired", "exhausted", "cancelled"}
BOT_A_ID = "nodesync-flow-a"
BOT_B_ID = "nodesync-flow-b"
BOT_A_NAME = "澪"
BOT_B_NAME = "柚"
BOT_A_PERSONA = "你叫澪，语气冷静、观察细致，像队伍里的调查员。你会把模糊线索拆成具体行动。"
BOT_B_PERSONA = "你叫柚，语气轻快但不跳脱，擅长把别人的想法接住并补一个可执行的小步骤。"
DEFAULT_OPENING_TOPIC = (
    "RP 场景：你们在一座旧图书馆地下室，面前是一扇会低声重复人名的石门。"
    "门上有三枚暗淡符文，空气里有潮湿铁锈味。不要换场景，先决定怎么推进。"
)
STAGNATION_MODES = ("short-repeat", "action-overload")
ACTION_OVERLOAD_MESSAGES = (
    "澪又把车票举到窗边对光，只描述票面泛黄，没有得到新的文字或反应。",
    "柚继续在笔记本上画钟楼和广播的连线，但没有确认任何新的对应关系。",
    "澪沿着过道走了两步又停下，仍然只是观察倒挂城市灯光。",
    "柚把广播间隔重新数了一遍，记录动作更多了，但结论仍停在十二分钟。",
    "澪再次摸出口袋里的车票，反复确认目的地，却没有发现新的站名。",
    "柚把车窗擦亮一点，继续描写灯光流动，没有让车厢或线索发生变化。",
    "澪把手放到车门把手上又收回，只补充动作，没有决定是否进入下一节车厢。",
    "柚重新整理笔记页，仍围绕钟楼和上车原因画圈，没有新增判断。",
    "澪看向广播喇叭，等待下一次播报，场面继续原地积累动作。",
)
ACTION_OVERLOAD_SECONDARY_MESSAGES = (
    "澪再次检查车票边角，只描述纸张磨损，没有发现日期以外的新信息。",
    "柚把笔记本翻到空白页，继续抄写十二分钟这个结论。",
    "澪沿着座椅边缘摸索，动作变多，但没有触发机关或反馈。",
    "柚盯着窗外钟楼灯光，继续记录亮度变化，却没有形成下一步决定。",
    "澪又看了一眼车门，仍停在准备动作，没有开门也没有呼叫乘务员。",
    "柚把连线图重画一遍，内容和上一页几乎一样。",
    "澪把耳朵贴近广播口，还是只等待同一句播报。",
    "柚把旧车票夹进笔记本，动作结束后仍没有选择去哪里。",
    "澪重新站回过道中央，继续观察窗外倒挂城市。",
    "柚低头确认自己的上车理由，结论仍然是记不清。",
    "澪又把车票举起来，没有任何新文字浮现。",
    "柚再次说先记录下来，话题仍停在记录本身。",
)


@dataclass(frozen=True, slots=True)
class Ports:
    """本地联调端口集合。"""

    nodesync: int
    bot_a_flow: int
    bot_a_control: int
    bot_b_flow: int
    bot_b_control: int


@dataclass(frozen=True, slots=True)
class ChildOptions:
    """子进程 bot 配置。"""

    run_dir: Path
    bot_id: str
    bot_name: str
    persona: str
    mode: str
    auth_token: str
    nodesync_port: int
    local_flow_port: int
    control_port: int
    stream_id: str
    data_dir: Path
    llm_worker_bot_id: str


class LocalChatFlowError(RuntimeError):
    """本地聊天流测试中的可预期错误。"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行 NodeSync 双 bot 本地聊天流实测。")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-dir", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--bot-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--bot-name", default="", help=argparse.SUPPRESS)
    parser.add_argument("--persona", default="", help=argparse.SUPPRESS)
    parser.add_argument("--mode", default="", help=argparse.SUPPRESS)
    parser.add_argument("--auth-token", default="", help=argparse.SUPPRESS)
    parser.add_argument("--nodesync-port", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--local-flow-port", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--control-port", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--stream-id", default=STREAM_ID, help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--llm-worker-bot-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--opening-topic", default=DEFAULT_OPENING_TOPIC, help="本地聊天流开场话题。")
    parser.add_argument(
        "--stagnation-mode",
        default="short-repeat",
        choices=STAGNATION_MODES,
        help="停滞注入模式：short-repeat 短句重复；action-overload 同话题动作堆叠。",
    )
    parser.add_argument("--keep-run-dir", action="store_true", help="保留本次运行目录，默认本来就会保留。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.child:
        options = ChildOptions(
            run_dir=_required_path(args.run_dir, "--run-dir"),
            bot_id=args.bot_id,
            bot_name=args.bot_name,
            persona=args.persona,
            mode=args.mode,
            auth_token=args.auth_token,
            nodesync_port=args.nodesync_port,
            local_flow_port=args.local_flow_port,
            control_port=args.control_port,
            stream_id=args.stream_id,
            data_dir=_required_path(args.data_dir, "--data-dir"),
            llm_worker_bot_id=args.llm_worker_bot_id,
        )
        return _run_child(options)
    try:
        report_path = _run_controller(args.opening_topic, args.stagnation_mode)
    except LocalChatFlowError as exc:
        print(f"[NodeSyncFlowTest] 失败: {exc}", file=sys.stderr)
        return 1
    print(f"[NodeSyncFlowTest] 报告已写入: {report_path}")
    return 0


def _run_controller(opening_topic: str = DEFAULT_OPENING_TOPIC, stagnation_mode: str = "short-repeat") -> Path:
    _validate_environment()
    uv_path = shutil.which("uv")
    if uv_path is None:
        raise LocalChatFlowError("缺少 uv，无法按 MaiBot 1.0 项目环境拉起 bot 进程。")

    run_dir = NODESYNC_ROOT / "data" / "local_chat_flow_runs" / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    ports = Ports(
        nodesync=_free_port(),
        bot_a_flow=_free_port(),
        bot_a_control=_free_port(),
        bot_b_flow=_free_port(),
        bot_b_control=_free_port(),
    )
    auth_token = secrets.token_hex(32)
    server_data_dir = run_dir / "bot_a_data"
    client_data_dir = run_dir / "bot_b_data"

    processes: list[subprocess.Popen[str]] = []
    log_handles = []
    try:
        processes.append(
            _start_child(
                uv_path=uv_path,
                run_dir=run_dir,
                log_path=run_dir / "bot_a.log",
                options=ChildOptions(
                    run_dir=run_dir,
                    bot_id=BOT_A_ID,
                    bot_name=BOT_A_NAME,
                    persona=BOT_A_PERSONA,
                    mode="server",
                    auth_token=auth_token,
                    nodesync_port=ports.nodesync,
                    local_flow_port=ports.bot_a_flow,
                    control_port=ports.bot_a_control,
                    stream_id=STREAM_ID,
                    data_dir=server_data_dir,
                    llm_worker_bot_id=BOT_B_ID,
                ),
                log_handles=log_handles,
            )
        )
        _wait_until(
            lambda: _http_json(f"http://127.0.0.1:{ports.bot_a_control}/health", token=auth_token).get("ok") is True,
            "bot A 控制入口启动",
            processes=processes,
            log_paths=[run_dir / "bot_a.log"],
            timeout_seconds=60,
        )
        _wait_until(
            lambda: _http_json(f"http://127.0.0.1:{ports.nodesync}/health").get("ok") is True,
            "NodeSync server 启动",
            processes=processes,
            log_paths=[run_dir / "bot_a.log"],
            timeout_seconds=30,
        )

        processes.append(
            _start_child(
                uv_path=uv_path,
                run_dir=run_dir,
                log_path=run_dir / "bot_b.log",
                options=ChildOptions(
                    run_dir=run_dir,
                    bot_id=BOT_B_ID,
                    bot_name=BOT_B_NAME,
                    persona=BOT_B_PERSONA,
                    mode="client",
                    auth_token=auth_token,
                    nodesync_port=ports.nodesync,
                    local_flow_port=ports.bot_b_flow,
                    control_port=ports.bot_b_control,
                    stream_id=STREAM_ID,
                    data_dir=client_data_dir,
                    llm_worker_bot_id="",
                ),
                log_handles=log_handles,
            )
        )
        _wait_until(
            lambda: _http_json(f"http://127.0.0.1:{ports.bot_b_control}/health", token=auth_token).get("ok") is True,
            "bot B 控制入口启动",
            processes=processes,
            log_paths=[run_dir / "bot_a.log", run_dir / "bot_b.log"],
            timeout_seconds=60,
        )
        _wait_until(
            lambda: {BOT_A_ID, BOT_B_ID}.issubset(
                set(_http_json(f"http://127.0.0.1:{ports.nodesync}/bots", token=auth_token).get("live", []))
            ),
            "两个 bot 均出现在 NodeSync 在线列表",
            processes=processes,
            log_paths=[run_dir / "bot_a.log", run_dir / "bot_b.log"],
            timeout_seconds=45,
        )

        transcript = _drive_conversation(
            ports,
            auth_token,
            processes,
            [run_dir / "bot_a.log", run_dir / "bot_b.log"],
            opening_topic,
        )
        _trigger_stagnation(
            ports,
            auth_token,
            processes,
            [run_dir / "bot_a.log", run_dir / "bot_b.log"],
            transcript,
            server_data_dir,
            stagnation_mode,
        )

        status = _collect_status(
            run_dir=run_dir,
            ports=ports,
            auth_token=auth_token,
            server_data_dir=server_data_dir,
            client_data_dir=client_data_dir,
            transcript=transcript,
        )
        report_path = run_dir / "report.json"
        report_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        _print_summary(status)
        return report_path
    finally:
        _shutdown_child(ports.bot_b_control, auth_token)
        _shutdown_child(ports.bot_a_control, auth_token)
        _stop_processes(processes)
        for handle in log_handles:
            handle.close()


def _drive_conversation(
    ports: Ports,
    auth_token: str,
    processes: Iterable[subprocess.Popen[str]],
    log_paths: Iterable[Path],
    opening_topic: str,
) -> list[dict[str, Any]]:
    transcript: list[dict[str, Any]] = []
    opening = opening_topic.strip() or DEFAULT_OPENING_TOPIC
    event = _broadcast_message(
        ports,
        auth_token,
        sender_id="local-narrator",
        sender_name="旁白",
        text=opening,
        report_bot="a",
        is_bot=False,
    )
    transcript.append(event)
    print(f"[NodeSyncFlowTest] 旁白: {opening}")

    for bot_label in ["a", "b", "a", "b"]:
        event = _request_bot_reply(ports, auth_token, bot_label)
        transcript.append(event)
        _mirror_event(ports, auth_token, event, except_bot=bot_label)
        print(f"[NodeSyncFlowTest] {event['sender_name']}: {event['plain_text']}")
        _ensure_processes_alive(processes, log_paths)
        time.sleep(0.8)
    return transcript


def _trigger_stagnation(
    ports: Ports,
    auth_token: str,
    processes: Iterable[subprocess.Popen[str]],
    log_paths: Iterable[Path],
    transcript: list[dict[str, Any]],
    server_data_dir: Path,
    stagnation_mode: str,
) -> None:
    server_db = server_data_dir / "nodesync.sqlite3"
    for index, text in enumerate(_stagnation_messages(stagnation_mode, second_round=False)):
        event = _broadcast_message(
            ports,
            auth_token,
            sender_id=f"stuck-user-{index}",
            sender_name="围观者",
            text=text,
            report_bot="a",
            is_bot=False,
        )
        transcript.append(event)
        time.sleep(0.2)

    try:
        _post_json(
            f"http://127.0.0.1:{ports.nodesync}/streams/{STREAM_ID}/analyze",
            token=auth_token,
            payload={},
            timeout=5,
        )
    except Exception as exc:
        print(f"[NodeSyncFlowTest] 手动 analyze 未同步返回，继续等待异步分析结果: {exc}")
    _wait_until_optional(
        lambda: _has_terminal_directive(server_db),
        "NodeSync 完成至少一条介入指令",
        processes=processes,
        log_paths=log_paths,
        timeout_seconds=180,
    )
    if _has_terminal_directive_type(server_db, "advance_dialogue"):
        return

    for index, text in enumerate(_stagnation_messages(stagnation_mode, second_round=True)):
        event = _broadcast_message(
            ports,
            auth_token,
            sender_id=f"still-stuck-user-{index}",
            sender_name="围观者",
            text=text,
            report_bot="a",
            is_bot=False,
        )
        transcript.append(event)
        _ensure_processes_alive(processes, log_paths)
        time.sleep(0.2)
    try:
        _post_json(
            f"http://127.0.0.1:{ports.nodesync}/streams/{STREAM_ID}/analyze",
            token=auth_token,
            payload={},
            timeout=5,
        )
    except Exception as exc:
        print(f"[NodeSyncFlowTest] 二次 analyze 未同步返回，继续等待 advance_dialogue: {exc}")
    _wait_until_optional(
        lambda: _has_terminal_directive_type(server_db, "advance_dialogue"),
        "NodeSync 完成 advance_dialogue 指令",
        processes=processes,
        log_paths=log_paths,
        timeout_seconds=240,
    )


def _stagnation_messages(stagnation_mode: str, *, second_round: bool) -> tuple[str, ...]:
    if stagnation_mode == "action-overload":
        return ACTION_OVERLOAD_SECONDARY_MESSAGES if second_round else ACTION_OVERLOAD_MESSAGES
    if second_round:
        return tuple("还是没变化。" for _ in range(12))
    return tuple("嗯。" for _ in range(9))


def _broadcast_message(
    ports: Ports,
    auth_token: str,
    sender_id: str,
    sender_name: str,
    text: str,
    report_bot: str,
    is_bot: bool,
) -> dict[str, Any]:
    event = {
        "stream_id": STREAM_ID,
        "message_id": f"flow-{uuid4().hex}",
        "sender_id": sender_id,
        "sender_name": sender_name,
        "plain_text": text,
        "timestamp": time.time(),
        "is_bot": is_bot,
    }
    for bot_label, port in {"a": ports.bot_a_flow, "b": ports.bot_b_flow}.items():
        payload = {**event, "report": bot_label == report_bot}
        _post_json(f"http://127.0.0.1:{port}/messages", token=auth_token, payload=payload)
    return event


def _mirror_event(ports: Ports, auth_token: str, event: dict[str, Any], except_bot: str) -> None:
    target_port = ports.bot_b_flow if except_bot == "a" else ports.bot_a_flow
    _post_json(f"http://127.0.0.1:{target_port}/messages", token=auth_token, payload={**event, "report": False})


def _request_bot_reply(ports: Ports, auth_token: str, bot_label: str) -> dict[str, Any]:
    port = ports.bot_a_control if bot_label == "a" else ports.bot_b_control
    payload = _post_json(f"http://127.0.0.1:{port}/reply", token=auth_token, payload={"stream_id": STREAM_ID}, timeout=120)
    event = dict(payload["event"])
    return event


def _collect_status(
    run_dir: Path,
    ports: Ports,
    auth_token: str,
    server_data_dir: Path,
    client_data_dir: Path,
    transcript: list[dict[str, Any]],
) -> dict[str, Any]:
    bots = _http_json(f"http://127.0.0.1:{ports.nodesync}/bots", token=auth_token)
    server_db = server_data_dir / "nodesync.sqlite3"
    bot_a_history = _http_json(f"http://127.0.0.1:{ports.bot_a_flow}/messages/{STREAM_ID}", token=auth_token).get(
        "messages", []
    )
    bot_b_history = _http_json(f"http://127.0.0.1:{ports.bot_b_flow}/messages/{STREAM_ID}", token=auth_token).get(
        "messages", []
    )
    return {
        "ok": True,
        "run_dir": str(run_dir),
        "stream_id": STREAM_ID,
        "bots": bots,
        "transcript": transcript,
        "bot_a_history_count": len(bot_a_history),
        "bot_b_history_count": len(bot_b_history),
        "bot_a_history_tail": bot_a_history[-8:],
        "bot_b_history_tail": bot_b_history[-8:],
        "server_sqlite": {
            "messages": _message_count(server_db, STREAM_ID),
            "scenes": _scene_rows(server_db),
            "directives": _directive_rows(server_db),
        },
        "client_injections": _read_jsonl(client_data_dir / "context_injections.jsonl")[-8:],
        "bot_a_llm_calls": _read_jsonl(run_dir / f"{BOT_A_ID}_llm.jsonl")[-8:],
        "bot_b_llm_calls": _read_jsonl(run_dir / f"{BOT_B_ID}_llm.jsonl")[-8:],
    }


def _print_summary(status: dict[str, Any]) -> None:
    live = status["bots"].get("live", [])
    sqlite_status = status["server_sqlite"]
    directives = sqlite_status["directives"]
    print("[NodeSyncFlowTest] NodeSync live bots:", ", ".join(live))
    print("[NodeSyncFlowTest] SQLite messages:", sqlite_status["messages"])
    print("[NodeSyncFlowTest] scenes:", len(sqlite_status["scenes"]))
    print("[NodeSyncFlowTest] directives:", len(directives))
    for directive in directives[-5:]:
        payload = directive.get("payload", {})
        print(
            "[NodeSyncFlowTest] directive:",
            payload.get("directive_type"),
            directive.get("status"),
            payload.get("target_bot_id"),
        )


def _run_child(options: ChildOptions) -> int:
    try:
        asyncio.run(_child_main(options))
    except Exception as exc:
        error_path = options.run_dir / f"{options.bot_id}.error.json"
        error_path.write_text(json.dumps({"error": repr(exc)}, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    return 0


async def _child_main(options: ChildOptions) -> None:
    sys.path.insert(0, str(NODESYNC_ROOT))
    sys.path.insert(0, str(MAIBOT_10_ROOT))

    from src.plugin_runtime.runner.plugin_loader import PluginLoader

    _install_workspace_model_config(options.run_dir / f"{options.bot_id}_config")

    plugin_root = options.run_dir / f"{options.bot_id}_plugins"
    plugin_dir = plugin_root / "nodesync_coordinator"
    if plugin_dir.exists():
        shutil.rmtree(plugin_dir)
    shutil.copytree(NODESYNC_ROOT / "nodesync" / "adapters" / "maibot_10", plugin_dir)

    loader = PluginLoader(host_version="1.0.0-pre.19")
    loaded = loader.discover_and_load([str(plugin_root)])
    if len(loaded) != 1:
        raise RuntimeError(f"NodeSync 插件加载失败: {loader.failed_plugins}")
    meta = loaded[0]
    instance = meta.instance
    module = sys.modules[meta.module_name]

    context = _ChildContext(options, instance)
    config = module.NodeSync10PluginConfig.model_validate(
        {
            "plugin": {"enabled": True, "config_version": "0.1.0"},
            "nodesync": {
                "mode": options.mode,
                "bot_id": options.bot_id,
                "bot_name": options.bot_name,
                "reply_persona": options.persona,
                "auth_token": options.auth_token,
                "server_host": "127.0.0.1",
                "server_port": options.nodesync_port,
                "server_url": f"http://127.0.0.1:{options.nodesync_port}",
                "data_dir": str(options.data_dir),
                "streams": [options.stream_id],
            },
            "local_flow": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": options.local_flow_port,
                "auth_token": options.auth_token,
                "stream_id": options.stream_id,
                "capture_outbound": True,
                "context_limit": 80,
                "max_messages": 500,
            },
            "policy": {
                "enable_llm_decision": True,
                "enable_llm_advance_generation": True,
                "llm_worker_bot_id": options.llm_worker_bot_id,
                "llm_decision_timeout_seconds": 90,
                "llm_generation_timeout_seconds": 90,
                "intervention_cooldown_seconds": 0,
                "directive_retry_interval_seconds": 60,
                "directive_max_attempts": 2,
                "analysis_min_messages": 4,
                "analysis_window_messages": 8,
            },
        }
    )
    if hasattr(instance, "set_plugin_config"):
        instance.set_plugin_config(config.model_dump(mode="json"))
    else:
        instance.config = config
    instance._set_context(context)
    await instance.on_load()

    stop_event = asyncio.Event()
    app = web.Application(client_max_size=128 * 1024)
    app.add_routes(
        [
            web.get("/health", _auth_handler(options, lambda _request: _control_health(instance, options))),
            web.post("/reply", _auth_handler(options, lambda request: _control_reply(request, instance, context, options))),
            web.post("/shutdown", _auth_handler(options, lambda _request: _control_shutdown(stop_event))),
        ]
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", options.control_port)
    await site.start()
    (options.run_dir / f"{options.bot_id}.ready.json").write_text(
        json.dumps({"ok": True, "bot_id": options.bot_id}, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        await stop_event.wait()
    finally:
        await runner.cleanup()
        await instance.on_unload()


class _ChildContext:
    """提供给真实 NodeSync 1.0 adapter 的 SDK 能力集合。"""

    def __init__(self, options: ChildOptions, plugin_instance: Any) -> None:
        self.options = options
        self.plugin_instance = plugin_instance
        self.message = _ChildMessageCapability(self)
        self.llm = _ChildLLMCapability(options)
        self.send = _ChildSendCapability(options)


class _ChildMessageCapability:
    def __init__(self, context: _ChildContext) -> None:
        self.context = context

    async def get_recent(self, stream_id: str, limit: int = 40) -> list[dict[str, Any]]:
        hub = getattr(self.context.plugin_instance, "_local_flow_hub", None)
        if hub is None:
            return []
        return [event.to_dict() for event in hub.get_recent(stream_id, limit)]


class _ChildLLMCapability:
    def __init__(self, options: ChildOptions) -> None:
        self.options = options

    async def generate(self, **kwargs: Any) -> dict[str, Any]:
        from src.services import llm_service

        prompt = str(kwargs.get("prompt") or "")
        max_tokens = min(int(kwargs.get("max_tokens") or 240), 300)
        result = await llm_service.generate(
            llm_service.LLMServiceRequest(
                task_name="utils",
                request_type="nodesync.local_chat_flow",
                prompt=prompt,
                temperature=kwargs.get("temperature", 0.45),
                max_tokens=max_tokens,
            )
        )
        payload = result.to_capability_payload()
        _append_jsonl(
            self.options.run_dir / f"{self.options.bot_id}_llm.jsonl",
            {
                "success": bool(payload.get("success")),
                "model_name": str(payload.get("model_name", "")),
                "response_excerpt": str(payload.get("response", ""))[:240],
                "error_excerpt": str(payload.get("error", ""))[:240] if payload.get("error") else "",
                "total_tokens": int(payload.get("total_tokens") or 0),
            },
        )
        return payload


class _ChildSendCapability:
    def __init__(self, options: ChildOptions) -> None:
        self.options = options

    async def text(self, content: str, stream_id: str) -> bool:
        _append_jsonl(
            self.options.run_dir / f"{self.options.bot_id}_send.jsonl",
            {"stream_id": stream_id, "content": content, "created_at": time.time()},
        )
        return True


def _auth_handler(options: ChildOptions, handler: Callable[[web.Request], Any]) -> Callable[[web.Request], Any]:
    async def wrapped(request: web.Request) -> web.Response:
        if not _request_auth_ok(request, options.auth_token):
            return web.json_response({"error": "unauthorized"}, status=401)
        result = handler(request)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    return wrapped


def _control_health(instance: Any, options: ChildOptions) -> web.Response:
    runtime = getattr(instance, "_runtime", None)
    client = getattr(runtime, "client", None) if runtime is not None else None
    ws = getattr(client, "_ws", None) if client is not None else None
    return web.json_response(
        {
            "ok": True,
            "bot_id": options.bot_id,
            "mode": options.mode,
            "ws_connected": bool(ws is not None and not ws.closed),
        }
    )


async def _control_reply(
    request: web.Request,
    instance: Any,
    context: _ChildContext,
    options: ChildOptions,
) -> web.Response:
    from nodesync.shared.models import ChatMessageEvent
    from nodesync.shared.time_utils import now_ts

    payload = await request.json()
    stream_id = str(payload.get("stream_id") or options.stream_id)
    hub = getattr(instance, "_local_flow_hub", None)
    if hub is None:
        return web.json_response({"error": "local_flow hub unavailable"}, status=500)
    history = [event.to_dict() for event in hub.get_recent(stream_id, 20)]
    prompt = _build_natural_reply_prompt(options, history)
    llm_payload = await context.llm.generate(prompt=prompt, temperature=0.55, max_tokens=220)
    if not llm_payload.get("success"):
        return web.json_response({"error": llm_payload.get("error", "LLM 生成失败")}, status=500)
    text = _clean_reply(str(llm_payload.get("response", "")))
    event = ChatMessageEvent(
        stream_id=stream_id,
        message_id=f"flow-{options.bot_id}-{uuid4().hex}",
        sender_id=options.bot_id,
        sender_name=options.bot_name,
        plain_text=text,
        timestamp=now_ts(),
        is_bot=True,
    )
    reported = await hub.ingest(event)
    return web.json_response({"ok": True, "reported": reported, "event": event.to_dict()})


async def _control_shutdown(stop_event: asyncio.Event) -> web.Response:
    stop_event.set()
    return web.json_response({"ok": True})


def _build_natural_reply_prompt(options: ChildOptions, history: list[dict[str, Any]]) -> str:
    rendered = "\n".join(
        f"- {item.get('sender_name') or item.get('sender_id')}: {str(item.get('plain_text', '')).strip()[:180]}"
        for item in history[-16:]
    )
    return (
        f"你正在参加一个本地群聊 RP 联调。你的名字是{options.bot_name}。\n"
        f"人设：{options.persona}\n"
        "任务：根据聊天记录自然回复一条中文群聊消息。\n"
        "要求：只输出最终发言；1到2句；不要解释；不要自称模型；不要重复上一句；"
        "保持当前场景，并轻轻推动当前内容进入下一步。\n\n"
        f"聊天记录：\n{rendered or '- 暂无'}\n"
    )


def _clean_reply(raw: str) -> str:
    text = raw.strip().strip("`").strip()
    for prefix in ("回复：", "发言：", "消息：", "最终消息："):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    text = " ".join(lines)
    if not text:
        return "我先把线索归一下：门、符文和低声重复的人名应该是同一个机关的一部分。"
    return text[:260]


def _install_workspace_model_config(temp_config_dir: Path) -> None:
    from src.config import config as cfg

    temp_config_dir.mkdir(parents=True, exist_ok=True)
    bot_copy = temp_config_dir / "bot_config.toml"
    model_copy = temp_config_dir / "model_config.toml"
    shutil.copy2(ROOT_BOT_CONFIG, bot_copy)
    shutil.copy2(ROOT_MODEL_CONFIG, model_copy)
    cfg.config_manager.global_config, _ = cfg.load_config_from_file(cfg.Config, bot_copy, cfg.CONFIG_VERSION)
    cfg.config_manager.model_config, _ = cfg.load_config_from_file(
        cfg.ModelConfig,
        model_copy,
        cfg.MODEL_CONFIG_VERSION,
        True,
    )


def _start_child(
    uv_path: str,
    run_dir: Path,
    log_path: Path,
    options: ChildOptions,
    log_handles: list[Any],
) -> subprocess.Popen[str]:
    handle = log_path.open("w", encoding="utf-8")
    log_handles.append(handle)
    command = [
        uv_path,
        "run",
        "python",
        "-u",
        str(Path(__file__).resolve()),
        "--child",
        "--run-dir",
        str(options.run_dir),
        "--bot-id",
        options.bot_id,
        "--bot-name",
        options.bot_name,
        "--persona",
        options.persona,
        "--mode",
        options.mode,
        "--auth-token",
        options.auth_token,
        "--nodesync-port",
        str(options.nodesync_port),
        "--local-flow-port",
        str(options.local_flow_port),
        "--control-port",
        str(options.control_port),
        "--stream-id",
        options.stream_id,
        "--data-dir",
        str(options.data_dir),
        "--llm-worker-bot-id",
        options.llm_worker_bot_id,
    ]
    env = os.environ.copy()
    entries = [str(NODESYNC_ROOT), str(MAIBOT_10_ROOT)]
    if env.get("PYTHONPATH"):
        entries.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(entries)
    env["PYTHONUTF8"] = "1"
    return subprocess.Popen(
        command,
        cwd=str(MAIBOT_10_ROOT),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _shutdown_child(port: int, token: str) -> None:
    with contextlib.suppress(Exception):
        _post_json(f"http://127.0.0.1:{port}/shutdown", token=token, payload={}, timeout=3)


def _validate_environment() -> None:
    if not MAIBOT_10_ROOT.exists():
        raise LocalChatFlowError(f"缺少 MaiBot 1.0 目录: {MAIBOT_10_ROOT}")
    if not ROOT_BOT_CONFIG.exists():
        raise LocalChatFlowError(f"缺少 bot_config.toml: {ROOT_BOT_CONFIG}")
    if not ROOT_MODEL_CONFIG.exists():
        raise LocalChatFlowError(f"缺少 model_config.toml: {ROOT_MODEL_CONFIG}")


def _required_path(value: Path | None, name: str) -> Path:
    if value is None:
        raise LocalChatFlowError(f"缺少 {name}")
    return value


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
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        _ensure_processes_alive(processes, log_paths)
        try:
            if predicate():
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise LocalChatFlowError(f"等待 {label} 超时\n{_logs_tail(log_paths)}")


def _wait_until_optional(
    predicate: Callable[[], bool],
    label: str,
    *,
    processes: Iterable[subprocess.Popen[str]],
    log_paths: Iterable[Path],
    timeout_seconds: float,
) -> bool:
    try:
        _wait_until(
            predicate,
            label,
            processes=processes,
            log_paths=log_paths,
            timeout_seconds=timeout_seconds,
        )
        return True
    except LocalChatFlowError as exc:
        print(f"[NodeSyncFlowTest] 警告: {exc}")
        return False


def _ensure_processes_alive(processes: Iterable[subprocess.Popen[str]], log_paths: Iterable[Path]) -> None:
    dead = [process for process in processes if process.poll() is not None]
    if dead:
        statuses = ", ".join(f"pid={process.pid} returncode={process.poll()}" for process in dead)
        raise LocalChatFlowError(f"子进程提前退出: {statuses}\n{_logs_tail(log_paths)}")


def _stop_processes(processes: list[subprocess.Popen[str]]) -> None:
    for process in processes:
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.terminate()
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _http_json(url: str, token: str = "", timeout: int = 8) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=_headers(token))
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 本地联调地址
        return dict(json.loads(response.read().decode("utf-8")))


def _post_json(url: str, token: str, payload: dict[str, Any], timeout: int = 15) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={**_headers(token), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 本地联调地址
            return dict(json.loads(response.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise LocalChatFlowError(f"{url} 返回 HTTP {exc.code}: {body}") from exc


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _request_auth_ok(request: web.Request, token: str) -> bool:
    header = request.headers.get("Authorization", "")
    return header == f"Bearer {token}"


def _message_count(db_path: Path, stream_id: str) -> int:
    if not db_path.exists():
        return 0
    with sqlite3.connect(str(db_path), timeout=5) as conn:
        row = conn.execute("SELECT COUNT(*) FROM messages WHERE stream_id = ?", (stream_id,)).fetchone()
    return int(row[0]) if row else 0


def _scene_rows(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    with sqlite3.connect(str(db_path), timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT payload_json, status FROM scenes ORDER BY updated_at").fetchall()
    return [{"payload": json.loads(str(row["payload_json"])), "status": str(row["status"])} for row in rows]


def _directive_rows(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    with sqlite3.connect(str(db_path), timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT payload_json, status FROM directives ORDER BY updated_at").fetchall()
    return [{"payload": json.loads(str(row["payload_json"])), "status": str(row["status"])} for row in rows]


def _has_terminal_directive(db_path: Path) -> bool:
    """检查是否已经至少完成一条介入指令。"""

    return any(row["status"] in TERMINAL_DIRECTIVE_STATUSES for row in _directive_rows(db_path))


def _has_terminal_directive_type(db_path: Path, directive_type: str) -> bool:
    """检查指定类型的指令是否已经完成。"""

    return any(
        row["status"] in TERMINAL_DIRECTIVE_STATUSES and row["payload"].get("directive_type") == directive_type
        for row in _directive_rows(db_path)
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        with contextlib.suppress(json.JSONDecodeError):
            items.append(dict(json.loads(line)))
    return items


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _logs_tail(log_paths: Iterable[Path], max_chars: int = 8000) -> str:
    parts = []
    for path in log_paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        parts.append(f"--- {path.name} ---\n{text[-max_chars:]}")
    return "\n".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
