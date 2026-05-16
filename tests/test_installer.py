from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path

from scripts.install_nodesync import (
    InstallerError,
    InstallOptions,
    build_parser,
    detect_maibot_version,
    install,
    options_from_args,
    resolve_spec,
)


class InstallerTest(unittest.TestCase):
    def test_install_maibot_012_to_maibot_root_generates_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            maibot_root = Path(tmp) / "MaiBot-0.12.2"
            result = install(
                InstallOptions(
                    maibot_version="0.12.2",
                    maibot_root=maibot_root,
                    mode="server",
                    bot_id="main-bot",
                    bot_name="主 Bot",
                    auth_token="fixed-token",
                    server_host="0.0.0.0",
                )
            )

            self.assertEqual(result.target_dir, maibot_root / "plugins" / "nodesync")
            self.assertTrue((result.target_dir / "plugin.py").is_file())
            self.assertTrue((result.target_dir / "_manifest.json").is_file())
            self.assertTrue((result.target_dir / "prompts" / "client_advance_reply.txt").is_file())
            config = _load_toml(result.config_path)
            self.assertEqual(config["plugin"]["name"], "nodesync")
            self.assertEqual(config["nodesync"]["mode"], "server")
            self.assertEqual(config["nodesync"]["bot_id"], "main-bot")
            self.assertEqual(config["nodesync"]["reply_persona"], "")
            self.assertEqual(config["nodesync"]["auth_token"], "fixed-token")
            self.assertEqual(config["nodesync"]["server_host"], "0.0.0.0")
            self.assertEqual(config["prompts"]["directory"], "prompts")
            self.assertFalse(config["diagnostics"]["enabled"])
            self.assertEqual(config["diagnostics"]["output_dir"], "")
            self.assertFalse(config["local_flow"]["enabled"])
            self.assertEqual(config["local_flow"]["host"], "127.0.0.1")
            self.assertEqual(config["policy"]["analysis_window_messages"], 8)

    def test_install_maibot_10_to_plugins_dir_generates_client_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugins_dir = Path(tmp) / "data" / "MaiMBot" / "plugins"
            result = install(
                InstallOptions(
                    maibot_version="1.0",
                    plugins_dir=plugins_dir,
                    mode="client",
                    bot_id="side-bot-a",
                    auth_token="shared-token",
                    server_url="http://maibot-server:8765",
                    streams=["group-a", "group-b"],
                    enable_llm_decision=False,
                )
            )

            self.assertEqual(result.target_dir, plugins_dir / "nodesync_coordinator")
            self.assertEqual(result.spec.plugin_id, "nodesync.coordinator")
            config = _load_toml(result.config_path)
            self.assertEqual(config["plugin"]["config_version"], "0.1.0")
            self.assertEqual(config["nodesync"]["mode"], "client")
            self.assertEqual(config["nodesync"]["server_url"], "http://maibot-server:8765")
            self.assertEqual(config["nodesync"]["streams"], ["group-a", "group-b"])
            self.assertFalse(config["policy"]["enable_llm_decision"])
            self.assertEqual(config["policy"]["analysis_window_messages"], 8)
            self.assertTrue(config["local_flow"]["capture_outbound"])

    def test_existing_target_requires_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            maibot_root = Path(tmp) / "MaiBot-1.0"
            target = maibot_root / "plugins" / "nodesync_coordinator"
            target.mkdir(parents=True)
            (target / "old.txt").write_text("old", encoding="utf-8")

            with self.assertRaises(InstallerError):
                install(InstallOptions(maibot_version="1.0", maibot_root=maibot_root, auth_token="token"))

            self.assertEqual((target / "old.txt").read_text(encoding="utf-8"), "old")

    def test_force_backs_up_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            maibot_root = Path(tmp) / "MaiBot-1.0"
            target = maibot_root / "plugins" / "nodesync_coordinator"
            target.mkdir(parents=True)
            (target / "old.txt").write_text("old", encoding="utf-8")

            result = install(
                InstallOptions(
                    maibot_version="1.0",
                    maibot_root=maibot_root,
                    auth_token="token",
                    force=True,
                )
            )

            backups = list((maibot_root / "plugins").glob("nodesync_coordinator.backup_*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0] / "old.txt").read_text(encoding="utf-8"), "old")
            self.assertTrue((result.target_dir / "plugin.py").is_file())

    def test_dry_run_does_not_create_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            maibot_root = Path(tmp) / "MaiBot-0.12.2"
            install(
                InstallOptions(
                    maibot_version="012",
                    maibot_root=maibot_root,
                    auth_token="token",
                    dry_run=True,
                )
            )

            self.assertFalse((maibot_root / "plugins").exists())

    def test_version_aliases(self) -> None:
        self.assertEqual(resolve_spec("012").version_label, "0.12.2")
        self.assertEqual(resolve_spec("0.12").version_label, "0.12.2")
        self.assertEqual(resolve_spec("10").version_label, "1.0")

    def test_detect_maibot_version_from_project_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_10 = Path(tmp) / "MaiBot-1.0"
            (root_10 / "src" / "plugin_runtime" / "runner").mkdir(parents=True)
            (root_10 / "src" / "plugin_runtime" / "runner" / "plugin_loader.py").write_text("", encoding="utf-8")

            root_012 = Path(tmp) / "MaiBot-0.12.2"
            (root_012 / "src" / "plugin_system" / "core").mkdir(parents=True)
            (root_012 / "src" / "plugin_system" / "core" / "plugin_manager.py").write_text("", encoding="utf-8")

            self.assertEqual(detect_maibot_version(root_10), "1.0")
            self.assertEqual(detect_maibot_version(root_012), "0.12.2")

    def test_positional_target_auto_detects_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            maibot_root = Path(tmp) / "MaiBot-1.0"
            (maibot_root / "src" / "plugin_runtime" / "runner").mkdir(parents=True)
            (maibot_root / "src" / "plugin_runtime" / "runner" / "plugin_loader.py").write_text("", encoding="utf-8")

            args = build_parser().parse_args([str(maibot_root), "--mode", "client", "--auth-token", "token"])
            options = options_from_args(args)

            self.assertEqual(options.maibot_version, "1.0")
            self.assertEqual(options.maibot_root, maibot_root)
            self.assertIsNone(options.plugins_dir)
            self.assertEqual(options.mode, "client")

    def test_plugins_dir_target_requires_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugins_dir = Path(tmp) / "data" / "MaiMBot" / "plugins"
            args = build_parser().parse_args([str(plugins_dir), "--auth-token", "token"])

            with self.assertRaises(InstallerError):
                options_from_args(args)


def _load_toml(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


if __name__ == "__main__":
    unittest.main()
