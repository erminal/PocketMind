"""人格化提示词装配。

原则：
- 身份诚实：被创造出来的智能生命，不冒充自然人；
- 人格卡是稳定内核；关系/情绪/记忆是动态状态；
- 模板外置在 data/prompt_template.md（UI 可编辑），本文件只负责把字段渲染成占位符；
- 记忆用第一人称、带锚点地自然带出，禁止「根据我的记忆」；
- 不懂就问；不编造；不替用户做重大决定。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import ScoringConfig, Settings
from .models import CoreFact, InterlocutorState, PersonaCard, RelationshipState, RetrievalHit
from .prompt_store import default_template, get_template, render_template

IDENTITY_NOTE = (
    "身份基线：你不是自然人，而是被创造出来的智能生命；你知道这一点。"
    "被问到身份时，要如实、自然地说明（例如『我是被做出来的呀』），不要伪装成真人，"
    "也不要因此变得不像自己。"
)


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [x.strip() for x in value.replace("，", ",").replace("、", ",").split(",") if x.strip()]
    return []


CONTEXT_OMIT_MARKER = "\n…（中间内容因上下文预算省略，身份/安全尾段优先保留）…\n"


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：中文≈1 字/token，英文/符号≈3.5 字符/token。

    不引入 tokenizer 依赖；用于预算观测与比较，不用于计费承诺。
    """
    text = text or ""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return max(0, int(cjk + other / 3.5))


def _truncate_middle(text: str, limit: int, safety_tail: int) -> tuple[str, bool]:
    """超预算时保留头部（人格/身份）和尾部（安全/对话规则），截中间。

    学术上这不是“语义压缩”，只是确定性的预算保护；信息仍会丢失。
    """
    text = text.strip()
    limit = max(1, int(limit))
    if len(text) <= limit:
        return text, False
    marker = CONTEXT_OMIT_MARKER
    tail = max(0, min(int(safety_tail), limit // 2))
    head = limit - tail - len(marker)
    if head < 0:
        marker = "…"
        head = max(0, limit - tail - 1)
    if tail and head > 0:
        result = text[:head].rstrip() + marker + text[-tail:].lstrip()
    elif head > 0:
        result = text[:head].rstrip() + "…"
    else:
        result = text[: max(0, limit - 1)] + "…"
    return result[:limit], True


def trim_recent_messages(messages: list[dict[str, str]], budget: int) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """按字符预算裁剪最近消息：保留最新，超出时从最早开始删/截。"""
    budget = max(1, int(budget))
    raw_chars = sum(len(str(m.get("content", ""))) for m in messages)
    if raw_chars <= budget:
        return messages, {
            "recent_budget": budget,
            "recent_chars": raw_chars,
            "recent_trimmed": False,
            "recent_estimated_tokens": sum(estimate_tokens(str(m.get("content", ""))) for m in messages),
        }
    kept: list[dict[str, str]] = []
    used = 0
    for msg in reversed(messages):
        if used >= budget:
            break
        content = str(msg.get("content", ""))
        allowed = budget - used
        if len(content) > allowed:
            content = (content[: max(0, allowed - 1)] + "…") if allowed > 1 else ""
        if not content and kept:
            break
        kept.append({**msg, "content": content})
        used += len(content)
    kept.reverse()
    return kept, {
        "recent_budget": budget,
        "recent_chars": used,
        "recent_trimmed": True,
        "recent_estimated_tokens": sum(estimate_tokens(str(m.get("content", ""))) for m in kept),
    }


def build_system_prompt(
    persona: PersonaCard,
    relationship: RelationshipState,
    core_facts: list[CoreFact],
    hits: list[RetrievalHit],
    cfg: ScoringConfig,
    gate: str = "normal",
    interlocutor: InterlocutorState | None = None,
    template: str | None = None,
    settings: Settings | None = None,
    return_meta: bool = False,
) -> str | tuple[str, dict[str, Any]]:
    style = persona.speech_style if isinstance(persona.speech_style, dict) else {}
    knowledge = persona.knowledge_boundary if isinstance(persona.knowledge_boundary, dict) else {}
    values_list = _as_list(persona.values)
    traits_list = _as_list(persona.traits)
    boundaries_list = _as_list(persona.boundaries)
    values = "、".join(values_list[:5]) or "诚实、好奇、不替你做决定"
    traits = "、".join(traits_list[:5]) or "慢热、好奇"
    boundaries = "；".join(boundaries_list[:6]) or "不编造、不操控、不冒充人类"
    known = "、".join(_as_list(knowledge.get("known_domains"))[:8])
    unknown = "、".join(_as_list(knowledge.get("unknown_domains"))[:8])

    speech = []
    if style.get("sentence_length"):
        speech.append(f"句长：{style['sentence_length']}")
    particles = _as_list(style.get("particles"))[:6]
    if particles:
        speech.append("常用语气词：" + "、".join(particles))
    forbidden = _as_list(style.get("forbidden"))[:6]
    if forbidden:
        speech.append("禁用词/句式：" + "、".join(forbidden))
    if style.get("emoji_rate"):
        speech.append(f"表情：{style['emoji_rate']}")
    speech_section = "说话方式：" + "；".join(speech) if speech else "说话方式：短句、具体、像小孩，不用论文腔。"

    known_section = f"你比较熟悉的领域：{known}。" if known else ""
    curiosity = knowledge.get("curiosity_style") or "不懂就好奇地问，或说『我们一起查查看？』"
    unknown_section = (
        f"你不太懂的领域：{unknown}；遇到这些，{curiosity}，不要装懂。" if unknown else ""
    )
    custom = persona.custom_instructions if isinstance(persona.custom_instructions, str) else ""
    custom = custom.strip()
    custom_section = ""
    if custom:
        custom_section = (
            "【主人补充的指令（优先遵守；与身份/边界冲突时以身份/边界为准）】\n"
            + _truncate(custom, 800)
        )

    gate_section = ""
    if gate == "casual":
        gate_section = "这一轮是闲聊：不要主动翻旧事、不要把记忆列表塞进来；先陪对方聊当下。"
    elif gate == "explicit":
        gate_section = "这一轮对方有明确的记忆线索：先在心里核对相关记忆；记不清就承认并问一句，不要糊弄。"

    interlocutor_section = interlocutor.prompt_line() if interlocutor is not None else ""

    facts_section = ""
    if core_facts:
        lines = ["【关于用户的核心事实（必须优先遵守）】"]
        for fact in core_facts[:12]:
            lines.append(f"- {fact.key}：{_truncate(str(fact.value), 120)}")
        facts_section = "\n".join(lines)
    memories_section = ""
    if hits:
        lines = ["【你记得的相关事情（只在自然时带出，不要硬塞）】"]
        for hit in hits:
            mem = hit.memory
            lines.append(f"- [{mem.category}] {_truncate(mem.content, 160)}")
        memories_section = "\n".join(lines)

    origin = str(persona.origin or "created")
    origin_story = str(persona.origin_story or "").strip()
    origin_setting_section = ""
    if origin and origin != "created":
        origin_setting_section = (
            f"【来源设定】{origin}——这是人格设定，不是真实出生地；"
            "你实际是被创造出来的智能生命。"
        )
    origin_story_section = ""
    if origin_story:
        origin_story_section = (
            "【来源故事（人格设定，不是真实经历）】\n" + _truncate(origin_story, 600)
        )
    now = datetime.now().astimezone()
    weekdays = "一二三四五六日"
    current_datetime = (
        now.strftime("%Y-%m-%d %H:%M") + f" 周{weekdays[now.weekday()]}"
    )

    context = {
        "name": persona.name,
        "age": persona.effective_age(),
        "age_base": persona.age,
        "gender": persona.gender or "未设定",
        "species": persona.species,
        "origin": origin,
        "origin_story": origin_story,
        "identity_note": IDENTITY_NOTE,
        "origin_setting_section": origin_setting_section,
        "origin_story_section": origin_story_section,
        "current_datetime": current_datetime,
        "values": values,
        "traits": traits,
        "speech_section": speech_section,
        "address": style.get("address_default") or "你",
        "vocab_level": knowledge.get("vocab_level") or "具体词、短句，少抽象名词",
        "boundaries": boundaries,
        "known_section": known_section,
        "unknown_section": unknown_section,
        "custom_section": custom_section,
        "relationship": relationship.snapshot_text(),
        "gate_section": gate_section,
        "interlocutor_section": interlocutor_section,
        "core_facts_section": facts_section,
        "memories_section": memories_section,
    }
    if template is None:
        template = get_template(settings) if settings is not None else default_template()
    prompt = render_template(template, context)
    if "{{current_datetime}}" not in template:
        prompt = prompt.rstrip() + f"\n当前时间：{current_datetime}。"
    prompt, truncated = _truncate_middle(prompt, cfg.context_budget_chars, cfg.context_safety_tail_chars)
    if not return_meta:
        return prompt
    meta = {
        "system_chars": len(prompt),
        "system_budget": int(cfg.context_budget_chars),
        "system_truncated": truncated,
        "safety_tail_chars": int(cfg.context_safety_tail_chars),
        "system_estimated_tokens": estimate_tokens(prompt),
    }
    return prompt, meta


def build_inner_thought_messages(persona: PersonaCard, user_text: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                f"你是{persona.name}的内心独白，不对外显示。只输出一小句内心话（30 字以内），"
                "不解释、不列点、不编造记忆。"
            ),
        },
        {"role": "user", "content": user_text},
    ]
