from __future__ import annotations

import unittest

from nodesync.core.llm_decision import LLMSceneDecision, merge_llm_decision
from nodesync.core.scene import ProgressDecision
from nodesync.shared.models import DecisionKind, LLMResponse, SceneState


class LLMDecisionTest(unittest.TestCase):
    def test_parse_json_from_markdown_fence(self) -> None:
        response = LLMResponse(
            request_id="req-1",
            ok=True,
            content='```json\n{"decision":"align_context","completed":false,"confidence":0.8}\n```',
        )

        decision = LLMSceneDecision.from_response(response)

        self.assertIsNotNone(decision)
        self.assertEqual(decision.decision, DecisionKind.ALIGN_CONTEXT.value)
        self.assertEqual(decision.confidence, 0.8)

    def test_completed_scene_merges_to_no_op(self) -> None:
        scene = SceneState(session_id="scene-1", stream_id="group-1")
        heuristic = ProgressDecision(kind=DecisionKind.ADVANCE_DIALOGUE.value, reason="loop", scene=scene)
        llm = LLMSceneDecision(decision=DecisionKind.NO_OP.value, completed=True, reason="自然收尾")

        merged = merge_llm_decision(scene, heuristic, llm, ["bot-a"])

        self.assertEqual(merged.kind, DecisionKind.NO_OP.value)
        self.assertEqual(merged.metadata["completed"], "true")

    def test_llm_decision_is_primary_over_heuristic(self) -> None:
        scene = SceneState(session_id="scene-1", stream_id="group-1")
        heuristic = ProgressDecision(kind=DecisionKind.NO_OP.value, reason="rule_progressing", scene=scene)
        llm = LLMSceneDecision(
            decision=DecisionKind.ADVANCE_DIALOGUE.value,
            reason="语义上仍在原地重复",
            target_bot_id="bot-a",
            content="让角色确认下一条线索。",
            confidence=0.9,
        )

        merged = merge_llm_decision(scene, heuristic, llm, ["bot-a"])

        self.assertEqual(merged.kind, DecisionKind.ADVANCE_DIALOGUE.value)
        self.assertEqual(merged.target_bot_id, "bot-a")
        self.assertEqual(merged.content, "让角色确认下一条线索。")
        self.assertEqual(merged.metadata["source"], "llm")


if __name__ == "__main__":
    unittest.main()
