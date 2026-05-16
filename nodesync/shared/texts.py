"""NodeSync 用户可见文案与模型提示。"""

from __future__ import annotations

from nodesync.shared.models import ChatMessageEvent, InjectionRecord, SceneMode, SceneState
from nodesync.shared.prompt_templates import render_prompt_template

FORBIDDEN_GROUP_REPLY_TERMS = (
    "NodeSync",
    "协调器",
    "推进意图",
    "当前场景",
    "角色们",
    "下一步定具体",
    "围绕「",
)


def build_private_alignment_content(scene: SceneState) -> str:
    """生成 align_context 的私有对齐内容。"""

    open_question = scene.open_questions[-1] if scene.open_questions else "把下一步具体化"
    return (
        "NodeSync 私有对齐指令：\n"
        f"- 当前话题/场景：{scene.topic}\n"
        f"- 当前阶段：{scene.stage}\n"
        f"- 避免重复：{scene.current_loop or '只停留在表层附和'}\n"
        "- 下一步目标：不要换话题，把当前话题或 RP 场景向更具体的一步推进。\n"
        f"- 可利用的未解决点：{open_question}\n"
    )


def build_advance_dialogue_message(scene: SceneState) -> str:
    """生成推进意图。

    这里的内容给目标 client 的回复器使用，不应直接发送到真实群内。
    """

    if scene.mode == SceneMode.ROLEPLAY.value:
        return (
            f"围绕「{scene.topic}」推进当前群聊内容：让角色做一个具体动作，"
            "优先确认最接近的线索、机关或风险，不要换场景。"
        )
    return (
        f"围绕「{scene.topic}」推进当前讨论："
        "请不要换话题。"
    )


def build_client_advance_reply_prompt(
    bot_name: str,
    reply_persona: str,
    directive_content: str,
    scene_snapshot: dict[str, object],
    messages: list[ChatMessageEvent],
) -> str:
    """生成 client 侧回复器兼容 prompt。

    首版 MaiBot SDK 尚未提供统一的“回复器直接生成”能力，因此这里用
    adapter 侧 LLM 能力模拟回复器落笔；宿主暴露正式 replyer 后可替换此层。
    """

    recent_messages = _render_messages(messages[-12:])
    topic = str(scene_snapshot.get("topic", "") or "当前话题")
    mode = str(scene_snapshot.get("mode", "") or "discussion")
    return render_prompt_template(
        "client_advance_reply",
        {
            "bot_name": bot_name,
            "reply_persona": reply_persona or "保持当前 bot 自身设定，语气自然，不要突然改变性格。",
            "topic": topic,
            "mode": mode,
            "directive_content": directive_content,
            "recent_messages": recent_messages,
        },
    )


def build_safe_advance_reply_fallback(scene_snapshot: dict[str, object]) -> str:
    """生成可直接群发的保底推进消息。"""

    mode = str(scene_snapshot.get("mode", ""))
    if mode == SceneMode.ROLEPLAY.value:
        return "我先不继续原地等了，伸手确认一下最近的线索有没有反应。"
    return "我先把这个点落到一个小问题上：我们下一步最该确认哪一个条件？"


def clean_advance_reply_content(raw: str, scene_snapshot: dict[str, object]) -> str:
    """清理 client 侧回复器输出，避免内部协调语言进入群聊。"""

    text = raw.strip().strip("`").strip()
    if text.lower().startswith("text"):
        text = text[4:].strip()
    for prefix in ("回复：", "发言：", "消息：", "最终消息："):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    text = " ".join(lines).strip()
    if not text or any(term in text for term in FORBIDDEN_GROUP_REPLY_TERMS):
        return build_safe_advance_reply_fallback(scene_snapshot)
    return text[:500]


def build_prompt_injection(base_prompt: str, record: InjectionRecord | None) -> str:
    """把本地有效注入拼接到 LLM prompt 尾部。"""

    if record is None:
        return base_prompt
    block = render_prompt_template(
        "prompt_injection",
        {
            "topic": record.source_scene_snapshot.get("topic", ""),
            "content": record.content,
        },
    )
    return f"{base_prompt or ''}{block}"


def build_scene_decision_prompt(
    scene: SceneState,
    messages: list[ChatMessageEvent],
    heuristic_decision: str,
    candidate_bot_ids: list[str],
) -> str:
    """生成服务端 LLM 场景决策 prompt。"""

    recent_messages = _render_messages(messages[-16:])
    candidates = ", ".join(candidate_bot_ids) if candidate_bot_ids else "无"
    return render_prompt_template(
        "scene_decision",
        {
            "topic": scene.topic,
            "mode": scene.mode,
            "stage": scene.stage,
            "known_facts": "；".join(scene.known_facts) or "无",
            "open_questions": "；".join(scene.open_questions) or "无",
            "heuristic_decision": heuristic_decision,
            "candidate_bot_ids": candidates,
            "recent_messages": recent_messages,
        },
    )


def build_advance_generation_prompt(
    scene: SceneState,
    messages: list[ChatMessageEvent],
    instruction: str,
) -> str:
    """生成内部推进意图的 LLM prompt。"""

    recent_messages = _render_messages(messages[-12:])
    return render_prompt_template(
        "advance_generation",
        {
            "topic": scene.topic,
            "mode": scene.mode,
            "goal": scene.goal or instruction,
            "current_loop": scene.current_loop or "重复表层附和",
            "recent_messages": recent_messages,
            "instruction": instruction,
        },
    )


def _render_messages(messages: list[ChatMessageEvent]) -> str:
    lines = []
    for item in messages:
        name = item.sender_name or item.sender_id or "unknown"
        text = item.plain_text.replace("\n", " ").strip()
        if text:
            lines.append(f"- {name}: {text[:180]}")
    return "\n".join(lines) if lines else "- 无"
