from __future__ import annotations

import unittest

from nodesync.core.scene import SceneAnalyzer
from nodesync.shared.models import ChatMessageEvent, DecisionKind, SceneMode
from nodesync.shared.texts import (
    build_client_advance_reply_prompt,
    build_safe_advance_reply_fallback,
    clean_advance_reply_content,
)


class SceneAnalyzerTest(unittest.TestCase):
    def test_progressing_discussion_no_op(self) -> None:
        analyzer = SceneAnalyzer()
        messages = [
            _msg("u1", "我们讨论一下远征队下一步怎么补给？"),
            _msg("u2", "可以先列资源，再决定路线。"),
            _msg("u3", "我选择先调查港口库存。"),
            _msg("u1", "那我们是否要把风险分成天气和价格两类？"),
        ]
        scene = analyzer.recognize_or_update("group-1", messages, None)
        decision = analyzer.decide(scene, messages, ["bot-a"])

        self.assertEqual(decision.kind, DecisionKind.NO_OP.value)

    def test_roleplay_light_stagnation_aligns_context(self) -> None:
        analyzer = SceneAnalyzer()
        messages = [
            _msg("u1", "RP 场景：我们在门口。"),
            _msg("u2", "我看看。"),
            _msg("u3", "我也看看。"),
            _msg("u1", "继续看看。"),
            _msg("u2", "还是看看。"),
        ]
        scene = analyzer.recognize_or_update("group-1", messages, None)
        decision = analyzer.decide(scene, messages, ["bot-a"])

        self.assertEqual(scene.mode, SceneMode.ROLEPLAY.value)
        self.assertEqual(decision.kind, DecisionKind.ALIGN_CONTEXT.value)

    def test_repeated_loop_advances_dialogue(self) -> None:
        analyzer = SceneAnalyzer()
        messages = [_msg(f"u{i % 2}", "嗯嗯") for i in range(8)]
        scene = analyzer.recognize_or_update("group-1", messages, None)
        decision = analyzer.decide(scene, messages, ["bot-a"])

        self.assertEqual(decision.kind, DecisionKind.ADVANCE_DIALOGUE.value)
        self.assertIn("推进", decision.content)
        self.assertNotIn("角色们", decision.content)
        self.assertNotIn("下一步定具体", decision.content)

    def test_completion_hint_closes_scene(self) -> None:
        analyzer = SceneAnalyzer()
        messages = [
            _msg("u1", "这个场景就到这里吧"),
            _msg("u2", "好，收尾"),
        ]
        scene = analyzer.recognize_or_update("group-1", messages, None)

        self.assertEqual(analyzer.close_reason(scene, messages, idle_close_seconds=3600), "completed")

    def test_advance_reply_cleaner_blocks_internal_coordination_text(self) -> None:
        snapshot = {"mode": SceneMode.ROLEPLAY.value}
        raw = "我们先继续留在当前场景里，把下一步定具体：角色们该选择行动。"

        cleaned = clean_advance_reply_content(raw, snapshot)

        self.assertEqual(cleaned, build_safe_advance_reply_fallback(snapshot))
        self.assertNotIn("当前场景", cleaned)
        self.assertNotIn("角色们", cleaned)

    def test_advance_reply_prompt_injects_persona(self) -> None:
        prompt = build_client_advance_reply_prompt(
            bot_name="澪",
            reply_persona="冷静、观察细致，像队伍里的调查员。",
            directive_content="确认最近的线索。",
            scene_snapshot={"topic": "石门", "mode": SceneMode.ROLEPLAY.value},
            messages=[_msg("u1", "门还在低声念名字。")],
        )

        self.assertIn("冷静、观察细致", prompt)
        self.assertIn("澪", prompt)


def _msg(sender_id: str, text: str) -> ChatMessageEvent:
    return ChatMessageEvent(
        stream_id="group-1",
        message_id=f"{sender_id}-{text}",
        sender_id=sender_id,
        sender_name=sender_id,
        plain_text=text,
    )


if __name__ == "__main__":
    unittest.main()
