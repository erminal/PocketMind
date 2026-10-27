"""数据模型（纯 dataclass，不依赖数据库）。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass
class PersonaCard:
    entity_id: str
    name: str
    age: int = 7
    gender: str = "女"
    species: str = "人类小孩（设定）"
    origin: str = "created"  # 被创造的智能生命；不是自然人
    values: list[str] = field(default_factory=list)
    traits: list[str] = field(default_factory=list)
    speech_style: dict[str, Any] = field(default_factory=dict)
    boundaries: list[str] = field(default_factory=list)
    knowledge_boundary: dict[str, Any] = field(default_factory=dict)
    origin_story: str = ""
    custom_instructions: str = ""
    # 生日自动增长（心智年龄的“角色年龄”）：birthday=MM-DD；
    # age_anchor_date=开启/重置时的日期，age_base=当时的年龄；
    # 之后每经过一个生日 +1 岁。默认关闭，旧数据不受影响。
    age_tracking: bool = False
    birthday: str = ""
    age_anchor_date: str = ""
    age_base: int = 0
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return _json(self.to_dict())

    def effective_age(self, today: date | None = None) -> int:
        """实际展示的年龄：关闭开关时=age；开启时=age_base+经过的生日次数。"""
        if not self.age_tracking or not self.birthday:
            return int(self.age)
        today = today or date.today()
        try:
            anchor = date.fromisoformat(self.age_anchor_date) if self.age_anchor_date else today
        except ValueError:
            anchor = today
        parts = self.birthday.strip().split("-")
        if len(parts) == 3:  # 兼容 YYYY-MM-DD
            parts = parts[1:]
        if len(parts) != 2:
            return int(self.age)
        try:
            month, day = int(parts[0]), int(parts[1])
        except ValueError:
            return int(self.age)
        base = int(self.age_base or self.age)
        count = 0
        for year in range(anchor.year, today.year + 1):
            try:
                anniversary = date(year, month, day)
            except ValueError:
                anniversary = date(year, 2, 28)
            if anchor < anniversary <= today:
                count += 1
        return base + count

    def brief(self, max_traits: int = 3) -> str:
        traits = "、".join(self.traits[:max_traits]) or "慢热、好奇"
        return f"{self.name}，{self.effective_age()} 岁，{self.species}；{traits}。"


@dataclass
class InterlocutorState:
    emotion: str = "neutral"       # joy/sad/anxious/angry/tired/fear/neutral
    valence: float = 0.0
    arousal: float = 0.3
    confidence: float = 0.3
    source: str = "heuristic"
    should_ask: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def prompt_line(self) -> str:
        label = {
            "joy": "开心", "sad": "难过", "anxious": "焦虑", "angry": "有点生气",
            "tired": "累", "fear": "害怕", "neutral": "平静",
        }.get(self.emotion, self.emotion)
        ask = "；不确定就先问一句，不要硬猜" if self.should_ask else ""
        return f"对方现在看起来：{label}（置信度 {self.confidence:.0%}{ask}）。"


@dataclass
class RelationshipState:
    entity_id: str
    favorability: float = 0.0       # -100..100，展示用主指标
    trust: float = 0.5
    closeness: float = 0.3
    boundary_respect: float = 0.5
    stage: str = "acquaintance"
    mood: float = 0.0               # -1..1 当前会话临时心境
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def snapshot_text(self) -> str:
        return (
            f"关系阶段：{self.stage}；好感度（展示）：{self.favorability:.0f}；"
            f"信任：{self.trust:.2f}；亲近：{self.closeness:.2f}；"
            f"尊重边界：{self.boundary_respect:.2f}；当前心境：{self.mood:+.2f}"
        )


@dataclass
class Event:
    id: str
    entity_id: str
    conversation_id: str
    turn_id: str
    occurred_at: str
    who: str                      # user | agent | inferred | system
    kind: str                     # statement | event | preference | promise | correction
    summary: str
    detail: str = ""
    valence: float = 0.0
    severity: int = 1
    importance: float = 0.3
    base_score: float = 0.0
    function: str = "social"       # self | social | directive
    anchor_flag: int = 0
    pinned: int = 0
    source_quote: str = ""
    confidence: float = 0.8


@dataclass
class MemoryItem:
    id: str
    entity_id: str
    event_id: str
    layer: str                    # L1 short / L2 long
    title: str
    summary: str
    gist: str
    content: str
    category: str = "OTHER"
    topic: str = ""
    function: str = "social"
    valence: float = 0.0
    severity: int = 1
    importance: float = 0.3
    base_score: float = 0.0
    half_life_days: float = 30.0
    distinctiveness: float = 0.5
    density: float = 0.5
    status: str = "active"        # active | superseded | archived | forgotten
    pinned: int = 0
    embedding: list[float] = field(default_factory=list)
    embedding_model: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_recalled_at: str = ""
    recall_count: int = 0
    used_count: int = 0
    replay_count: int = 0
    superseded_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["embedding"] = list(self.embedding)
        return data


@dataclass
class CoreFact:
    id: str
    entity_id: str
    key: str
    value: str
    source_event_id: str = ""
    pinned_by: str = "auto"
    valid_from: str = ""
    valid_to: str = ""
    confidence: float = 1.0
    status: str = "active"
    superseded_by: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass
class RetrievalHit:
    memory: MemoryItem
    semantic: float = 0.0
    keyword: float = 0.0
    affect_match: float = 0.0
    importance: float = 0.0
    recency: float = 0.0
    relation: float = 0.0
    final: float = 0.0
    channels: list[str] = field(default_factory=list)

    def to_log(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory.id,
            "content": self.memory.content[:200],
            "semantic": round(self.semantic, 4),
            "keyword": round(self.keyword, 4),
            "affect_match": round(self.affect_match, 4),
            "importance": round(self.importance, 4),
            "recency": round(self.recency, 4),
            "relation": round(self.relation, 4),
            "final": round(self.final, 4),
            "channels": self.channels,
        }


@dataclass
class RetrievalResult:
    hits: list[RetrievalHit]
    core_facts: list[CoreFact]
    relationship: RelationshipState
    log_id: str
    query: str
    method: str = "hybrid"
    gate: str = "normal"
    gate_reason: str = ""


@dataclass
class TurnResult:
    conversation_id: str
    turn_id: str
    reply: str
    used_memories: list[dict[str, Any]]
    retrieval_log_id: str
    saved_memories: int = 0
    inner_thought: str = ""
    diary_path: str = ""
    interlocutor: dict[str, Any] = field(default_factory=dict)
    gate: str = ""
    image_caption: str = ""
    media_path: str = ""
    relationship_before: dict[str, Any] = field(default_factory=dict)
    relationship_after: dict[str, Any] = field(default_factory=dict)
    relationship_delta: dict[str, float] = field(default_factory=dict)
    event_scores: dict[str, Any] = field(default_factory=dict)
    transient: dict[str, Any] = field(default_factory=dict)
    context_meta: dict[str, Any] = field(default_factory=dict)
    energy: float = 100.0
    sleeping: bool = False
    sleep_state: str = "awake"
    unread_count: int = 0
    wake_greeting: dict[str, Any] = field(default_factory=dict)
    mock: bool = True
    error: str = ""


@dataclass
class DiaryEntry:
    id: str
    entity_id: str
    date: str
    content: str
    created_at: str
    updated_at: str
