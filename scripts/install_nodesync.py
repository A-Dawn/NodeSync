"""NodeSync 跨平台安装脚本。

该脚本只负责安装准备工作，不会在 MaiBot 插件运行入口里执行 pip 或复制文件。
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))


class InstallerError(RuntimeError):
    """安装流程中的可预期错误。"""


@dataclass(frozen=True, slots=True)
class PluginSpec:
    """MaiBot 版本对应的 adapter 安装规格。"""

    version_label: str
    adapter_dir_name: str
    default_plugin_dir_name: str
    plugin_id: str


@dataclass(slots=True)
class InstallOptions:
    """安装选项，供命令行和测试共同使用。"""

    maibot_version: str
    maibot_root: Path | None = None
    plugins_dir: Path | None = None
    mode: str = "server"
    bot_id: str = ""
    bot_name: str = ""
    auth_token: str = ""
    server_host: str = ""
    server_port: int = 8765
    server_url: str = ""
    data_dir: str = "data/nodesync"
    streams: list[str] = field(default_factory=list)
    llm_worker_bot_id: str = ""
    enable_llm_decision: bool = True
    enable_llm_advance_generation: bool = True
    intervention_cooldown_seconds: int = 300
    analysis_min_messages: int = 4
    analysis_window_messages: int = 8
    plugin_dir_name: str = ""
    force: bool = False
    backup_existing: bool = True
    skip_config: bool = False
    overwrite_config: bool = False
    dry_run: bool = False
    install_package: bool = False
    python_executable: str = sys.executable
    uv_project: Path | None = None
    nodesync_root: Path = field(default_factory=lambda: Path(__file__).resolve().parents[1])


@dataclass(frozen=True, slots=True)
class InstallResult:
    """安装结果。"""

    spec: PluginSpec
    plugins_dir: Path
    target_dir: Path
    config_path: Path
    auth_token: str
    generated_token: bool
    package_install_command: list[str] = field(default_factory=list)


PLUGIN_SPECS = {
    "012": PluginSpec(
        version_label="0.12.2",
        adapter_dir_name="maibot_012",
        default_plugin_dir_name="nodesync",
        plugin_id="nodesync",
    ),
    "0.12": PluginSpec(
        version_label="0.12.2",
        adapter_dir_name="maibot_012",
        default_plugin_dir_name="nodesync",
        plugin_id="nodesync",
    ),
    "0.12.2": PluginSpec(
        version_label="0.12.2",
        adapter_dir_name="maibot_012",
        default_plugin_dir_name="nodesync",
        plugin_id="nodesync",
    ),
    "10": PluginSpec(
        version_label="1.0",
        adapter_dir_name="maibot_10",
        default_plugin_dir_name="nodesync_coordinator",
        plugin_id="nodesync.coordinator",
    ),
    "1.0": PluginSpec(
        version_label="1.0",
        adapter_dir_name="maibot_10",
        default_plugin_dir_name="nodesync_coordinator",
        plugin_id="nodesync.coordinator",
    ),
}


def install(options: InstallOptions) -> InstallResult:
    """执行 NodeSync 安装。

    Args:
        options: 安装参数。

    Returns:
        InstallResult: 安装后的关键路径和 token 信息。

    Raises:
        InstallerError: 参数不合法、目标冲突或文件操作失败。
    """

    spec = resolve_spec(options.maibot_version)
    nodesync_root = options.nodesync_root.resolve()
    source_dir = nodesync_root / "nodesync" / "adapters" / spec.adapter_dir_name
    _validate_adapter_source(source_dir)

    plugins_dir = resolve_plugins_dir(options).resolve()
    target_dir = (plugins_dir / (options.plugin_dir_name or spec.default_plugin_dir_name)).resolve()
    _ensure_safe_target(plugins_dir, target_dir)
    if _same_or_nested(source_dir.resolve(), target_dir):
        raise InstallerError("目标插件目录不能指向 NodeSync 源码中的 adapter 目录。")

    package_command = _install_package(options, nodesync_root) if options.install_package else []

    _copy_adapter(source_dir, plugins_dir, target_dir, options)
    _write_prompt_templates_if_needed(target_dir, options)

    token, generated_token = _resolve_auth_token(options.auth_token)
    config_path = target_dir / "config.toml"
    if not options.skip_config:
        _write_config_if_needed(config_path, spec, options, token)

    return InstallResult(
        spec=spec,
        plugins_dir=plugins_dir,
        target_dir=target_dir,
        config_path=config_path,
        auth_token=token,
        generated_token=generated_token,
        package_install_command=package_command,
    )


def resolve_spec(raw_version: str) -> PluginSpec:
    """解析用户输入的 MaiBot 版本。"""

    normalized = raw_version.strip().lower().replace("maibot", "").replace("v", "").replace("-", "").replace("_", "")
    spec = PLUGIN_SPECS.get(normalized)
    if spec is None:
        raise InstallerError("不支持的 MaiBot 版本，请使用 0.12.2 或 1.0。")
    return spec


def resolve_plugins_dir(options: InstallOptions) -> Path:
    """解析第三方插件根目录。"""

    if options.maibot_root is None and options.plugins_dir is None:
        raise InstallerError("必须提供 --maibot-root 或 --plugins-dir。")
    if options.maibot_root is not None and options.plugins_dir is not None:
        raise InstallerError("--maibot-root 和 --plugins-dir 只能二选一。")
    if options.plugins_dir is not None:
        return options.plugins_dir
    if options.maibot_root is None:
        raise InstallerError("缺少 MaiBot 根目录。")
    return options.maibot_root / "plugins"


def detect_maibot_version(maibot_root: Path) -> str | None:
    """根据 MaiBot 根目录结构识别版本。"""

    root = maibot_root.resolve()
    if (root / "src" / "plugin_runtime" / "runner" / "plugin_loader.py").is_file():
        return "1.0"
    if (root / "src" / "plugin_system" / "core" / "plugin_manager.py").is_file():
        return "0.12.2"

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8", errors="ignore")
        if "maibot-plugin-sdk" in text:
            return "1.0"
    return None


def _looks_like_plugins_dir(path: Path) -> bool:
    """判断用户给的是 MaiBot 根目录还是插件根目录。"""

    normalized_parts = [part.lower() for part in path.parts]
    if path.name.lower() == "plugins":
        return True
    return len(normalized_parts) >= 3 and normalized_parts[-3:] == ["data", "maimbot", "plugins"]


def _install_package(options: InstallOptions, nodesync_root: Path) -> list[str]:
    if options.uv_project is not None:
        command = ["uv", "pip", "install", "-e", str(nodesync_root)]
        cwd = options.uv_project
    else:
        command = [options.python_executable, "-m", "pip", "install", "-e", str(nodesync_root)]
        cwd = None

    if options.dry_run:
        print("[NodeSyncInstaller] dry-run: " + " ".join(command))
        return command

    completed = subprocess.run(command, cwd=cwd, check=False)  # noqa: S603
    if completed.returncode != 0:
        raise InstallerError("NodeSync Python 包安装失败，请检查 Python/uv 环境。")
    return command


def _copy_adapter(source_dir: Path, plugins_dir: Path, target_dir: Path, options: InstallOptions) -> None:
    if options.dry_run:
        print(f"[NodeSyncInstaller] dry-run: 创建插件根目录 {plugins_dir}")
        print(f"[NodeSyncInstaller] dry-run: 复制 {source_dir} -> {target_dir}")
        return

    plugins_dir.mkdir(parents=True, exist_ok=True)
    if target_dir.exists():
        if not options.force:
            raise InstallerError(f"目标插件目录已存在: {target_dir}。如需替换，请加 --force。")
        if options.backup_existing:
            backup_dir = _next_backup_path(target_dir)
            shutil.move(str(target_dir), str(backup_dir))
            print(f"[NodeSyncInstaller] 已备份旧插件目录: {backup_dir}")
        else:
            _ensure_safe_target(plugins_dir, target_dir)
            shutil.rmtree(target_dir)
    shutil.copytree(source_dir, target_dir)
    print(f"[NodeSyncInstaller] 已复制 adapter: {target_dir}")


def _write_config_if_needed(config_path: Path, spec: PluginSpec, options: InstallOptions, token: str) -> None:
    if options.dry_run:
        print(f"[NodeSyncInstaller] dry-run: 写入配置 {config_path}")
        return
    if config_path.exists() and not options.overwrite_config:
        print(f"[NodeSyncInstaller] 配置已存在，未覆盖: {config_path}")
        return
    config_path.write_text(_build_config_text(spec, options, token), encoding="utf-8")
    print(f"[NodeSyncInstaller] 已写入配置: {config_path}")


def _write_prompt_templates_if_needed(target_dir: Path, options: InstallOptions) -> None:
    """在插件目录内预置默认 prompt 模板。"""

    from nodesync.shared.prompt_templates import ensure_prompt_templates

    prompt_dir = target_dir / "prompts"
    if options.dry_run:
        print(f"[NodeSyncInstaller] dry-run: 写入默认 prompt 模板 {prompt_dir}")
        return
    ensure_prompt_templates(prompt_dir)
    print(f"[NodeSyncInstaller] 已准备 prompt 模板目录: {prompt_dir}")


def _build_config_text(spec: PluginSpec, options: InstallOptions, token: str) -> str:
    mode = _validate_mode(options.mode)
    bot_id = options.bot_id.strip() or _default_bot_id(mode)
    bot_name = options.bot_name.strip() or _default_bot_name(spec, mode)
    server_host = options.server_host.strip() or ("127.0.0.1" if mode != "server" else "127.0.0.1")
    server_url = options.server_url.strip() or f"http://127.0.0.1:{options.server_port}"
    streams = options.streams

    plugin_section = (
        'name = "nodesync"\n'
        'version = "0.1.0"\n'
        "enabled = true\n"
        'description = "多 Bot 协作与按需推进插件"\n'
        if spec.version_label == "0.12.2"
        else 'enabled = true\nconfig_version = "0.1.0"\n'
    )

    return (
        "# NodeSync - 安装脚本生成的基础配置\n"
        "# 修改配置后，建议重启 MaiBot。\n\n"
        "[plugin]\n"
        f"{plugin_section}\n"
        "[nodesync]\n"
        f"mode = {_toml(mode)}\n"
        f"bot_id = {_toml(bot_id)}\n"
        f"bot_name = {_toml(bot_name)}\n"
        'reply_persona = ""\n'
        f"auth_token = {_toml(token)}\n"
        "allow_query_token_auth = false\n"
        f"server_host = {_toml(server_host)}\n"
        f"server_port = {options.server_port}\n"
        f"server_url = {_toml(server_url)}\n"
        "http_client_max_bytes = 1048576\n"
        "ws_max_msg_bytes = 1048576\n"
        f"data_dir = {_toml(options.data_dir)}\n"
        f"streams = {_toml_list(streams)}\n\n"
        "[prompts]\n"
        'directory = "prompts"\n\n'
        "[diagnostics]\n"
        "enabled = false\n"
        'output_dir = ""\n'
        "include_hashes = true\n"
        "max_depth = 4\n"
        "max_items = 40\n\n"
        "[local_flow]\n"
        "enabled = false\n"
        'host = "127.0.0.1"\n'
        "port = 8788\n"
        'auth_token = ""\n'
        'stream_id = "local-flow"\n'
        "capture_outbound = true\n"
        "context_limit = 80\n"
        "max_messages = 500\n\n"
        "[policy]\n"
        "context_limit = 40\n"
        "injection_history_limit = 5\n"
        f"enable_llm_decision = {_toml_bool(options.enable_llm_decision)}\n"
        f"enable_llm_advance_generation = {_toml_bool(options.enable_llm_advance_generation)}\n"
        f"llm_worker_bot_id = {_toml(options.llm_worker_bot_id)}\n"
        "llm_decision_timeout_seconds = 20\n"
        "llm_generation_timeout_seconds = 30\n"
        f"intervention_cooldown_seconds = {options.intervention_cooldown_seconds}\n"
        "directive_ttl_seconds = 900\n"
        "directive_retry_interval_seconds = 20\n"
        "directive_max_attempts = 3\n"
        "scene_idle_close_seconds = 3600\n"
        f"analysis_min_messages = {options.analysis_min_messages}\n"
        f"analysis_window_messages = {options.analysis_window_messages}\n"
    )


def _validate_adapter_source(source_dir: Path) -> None:
    if not source_dir.is_dir():
        raise InstallerError(f"找不到 adapter 目录: {source_dir}")
    for filename in ("plugin.py", "_manifest.json"):
        if not (source_dir / filename).is_file():
            raise InstallerError(f"adapter 目录缺少 {filename}: {source_dir}")


def _validate_mode(mode: str) -> str:
    normalized = mode.strip().lower()
    if normalized not in {"server", "client", "disabled"}:
        raise InstallerError("mode 只能是 server、client 或 disabled。")
    return normalized


def _resolve_auth_token(raw_token: str) -> tuple[str, bool]:
    token = raw_token.strip()
    if token:
        return token, False
    return secrets.token_hex(32), True


def _default_bot_id(mode: str) -> str:
    if mode == "server":
        return "nodesync-server"
    if mode == "client":
        return "nodesync-client"
    return "nodesync-disabled"


def _default_bot_name(spec: PluginSpec, mode: str) -> str:
    if mode == "server":
        return f"NodeSync Server {spec.version_label}"
    if mode == "client":
        return f"NodeSync Client {spec.version_label}"
    return f"NodeSync Disabled {spec.version_label}"


def _toml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def _toml_list(values: list[str]) -> str:
    return "[" + ", ".join(_toml(item) for item in values) + "]"


def _ensure_safe_target(plugins_dir: Path, target_dir: Path) -> None:
    plugins_root = plugins_dir.resolve()
    target = target_dir.resolve()
    try:
        target.relative_to(plugins_root)
    except ValueError as exc:
        raise InstallerError(f"目标目录必须位于插件根目录内: {target}") from exc


def _same_or_nested(source_dir: Path, target_dir: Path) -> bool:
    try:
        target_dir.relative_to(source_dir)
        return True
    except ValueError:
        return source_dir == target_dir


def _next_backup_path(target_dir: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = target_dir.with_name(f"{target_dir.name}.backup_{timestamp}")
    candidate = base
    counter = 1
    while candidate.exists():
        candidate = target_dir.with_name(f"{base.name}_{counter}")
        counter += 1
    return candidate


def _parse_streams(raw_streams: str) -> list[str]:
    return [item.strip() for item in raw_streams.split(",") if item.strip()]


def run_wizard(input_func: Callable[[str], str] = input) -> InstallOptions:
    """运行面向普通用户的交互式安装向导。"""

    print("NodeSync 安装向导")
    print("直接回车会使用括号里的默认值。")
    target = Path(_prompt_required(input_func, "请输入 MaiBot 根目录，或 Docker 挂载出的 plugins 目录")).expanduser()
    target_is_plugins_dir = _looks_like_plugins_dir(target)
    if not target_is_plugins_dir and not _looks_like_maibot_root(target):
        target_is_plugins_dir = _prompt_bool(input_func, "这个路径是 Docker/compose 挂载出的 plugins 目录吗？", False)

    maibot_root = None if target_is_plugins_dir else target
    plugins_dir = target if target_is_plugins_dir else None

    detected_version = detect_maibot_version(maibot_root) if maibot_root is not None else None
    maibot_version = detected_version or _prompt_choice(input_func, "请选择 MaiBot 版本", ["1.0", "0.12.2"], "1.0")
    if detected_version:
        print(f"已识别 MaiBot 版本: {detected_version}")

    mode = _prompt_choice(input_func, "请选择 NodeSync 模式", ["server", "client", "disabled"], "server")
    default_bot_id = _default_bot_id(mode)
    bot_id = _prompt(input_func, f"当前 bot 的唯一 ID [{default_bot_id}]", default_bot_id)
    default_bot_name = _default_bot_name(resolve_spec(maibot_version), mode)
    bot_name = _prompt(input_func, f"当前 bot 显示名 [{default_bot_name}]", default_bot_name)

    if mode == "client":
        auth_token = _prompt_required(input_func, "请输入 server 的 auth_token")
        server_url = _prompt(input_func, "请输入 server_url [http://127.0.0.1:8765]", "http://127.0.0.1:8765")
        server_host = "127.0.0.1"
    else:
        auth_token = _prompt(input_func, "请输入 auth_token，留空会自动生成强 token", "")
        exposed = target_is_plugins_dir or _prompt_bool(input_func, "是否需要其他机器或 Docker 容器连接这个 server？", False)
        server_host = "0.0.0.0" if mode == "server" and exposed else "127.0.0.1"
        server_url = _prompt(input_func, "server_url [http://127.0.0.1:8765]", "http://127.0.0.1:8765")

    install_package = _prompt_bool(input_func, "是否现在执行 pip install -e 安装 nodesync 包？", False)
    force = False
    try:
        spec = resolve_spec(maibot_version)
        current_plugins_dir = plugins_dir or (maibot_root / "plugins" if maibot_root is not None else None)
        if current_plugins_dir is not None:
            target_dir = current_plugins_dir / spec.default_plugin_dir_name
            if target_dir.exists():
                force = _prompt_bool(input_func, "目标插件目录已存在，是否替换并自动备份旧目录？", False)
    except InstallerError:
        force = False

    return InstallOptions(
        maibot_version=maibot_version,
        maibot_root=maibot_root,
        plugins_dir=plugins_dir,
        mode=mode,
        bot_id=bot_id,
        bot_name=bot_name,
        auth_token=auth_token,
        server_host=server_host,
        server_url=server_url,
        force=force,
        install_package=install_package,
    )


def _looks_like_maibot_root(path: Path) -> bool:
    return (path / "bot.py").is_file() or (path / "src").is_dir() or (path / "pyproject.toml").is_file()


def _prompt(input_func: Callable[[str], str], message: str, default: str = "") -> str:
    try:
        value = input_func(message + ": ")
    except EOFError as exc:
        raise InstallerError("无法读取交互输入，请改用参数模式运行。") from exc
    return value.strip() or default


def _prompt_required(input_func: Callable[[str], str], message: str) -> str:
    while True:
        value = _prompt(input_func, message)
        if value:
            return value
        print("这个值不能为空。")


def _prompt_choice(
    input_func: Callable[[str], str],
    message: str,
    choices: list[str],
    default: str,
) -> str:
    normalized_choices = {choice.lower(): choice for choice in choices}
    while True:
        raw_value = _prompt(input_func, f"{message} ({'/'.join(choices)}) [{default}]", default)
        value = normalized_choices.get(raw_value.lower())
        if value is not None:
            return value
        print(f"请输入以下值之一：{', '.join(choices)}")


def _prompt_bool(input_func: Callable[[str], str], message: str, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        raw_value = _prompt(input_func, f"{message} [{suffix}]", "yes" if default else "no").lower()
        if raw_value in {"y", "yes", "1", "true", "是"}:
            return True
        if raw_value in {"n", "no", "0", "false", "否"}:
            return False
        print("请输入 y 或 n。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="安装 NodeSync 到 MaiBot 插件目录。")
    parser.add_argument("target", nargs="?", type=Path, help="MaiBot 根目录；Docker 用户也可以传 plugins 目录")
    parser.add_argument("--wizard", action="store_true", help="启动交互式安装向导")
    parser.add_argument("--maibot-version", "--version", dest="maibot_version", help="MaiBot 版本：0.12.2 或 1.0")
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument("--maibot-root", type=Path, help="MaiBot 根目录，脚本会使用其 plugins 子目录")
    target_group.add_argument("--plugins-dir", type=Path, help="插件根目录；Docker 宿主机常用 ./data/MaiMBot/plugins")
    parser.add_argument("--mode", default="server", choices=["server", "client", "disabled"], help="NodeSync 运行模式")
    parser.add_argument("--bot-id", default="", help="当前 bot 的唯一 ID；留空时使用基础默认值")
    parser.add_argument("--bot-name", default="", help="当前 bot 显示名")
    parser.add_argument("--auth-token", default="", help="server/client 共享 token；留空时自动生成强 token")
    parser.add_argument("--server-host", default="", help="server 监听地址；跨机器或 Docker server 常用 0.0.0.0")
    parser.add_argument("--server-port", type=int, default=8765, help="server 监听端口")
    parser.add_argument("--server-url", default="", help="client 连接 server 的 URL")
    parser.add_argument("--data-dir", default="data/nodesync", help="NodeSync 数据目录")
    parser.add_argument("--streams", default="", help="逗号分隔的 stream_id 白名单；留空表示全部")
    parser.add_argument("--llm-worker-bot-id", default="", help="指定负责 LLM 委托的 bot_id")
    parser.add_argument("--disable-llm-decision", action="store_true", help="关闭 LLM 主判定，回退规则判断")
    parser.add_argument("--disable-llm-advance-generation", action="store_true", help="关闭 LLM 推进文本生成")
    parser.add_argument("--intervention-cooldown-seconds", type=int, default=300, help="介入冷却秒数")
    parser.add_argument("--analysis-min-messages", type=int, default=4, help="触发分析的最小消息数")
    parser.add_argument("--analysis-window-messages", type=int, default=8, help="自动分析窗口消息数")
    parser.add_argument("--plugin-dir-name", default="", help="自定义目标插件目录名")
    parser.add_argument("--force", action="store_true", help="目标目录已存在时替换")
    parser.add_argument("--no-backup", action="store_true", help="配合 --force 使用，不备份旧目录而是直接删除")
    parser.add_argument("--skip-config", action="store_true", help="不生成 config.toml")
    parser.add_argument("--overwrite-config", action="store_true", help="覆盖已有 config.toml")
    parser.add_argument("--dry-run", action="store_true", help="只展示操作，不写入文件")
    parser.add_argument("--install-package", action="store_true", help="执行 pip/uv，把 nodesync 包安装到目标 Python 环境")
    parser.add_argument("--python-executable", default=sys.executable, help="执行 pip 安装时使用的 Python")
    parser.add_argument("--uv-project", type=Path, help="使用 uv pip install -e，并以该目录为工作目录")
    return parser


def options_from_args(args: argparse.Namespace) -> InstallOptions:
    maibot_root = args.maibot_root
    plugins_dir = args.plugins_dir
    if args.target is not None:
        if maibot_root is not None or plugins_dir is not None:
            raise InstallerError("位置路径不能和 --maibot-root/--plugins-dir 同时使用。")
        if _looks_like_plugins_dir(args.target):
            plugins_dir = args.target
        else:
            maibot_root = args.target

    maibot_version = args.maibot_version
    if not maibot_version and maibot_root is not None:
        maibot_version = detect_maibot_version(maibot_root)
        if maibot_version:
            print(f"[NodeSyncInstaller] 已自动识别 MaiBot 版本: {maibot_version}")
    if not maibot_version:
        raise InstallerError("无法自动判断 MaiBot 版本，请加 --version 1.0 或 --version 0.12.2。")

    return InstallOptions(
        maibot_version=maibot_version,
        maibot_root=maibot_root,
        plugins_dir=plugins_dir,
        mode=args.mode,
        bot_id=args.bot_id,
        bot_name=args.bot_name,
        auth_token=args.auth_token,
        server_host=args.server_host,
        server_port=args.server_port,
        server_url=args.server_url,
        data_dir=args.data_dir,
        streams=_parse_streams(args.streams),
        llm_worker_bot_id=args.llm_worker_bot_id,
        enable_llm_decision=not args.disable_llm_decision,
        enable_llm_advance_generation=not args.disable_llm_advance_generation,
        intervention_cooldown_seconds=args.intervention_cooldown_seconds,
        analysis_min_messages=args.analysis_min_messages,
        analysis_window_messages=args.analysis_window_messages,
        plugin_dir_name=args.plugin_dir_name,
        force=args.force,
        backup_existing=not args.no_backup,
        skip_config=args.skip_config,
        overwrite_config=args.overwrite_config,
        dry_run=args.dry_run,
        install_package=args.install_package,
        python_executable=args.python_executable,
        uv_project=args.uv_project,
    )


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not raw_args:
        try:
            result = install(run_wizard())
        except InstallerError as exc:
            print(f"[NodeSyncInstaller] 安装失败: {exc}", file=sys.stderr)
            return 1
        _print_success(result, skip_config=False)
        return 0

    args = parser.parse_args(raw_args)
    try:
        options = run_wizard() if args.wizard else options_from_args(args)
        result = install(options)
    except InstallerError as exc:
        print(f"[NodeSyncInstaller] 安装失败: {exc}", file=sys.stderr)
        return 1

    _print_success(result, skip_config=options.skip_config)
    return 0


def _print_success(result: InstallResult, *, skip_config: bool) -> None:
    print("[NodeSyncInstaller] 安装完成")
    print(f"[NodeSyncInstaller] MaiBot 版本: {result.spec.version_label}")
    print(f"[NodeSyncInstaller] 插件目录: {result.target_dir}")
    if not skip_config:
        print(f"[NodeSyncInstaller] 配置文件: {result.config_path}")
    if result.generated_token:
        print(f"[NodeSyncInstaller] 已生成 auth_token，请复制给所有 client: {result.auth_token}")
    if not result.package_install_command:
        print("[NodeSyncInstaller] 提醒：如果 MaiBot 环境还不能 import nodesync，请在对应 Python 环境执行 pip install -e NodeSync。")


if __name__ == "__main__":
    raise SystemExit(main())
