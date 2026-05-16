"""NodeSync 本地构造消息流交互脚本。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class DriverOptions:
    """交互脚本运行参数。"""

    base_url: str
    token: str
    stream_id: str
    sender_id: str
    sender_name: str


HELP_TEXT = """可用命令：
  直接输入文本              作为本地用户发言
  /topic 文本               发送“新话题：文本”
  /rp 文本                  发送“RP 场景：文本”
  /say 名字: 文本           以指定名字发言
  /bot 名字: 文本           以 bot 身份补一条本地发言
  /history                  查看当前 stream 最近消息
  /help                     查看帮助
  /quit                     退出
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="向已启用 local_flow 的 NodeSync adapter 喂入本地消息。")
    parser.add_argument("--url", default="http://127.0.0.1:8788", help="local_flow 地址")
    parser.add_argument(
        "--token",
        default=os.environ.get("NODESYNC_LOCAL_FLOW_TOKEN", ""),
        help="local_flow token；也可用环境变量 NODESYNC_LOCAL_FLOW_TOKEN",
    )
    parser.add_argument("--stream", default="local-flow", help="构造消息使用的 stream_id")
    parser.add_argument("--sender-id", default="local-user", help="默认发送者 ID")
    parser.add_argument("--sender-name", default="本地用户", help="默认发送者显示名")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    options = DriverOptions(
        base_url=args.url.rstrip("/"),
        token=args.token,
        stream_id=args.stream,
        sender_id=args.sender_id,
        sender_name=args.sender_name,
    )
    print("NodeSync 本地信息流交互模式")
    print(f"local_flow: {options.base_url}  stream: {options.stream_id}")
    print("输入 /help 查看命令，输入 /quit 退出。")
    if not _health_ok(options):
        print("无法访问 local_flow /health，请确认对应 bot 进程已启动且 local_flow.enabled=true。", file=sys.stderr)
        return 1

    while True:
        try:
            raw = input("NodeSync> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not raw:
            continue
        if raw in {"/quit", "/exit"}:
            return 0
        if raw == "/help":
            print(HELP_TEXT)
            continue
        try:
            if raw == "/history":
                _print_history(options)
            else:
                payload = _payload_from_line(raw, options)
                response = _post_json(options, "/messages", payload)
                event = response.get("event", {})
                print(f"已发送: {event.get('sender_name')} -> {event.get('plain_text')}")
        except LocalFlowDriverError as exc:
            print(f"发送失败: {exc}", file=sys.stderr)


class LocalFlowDriverError(RuntimeError):
    """交互脚本可读错误。"""


def _health_ok(options: DriverOptions) -> bool:
    try:
        with urllib.request.urlopen(options.base_url + "/health", timeout=5) as response:  # noqa: S310 - 用户指定本地调试地址
            payload = json.loads(response.read().decode("utf-8"))
        return bool(payload.get("ok"))
    except Exception:
        return False


def _payload_from_line(raw: str, options: DriverOptions) -> dict[str, Any]:
    sender_id = options.sender_id
    sender_name = options.sender_name
    is_bot = False
    text = raw
    if raw.startswith("/topic "):
        text = "新话题：" + raw[len("/topic ") :].strip()
    elif raw.startswith("/rp "):
        text = "RP 场景：" + raw[len("/rp ") :].strip()
    elif raw.startswith("/say "):
        sender_name, text = _split_named_text(raw[len("/say ") :].strip(), sender_name)
        sender_id = "local-" + sender_name
    elif raw.startswith("/bot "):
        sender_name, text = _split_named_text(raw[len("/bot ") :].strip(), "本地Bot")
        sender_id = "bot-" + sender_name
        is_bot = True
    elif raw.startswith("/"):
        raise LocalFlowDriverError("未知命令，输入 /help 查看可用命令")
    if not text.strip():
        raise LocalFlowDriverError("消息内容不能为空")
    return {
        "stream_id": options.stream_id,
        "message_id": f"local-flow-{uuid4().hex}",
        "sender_id": sender_id,
        "sender_name": sender_name,
        "plain_text": text.strip(),
        "timestamp": time.time(),
        "is_bot": is_bot,
    }


def _split_named_text(raw: str, default_name: str) -> tuple[str, str]:
    for separator in (":", "："):
        if separator in raw:
            name, text = raw.split(separator, 1)
            return name.strip() or default_name, text.strip()
    return default_name, raw


def _print_history(options: DriverOptions) -> None:
    payload = _get_json(options, f"/messages/{options.stream_id}?limit=20")
    messages = payload.get("messages", [])
    if not messages:
        print("当前 stream 还没有本地构造消息。")
        return
    for item in messages:
        marker = "bot" if item.get("is_bot") else "user"
        print(f"[{marker}] {item.get('sender_name')}: {item.get('plain_text')}")


def _post_json(options: DriverOptions, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        options.base_url + path,
        data=data,
        method="POST",
        headers=_headers(options),
    )
    return _open_json(request)


def _get_json(options: DriverOptions, path: str) -> dict[str, Any]:
    request = urllib.request.Request(options.base_url + path, headers=_headers(options))
    return _open_json(request)


def _headers(options: DriverOptions) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if options.token:
        headers["Authorization"] = f"Bearer {options.token}"
    return headers


def _open_json(request: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - 用户指定本地调试地址
            return dict(json.loads(response.read().decode("utf-8")))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise LocalFlowDriverError(f"HTTP {exc.code}: {body}") from exc
    except Exception as exc:
        raise LocalFlowDriverError(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
