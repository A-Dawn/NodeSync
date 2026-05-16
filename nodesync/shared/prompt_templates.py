"""可文件化维护的 prompt 模板。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nodesync.shared.logging_utils import get_logger

logger = get_logger("shared.prompt_templates")

PROMPT_README = """# NodeSync Prompts

这些文件由 NodeSync 自动生成，已存在的文件不会被覆盖。

你可以直接修改同目录下的 `.txt` 文件来调整提示词；修改后通常重启 MaiBot 最稳妥。
模板变量使用 `{{变量名}}` 格式。请尽量保留原有变量，否则对应上下文不会出现在 prompt 中。
"""

DEFAULT_TEMPLATES: dict[str, str] = {
    "client_advance_reply": """你是群聊里的 {{bot_name}}，需要根据自己的语气自然发一条消息。
你的人设/说话方式：{{reply_persona}}
你收到的是内部推进意图，不要把它当作可直接发送的文本。
请把推进意图改写成一条符合当前聊天/角色扮演的最终群聊发言。
要求：只输出最终发言；1到2句；不要解释；不要自称系统、协调器或 NodeSync；不要说“当前场景、推进意图、角色们、下一步定具体、围绕”等元叙事词；不要换话题；优先用第一人称动作、观察或一个自然问题推动当前内容。

话题: {{topic}}
模式: {{mode}}
内部推进意图: {{directive_content}}

最近消息:
{{recent_messages}}
""",
    "prompt_injection": """

[NodeSync 私有上下文对齐]
当前协作场景: {{topic}}
对齐指令: {{content}}
请保持当前话题或 RP 场景，不要突然换话题；优先推动当前内容向下一步发展。
[/NodeSync 私有上下文对齐]
""",
    "scene_decision": """你是 NodeSync 的群聊协作协调器。请只输出一个 JSON 对象，不要输出 Markdown。
任务：你是本次窗口的主判定者，判断当前群聊话题或 RP 场景是否需要介入。介入不是换话题，而是让当前内容进入下一步。
允许的 decision 只能是 no_op、align_context、advance_dialogue。
如果场景已经自然结束，保持 decision=no_op，并设置 completed=true。
轻度停滞优先 align_context；明显原地打转或对齐后仍无进展时 advance_dialogue。
不要只看表面重复，要判断最近消息是否产生了新的事实、选择、行动、风险确认或角色关系变化。
如果最近消息虽然很短但已经带来明确行动或新信息，应选择 no_op。
规则基线只是兜底参考，不要被它束缚。

当前场景: {{topic}}
模式: {{mode}}
阶段: {{stage}}
已知事实: {{known_facts}}
未解决问题: {{open_questions}}
规则基线参考: {{heuristic_decision}}
候选 bot: {{candidate_bot_ids}}

最近消息:
{{recent_messages}}

JSON schema:
{"decision":"no_op|align_context|advance_dialogue","completed":false,"reason":"简短原因","target_bot_id":"可为空","content":"align_context 的私有对齐内容，或 advance_dialogue 的内部推进意图","stage":"opening|developing|questioning|action|closing","goal":"当前场景下一步目标","progress_delta":"最近是否产生了真实进展","confidence":0.0}
""",
    "advance_generation": """你正在为 NodeSync 生成内部推进意图，不是群内最终发言。
目标：不换话题，判断接下来应该做什么，帮助目标 bot 的回复器稍后自然落笔。
要求：只输出一条简短中文推进意图；不要解释；不要写成群聊发言；不要使用第二人称命令。

当前场景: {{topic}}
模式: {{mode}}
下一步目标: {{goal}}
需要避免: {{current_loop}}

最近消息:
{{recent_messages}}

规则建议: {{instruction}}
""",
}

_prompt_dir: Path | None = None


def configure_prompt_templates(prompt_dir: Path | str | None) -> None:
    """配置用户可编辑 prompt 目录，并补齐默认模板。"""

    global _prompt_dir
    if prompt_dir is None or not str(prompt_dir).strip():
        _prompt_dir = None
        return
    _prompt_dir = Path(prompt_dir)
    ensure_prompt_templates(_prompt_dir)


def ensure_prompt_templates(prompt_dir: Path) -> None:
    """把缺失的默认模板写入指定目录。"""

    prompt_dir.mkdir(parents=True, exist_ok=True)
    readme_path = prompt_dir / "README.md"
    if not readme_path.exists():
        readme_path.write_text(PROMPT_README, encoding="utf-8")
    for name, content in DEFAULT_TEMPLATES.items():
        path = prompt_dir / f"{name}.txt"
        if not path.exists():
            path.write_text(content, encoding="utf-8")


def render_prompt_template(name: str, variables: dict[str, Any]) -> str:
    """读取模板并替换 `{{变量名}}` 占位符。"""

    template = _load_template(name)
    rendered = template
    for key, value in variables.items():
        rendered = rendered.replace("{{" + key + "}}", str(value))
    return rendered


def _load_template(name: str) -> str:
    default = DEFAULT_TEMPLATES[name]
    if _prompt_dir is None:
        return default
    path = _prompt_dir / f"{name}.txt"
    try:
        if path.exists():
            return path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("[NodeSyncPrompt] 读取 prompt 模板失败，使用内置默认: %s", exc)
    return default
