from __future__ import annotations

import unittest
from pathlib import Path

from nodesync.adapters.maibot_012 import plugin as maibot_012
from nodesync.adapters.maibot_10 import plugin as maibot_10


class AdapterConfigPathsTest(unittest.TestCase):
    def test_maibot_10_prompt_dir_defaults_to_plugin_folder(self) -> None:
        config = maibot_10._build_config(maibot_10.NodeSync10PluginConfig())

        self.assertEqual(config.prompts_dir, Path(maibot_10.__file__).resolve().parent / "prompts")

    def test_maibot_10_prompt_dir_resolves_relative_to_plugin_folder(self) -> None:
        config_model = maibot_10.NodeSync10PluginConfig.model_validate({"prompts": {"directory": "custom-prompts"}})
        config = maibot_10._build_config(config_model)

        self.assertEqual(config.prompts_dir, Path(maibot_10.__file__).resolve().parent / "custom-prompts")

    def test_maibot_10_legacy_prompts_dir_is_still_supported(self) -> None:
        config_model = maibot_10.NodeSync10PluginConfig.model_validate(
            {"nodesync": {"prompts_dir": "legacy-prompts"}}
        )
        config = maibot_10._build_config(config_model)

        self.assertEqual(config.prompts_dir, Path(maibot_10.__file__).resolve().parent / "legacy-prompts")

    def test_maibot_012_prompt_dir_resolves_relative_to_plugin_folder(self) -> None:
        config = maibot_012._build_config({"prompts": {"directory": "custom-prompts"}})

        self.assertEqual(config.prompts_dir, Path(maibot_012.__file__).resolve().parent / "custom-prompts")

    def test_maibot_10_prompt_and_diagnostics_have_webui_metadata(self) -> None:
        self.assertIn("prompts", maibot_10.NodeSync10PluginConfig.model_fields)
        self.assertEqual(maibot_10.PromptSectionConfig.__ui_label__, "提示词")
        self.assertEqual(maibot_10.PromptSectionConfig.__ui_icon__, "file-text")
        self.assertEqual(
            maibot_10.PromptSectionConfig.model_fields["directory"].json_schema_extra["x-widget"],
            "input",
        )
        self.assertEqual(
            maibot_10.DiagnosticsSectionConfig.model_fields["enabled"].json_schema_extra["x-widget"],
            "switch",
        )


if __name__ == "__main__":
    unittest.main()
