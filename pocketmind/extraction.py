"""记忆抽取：mock 规则 + 真实 LLM 的 prompt/解析。

M1 设计：
- 抽取与回复由不同调用完成（防记忆污染）；
- 无 API Key 时用启发式规则，保证离线 demo/测试；
- 有 Key 时用 DeepSeek/OpenAI 兼容接口，提示词要求严格 JSON；
- 所有抽取结果仍要经过代码去重/冲突/上限（模型只负责“说出变化”）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .config import ScoringConfig
from .scoring import (
    classify_category,
    classify_function,
    estimate_importance,
    estimate_severity,
    estimate_valence,
    is_noise,
    is_pin_candidate,
)


@dataclass
class ExtractedFact:
    content: str
    category: str = "OTHER"
    topic: str = ""
    valence: float = 0.0
    severity: int = 1
    importance: float = 0.3
    function: str = "social"
    pinned: bool = False
    core_key: str = ""
    title: str = ""
    gist: str = ""
    confidence: float = 0.8
    metadata: dict[str, Any] = field(default_factory=dict)


_SENTENCE_RE = re.compile(r"[^。！？!?；;，,\n]+[。！？!?；;，,]?")
_PREFERRED_OBJECT = re.compile(r"(?:我喜欢|我不喜欢|我爱吃|我不吃|我讨厌|我怕|我习惯|我想要|我希望)([^，。！？!?；;\n]{1,24})")


def _split_sentences(text: str) -> list[str]:
    return [s.strip(" \t\r\n，,。") for s in _SENTENCE_RE.findall(text or "") if s.strip()]


def _core_key(sentence: str) -> tuple[str, str]:
    """返回 (core_key, topic)；只有明确的核心事实才 pin。"""
    patterns = [
        (r"我叫([^，。！？!?；;\s]{1,16})", "name"),
        (r"我的名字(?:是|叫)([^，。！？!?；;\s]{1,16})", "name"),
        (r"我住在([^，。！？!?；;\s]{1,20})", "city"),
        (r"我的生日(?:是)?([^，。！？!?；;\s]{1,20})", "birthday"),
        (r"我(?:对)?([^，。！？!?；;\s]{1,12})过敏", "allergy"),
        (r"我(?:养了|有一只|有一只叫)([^，。！？!?；;\s]{1,20})", "pet"),
    ]
    for pattern, key in patterns:
        m = re.search(pattern, sentence)
        if m:
            return key, key
    m = _PREFERRED_OBJECT.search(sentence)
    if m:
        obj = m.group(1).strip()
        if "不吃" in sentence or "讨厌" in sentence or "怕" in sentence:
            return "", f"diet:{obj}"
        return "", f"preference:{obj}"
    return "", ""


def to_second_person(text: str) -> str:
    """把用户第一人称叙述转成记忆里的第二人称（避免角色把用户的事当成自己的）。"""
    t = (text or "").strip()
    # 先处理常见「我 + 动词 + 你」的组合，避免变成「你答应你」
    for src, dst in (
        ("我答应你", "你答应"),
        ("我告诉你", "你告诉"),
        ("我帮你", "你帮"),
        ("我给你", "你给"),
        ("我陪你", "你陪"),
    ):
        t = t.replace(src, dst)
    t = t.replace("我的", "你的")
    t = re.sub(r"我(?!们)", "你", t)
    return t


def _looks_like_question(sentence: str) -> bool:
    s = sentence.strip()
    return s.endswith(("吗", "？", "?", "呢")) or any(w in s for w in ("还记得", "是什么", "在哪", "为什么", "怎么"))


def extract_mock(text: str, cfg: ScoringConfig) -> list[ExtractedFact]:
    """离线启发式抽取：足够跑通「我叫/住/喜欢/不吃/事件/承诺」。"""
    facts: list[ExtractedFact] = []
    for sentence in _split_sentences(text):
        if is_noise(sentence, cfg) or _looks_like_question(sentence):
            continue
        category = classify_category(sentence)
        function = classify_function(sentence)
        valence = estimate_valence(sentence)
        severity = estimate_severity(sentence)
        importance = estimate_importance(sentence, valence, severity)
        core_key, topic = _core_key(sentence)
        pinned = bool(core_key) or is_pin_candidate(sentence)
        if pinned:
            importance = max(importance, 0.8)
            category = "FACT" if core_key else category
        if category == "PROMISE":
            importance = max(importance, 0.7)
        if category == "EVENT" and importance < 0.3:
            importance = 0.35
        content = to_second_person(sentence)
        # 标题/要旨：短句 + 前几个词，不做花哨生成
        title = content[:24] + ("…" if len(content) > 24 else "")
        gist = " ".join(list(dict.fromkeys(re.findall(r"[\u4e00-\u9fff]{2,4}", content)))[:5])
        facts.append(
            ExtractedFact(
                content=content,
                category=category,
                topic=topic,
                valence=valence,
                severity=severity,
                importance=importance,
                function=function,
                pinned=pinned,
                core_key=core_key,
                title=title,
                gist=gist,
                confidence=0.82 if pinned else 0.7,
                metadata={"extractor": "heuristic"},
            )
        )
        if len(facts) >= cfg.max_new_per_turn:
            break
    return facts


EXTRACTION_SYSTEM = """你是一个「记忆抽取器」，不是对话者。你的唯一任务是从用户本轮消息中抽取值得长期记住的内容。
严格要求：
1. 只输出 JSON，不要解释、不要 Markdown。
2. 只从用户说的话里抽取；不要把助手说过的话、你的推测、常识当作记忆。
3. 每条内容必须是一句独立、脱离上下文也能读懂的话，尽量保留具体锚点（时间/地点/人物/原话）。
4. 不确定的信息不要写；宁可少写。
5. content 用第二人称记录用户（例如「你喜欢吃草莓」「你住在杭州」），不要用「我」。
6. 输出格式：
{"facts":[{"content":"...","category":"FACT|PREFERENCE|EVENT|PERSON|PROJECT|DECISION|PROMISE|BOUNDARY|OTHER","topic":"简短主题","valence":-1.0,"severity":1,"importance":0.5,"function":"self|social|directive","pinned":false,"core_key":"","title":"...","gist":"...","confidence":0.8}]}
7. valence ∈ [-1,1]；severity ∈ {1,2,3}；importance ∈ [0,1]；pinned 只在身份/健康/禁忌/项目参数/重要约定时为 true。"""


def build_extraction_messages(user_text: str, existing: list[str] | None = None) -> list[dict[str, str]]:
    existing = existing or []
    extra = ""
    if existing:
        extra = "\n已有记忆（避免重复）：\n" + "\n".join(f"- {x[:120]}" for x in existing[:20])
    return [
        {"role": "system", "content": EXTRACTION_SYSTEM + extra},
        {"role": "user", "content": user_text},
    ]


def parse_extraction_json(raw: str) -> list[dict[str, Any]]:
    """从模型输出里稳健地取出 JSON；失败返回空列表。"""
    if not raw:
        return []
    text = raw.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return []
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return []
    facts = data.get("facts") if isinstance(data, dict) else None
    return [f for f in facts if isinstance(f, dict)] if isinstance(facts, list) else []


def fact_from_llm_dict(item: dict[str, Any], cfg: ScoringConfig) -> ExtractedFact | None:
    content = to_second_person(str(item.get("content", "")).strip())
    if not content or is_noise(content, cfg):
        return None
    try:
        valence = float(item.get("valence", 0.0))
    except (TypeError, ValueError):
        valence = 0.0
    try:
        severity = int(item.get("severity", 1))
    except (TypeError, ValueError):
        severity = 1
    severity = max(1, min(3, severity))
    try:
        importance = float(item.get("importance", 0.3))
    except (TypeError, ValueError):
        importance = 0.3
    importance = max(0.0, min(1.0, importance))
    category = str(item.get("category", "OTHER")).upper()
    function = str(item.get("function", "social"))
    return ExtractedFact(
        content=content,
        category=category,
        topic=str(item.get("topic", "")),
        valence=max(-1.0, min(1.0, valence)),
        severity=severity,
        importance=importance,
        function=function,
        pinned=bool(item.get("pinned", False)),
        core_key=str(item.get("core_key", "")),
        title=str(item.get("title", "")) or content[:24],
        gist=str(item.get("gist", "")),
        confidence=float(item.get("confidence", 0.7) or 0.7),
        metadata={"extractor": "llm"},
    )
