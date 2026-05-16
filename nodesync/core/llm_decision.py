"""LLM 辅助决策解析与合并。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from nodesync.core.scene import ProgressDecision
from nodesync.shared.models import DecisionKind, LLMResponse, SceneState
from nodesync.shared.time_utils import now_ts


@dataclass(slots=True)
class LLMSceneDecision:
    """LLM 对当前场景的结构化判断。"""

    decision: str
    completed: bool = False
    reason: str = ""
    target_bot_id: str = ""
    content: str = ""
    stage: str = ""
    goal: str = ""
    progress_delta: str = ""
    confidence: float = 0.0

    @classmethod
    def from_response(cls, response: LLMResponse) -> LLMSceneDecision | None:
        """从 LLM 响应中解析结构化 JSON。"""

        if not response.ok or not response.content.strip():
            return None
        data = _extract_json_object(response.content)
        if data is None:
            return None
        decision = str(data.get("decision", "")).strip()
        if decision not in {
            DecisionKind.NO_OP.value,
            DecisionKind.ALIGN_CONTEXT.value,
            DecisionKind.ADVANCE_DIALOGUE.value,
        }:
            return None
        return cls(
            decision=decision,
            completed=_parse_bool(data.get("completed", False)),
            reason=str(data.get("reason", ""))[:300],
            target_bot_id=str(data.get("target_bot_id", "")),
            content=str(data.get("content", ""))[:1200],
            stage=str(data.get("stage", ""))[:40],
            goal=str(data.get("goal", ""))[:300],
            progress_delta=str(data.get("progress_delta", ""))[:300],
            confidence=_parse_float(data.get("confidence", 0.0)),
        )


def merge_llm_decision(
    scene: SceneState,
    heuristic: ProgressDecision,
    llm_decision: LLMSceneDecision | None,
    candidate_bot_ids: list[str],
) -> ProgressDecision:
    """把 LLM 判断合并到规则基线决策中。"""

    if llm_decision is None:
        return heuristic

    if llm_decision.stage:
        scene.stage = llm_decision.stage
    if llm_decision.goal:
        scene.goal = llm_decision.goal
    if llm_decision.progress_delta:
        scene.progress_delta = llm_decision.progress_delta

    if llm_decision.completed:
        return ProgressDecision(
            kind=DecisionKind.NO_OP.value,
            reason=llm_decision.reason or "llm_scene_completed",
            scene=scene,
            target_bot_id="",
            content="",
            stagnation_score=heuristic.stagnation_score,
            metadata={"completed": "true", "source": "llm"},
        )

    if llm_decision.decision == DecisionKind.NO_OP.value:
        scene.last_progress_at = now_ts()

    target = llm_decision.target_bot_id if llm_decision.target_bot_id in candidate_bot_ids else heuristic.target_bot_id
    content = llm_decision.content or heuristic.content
    return ProgressDecision(
        kind=llm_decision.decision,
        reason=llm_decision.reason or f"llm_{llm_decision.decision}",
        scene=scene,
        target_bot_id=target,
        content=content,
        stagnation_score=heuristic.stagnation_score,
        metadata={"source": "llm", "confidence": f"{llm_decision.confidence:.2f}"},
    )


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "是", "已结束"}
    return bool(value)


def _parse_float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(parsed, 1.0))
