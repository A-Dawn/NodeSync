from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nodesync.shared.models import ChatMessageEvent, SceneMode
from nodesync.shared.prompt_templates import configure_prompt_templates
from nodesync.shared.texts import build_client_advance_reply_prompt


class PromptTemplateTest(unittest.TestCase):
    def tearDown(self) -> None:
        configure_prompt_templates(None)

    def test_prompt_templates_can_be_overridden_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt_dir = Path(tmp)
            configure_prompt_templates(prompt_dir)
            template_path = prompt_dir / "client_advance_reply.txt"
            self.assertTrue(template_path.is_file())
            template_path.write_text(
                "自定义模板：{{bot_name}} / {{reply_persona}} / {{directive_content}} / {{recent_messages}}",
                encoding="utf-8",
            )

            prompt = build_client_advance_reply_prompt(
                bot_name="澪",
                reply_persona="冷静观察",
                directive_content="检查符文反应",
                scene_snapshot={"topic": "石门", "mode": SceneMode.ROLEPLAY.value},
                messages=[
                    ChatMessageEvent(
                        stream_id="group-1",
                        message_id="m1",
                        sender_id="u1",
                        sender_name="小林",
                        plain_text="门还在低声重复名字。",
                    )
                ],
            )

            self.assertIn("自定义模板：澪", prompt)
            self.assertIn("冷静观察", prompt)
            self.assertIn("检查符文反应", prompt)
            self.assertIn("小林", prompt)


if __name__ == "__main__":
    unittest.main()
