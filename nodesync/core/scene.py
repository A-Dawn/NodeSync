"""场景识别与推进判断。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from uuid import uuid4

from nodesync.shared.models import ChatMessageEvent, DecisionKind, SceneMode, SceneState
from nodesync.shared.texts import build_advance_dialogue_message, build_private_alignment_content
from nodesync.shared.time_utils import now_ts

ROLEPLAY_HINTS = ("roleplay", "rp", "scene", "character", "扮演", "角色", "场景", "剧情")
ACTION_HINTS = ("decide", "choose", "enter", "leave", "investigate", "选择", "决定", "进入", "调查", "行动")
QUESTION_MARKERS = ("?", "？", "how", "why", "what", "怎么", "为什么", "是否", "哪个")
COMPLETION_HINTS = ("结束", "完结", "收尾", "散了", "下次再", "晚安", "拜拜", "就到这里", "end scene")
NEW_SCENE_HINTS = ("新话题", "换个话题", "开新场景", "另一个话题", "另一个问题", "new topic", "new scene")


@dataclass(slots=True)
class ProgressDecision:
    kind: str
    reason: str
    scene: SceneState
    target_bot_id: str = ""
    content: str = ""
    stagnation_score: float = 0.0
    metadata: dict[str, str] = field(default_factory=dict)


class SceneAnalyzer:
    """场景分析器的规则基线。"""

    def recognize_or_update(self, stream_id: str, messages: list[ChatMessageEvent], scene: SceneState | None) -> SceneState:
        """根据最近消息创建或更新场景。"""

        if scene is None:
            scene = SceneState(
                session_id=f"scene_{uuid4().hex}",
                stream_id=stream_id,
                topic=self._derive_topic(messages),
                mode=self._detect_mode(messages),
                participants=self._participants(messages),
            )
        else:
            scene.participants = sorted(set(scene.participants) | set(self._participants(messages)))
            if not scene.topic:
                scene.topic = self._derive_topic(messages)
            if scene.mode == SceneMode.UNKNOWN.value:
                scene.mode = self._detect_mode(messages)

        scene.open_questions = self._extract_open_questions(messages)
        scene.known_facts = self._extract_known_facts(messages)
        scene.current_loop = self._detect_loop(messages)
        scene.stage = self._derive_stage(scene, messages)
        scene.updated_at = now_ts()
        return scene

    def close_reason(
        self,
        scene: SceneState,
        messages: list[ChatMessageEvent],
        idle_close_seconds: int,
        current_time: float | None = None,
    ) -> str:
        """判断当前活跃场景是否应自动关闭。"""

        if not messages:
            return ""
        latest_text = " ".join(event.plain_text.lower() for event in messages[-5:])
        if any(hint in latest_text for hint in COMPLETION_HINTS):
            return "completed"
        if any(hint in latest_text for hint in NEW_SCENE_HINTS):
            return "new_scene"
        now = current_time if current_time is not None else now_ts()
        if idle_close_seconds > 0 and now - scene.updated_at > idle_close_seconds:
            return "idle_timeout"
        return ""

    def decide(self, scene: SceneState, messages: list[ChatMessageEvent], candidate_bot_ids: list[str]) -> ProgressDecision:
        """判断当前场景是否需要介入。"""

        if len(messages) < 4:
            return ProgressDecision(
                kind=DecisionKind.NO_OP.value,
                reason="not_enough_messages",
                scene=scene,
                stagnation_score=0.0,
            )

        score = self._stagnation_score(scene, messages)
        scene.stagnation_score = score
        current_time = now_ts()

        if score < 0.45:
            scene.last_progress_at = current_time
            return ProgressDecision(
                kind=DecisionKind.NO_OP.value,
                reason="conversation_progressing",
                scene=scene,
                stagnation_score=score,
            )

        target = self._select_target_bot(messages, candidate_bot_ids)
        if score < 0.72:
            content = self._build_alignment(scene)
            return ProgressDecision(
                kind=DecisionKind.ALIGN_CONTEXT.value,
                reason="light_stagnation_align_context",
                scene=scene,
                target_bot_id=target,
                content=content,
                stagnation_score=score,
            )

        content = self._build_advance_message(scene)
        return ProgressDecision(
            kind=DecisionKind.ADVANCE_DIALOGUE.value,
            reason="stalled_after_alignment_or_high_loop_score",
            scene=scene,
            target_bot_id=target,
            content=content,
            stagnation_score=score,
        )

    def _derive_topic(self, messages: list[ChatMessageEvent]) -> str:
        for event in messages:
            text = event.plain_text.strip()
            if len(text) >= 4 and not text.startswith("/"):
                return text[:80]
        return "自动识别场景"

    def _detect_mode(self, messages: list[ChatMessageEvent]) -> str:
        text = " ".join(event.plain_text.lower() for event in messages[-12:])
        if any(hint in text for hint in ROLEPLAY_HINTS):
            return SceneMode.ROLEPLAY.value
        return SceneMode.DISCUSSION.value

    def _participants(self, messages: list[ChatMessageEvent]) -> list[str]:
        return sorted({event.sender_id for event in messages if event.sender_id})

    def _extract_open_questions(self, messages: list[ChatMessageEvent]) -> list[str]:
        questions = []
        for event in messages[-12:]:
            text = event.plain_text.strip()
            lowered = text.lower()
            if any(marker in lowered for marker in QUESTION_MARKERS):
                questions.append(text[:120])
        return questions[-5:]

    def _extract_known_facts(self, messages: list[ChatMessageEvent]) -> list[str]:
        facts = []
        for event in messages[-12:]:
            text = re.sub(r"\s+", " ", event.plain_text.strip())
            if len(text) >= 12 and not any(marker in text.lower() for marker in QUESTION_MARKERS):
                facts.append(text[:120])
        return facts[-6:]

    def _detect_loop(self, messages: list[ChatMessageEvent]) -> str:
        short = [self._normalize(event.plain_text) for event in messages[-8:] if event.plain_text.strip()]
        if len(short) < 4:
            return ""
        unique = len(set(short))
        if unique <= max(2, len(short) // 3):
            return "repeated_surface_responses"
        return ""

    def _derive_stage(self, scene: SceneState, messages: list[ChatMessageEvent]) -> str:
        text = " ".join(event.plain_text.lower() for event in messages[-6:])
        if any(hint in text for hint in COMPLETION_HINTS):
            return "closing"
        if any(marker in text for marker in ACTION_HINTS):
            return "action"
        if scene.open_questions:
            return "questioning"
        if len(scene.known_facts) >= 2:
            return "developing"
        return scene.stage or "opening"

    def _stagnation_score(self, scene: SceneState, messages: list[ChatMessageEvent]) -> float:
        recent = [self._normalize(event.plain_text) for event in messages[-8:] if event.plain_text.strip()]
        if not recent:
            return 0.0

        unique_ratio = len(set(recent)) / max(len(recent), 1)
        avg_len = sum(len(item) for item in recent) / max(len(recent), 1)
        question_count = sum(1 for item in recent if any(marker in item for marker in QUESTION_MARKERS))
        action_count = sum(1 for item in recent if any(marker in item for marker in ACTION_HINTS))

        score = 0.0
        if unique_ratio < 0.5:
            score += 0.35
        if avg_len < 18:
            score += 0.20
        if question_count == 0:
            score += 0.15
        if action_count == 0 and scene.mode == SceneMode.ROLEPLAY.value:
            score += 0.20
        if scene.current_loop:
            score += 0.20
        return min(score, 1.0)

    def _select_target_bot(self, messages: list[ChatMessageEvent], candidate_bot_ids: list[str]) -> str:
        if not candidate_bot_ids:
            return ""
        recent_senders = [event.sender_id for event in messages[-8:] if event.sender_id]
        for bot_id in candidate_bot_ids:
            if bot_id not in recent_senders:
                return bot_id
        return candidate_bot_ids[0]

    def _build_alignment(self, scene: SceneState) -> str:
        return build_private_alignment_content(scene)

    def _build_advance_message(self, scene: SceneState) -> str:
        return build_advance_dialogue_message(scene)

    def _normalize(self, text: str) -> str:
        lowered = text.lower().strip()
        lowered = re.sub(r"\s+", " ", lowered)
        return lowered[:80]
