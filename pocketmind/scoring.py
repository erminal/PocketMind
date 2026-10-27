"""数学/打分/遗忘的纯函数实现（不依赖数据库，便于单测）。

对照文档：docs/research/16-数学与公式手册.md。
所有阈值/权重来自 config.ScoringConfig，不在这里写死。
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime

import jieba

from .config import ScoringConfig
from .models import InterlocutorState, RelationshipState

# ---------------------------------------------------------------------------
# 一、向量与文本相似度
# ---------------------------------------------------------------------------

_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")
_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")


def tokens(text: str) -> set[str]:
    """中英混合的粗分词：英文/数字按词，中文按 jieba 词 + 2-gram 兜底。"""
    text = (text or "").lower().strip()
    if not text:
        return set()
    out: set[str] = set()
    for m in _WORD_RE.findall(text):
        if len(m) >= 2:
            out.add(m)
    for part in jieba.cut(text, cut_all=False):
        part = part.strip()
        if not part:
            continue
        if _CJK_RE.search(part):
            if len(part) >= 2:
                out.add(part)
            else:
                out.add(part)
    cjk = "".join(ch for ch in text if _CJK_RE.match(ch))
    if len(cjk) > 2:
        for i in range(len(cjk) - 1):
            out.add(cjk[i : i + 2])
    return out


def token_text(text: str) -> str:
    """给 FTS5 用的空格分隔 token 串（中文必须先分词）。"""
    return " ".join(sorted(tokens(text)))


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (na * nb)


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / len(a | b)


def text_similarity(a: str, b: str) -> float:
    return jaccard(tokens(a), tokens(b))


def is_duplicate(a: str, b: str, threshold: float) -> bool:
    return text_similarity(a, b) >= threshold


def is_noise(text: str, cfg: ScoringConfig) -> bool:
    t = (text or "").strip()
    if len(t) < cfg.noise_min_len or len(t) > cfg.noise_max_len:
        return True
    plain = t.strip("。！？!?，,、~～ ")
    noise = {
        "好的", "好", "嗯", "嗯嗯", "是的", "对", "谢谢", "多谢", "收到",
        "明白了", "知道了", "ok", "okay", "thanks", "thank you", "yes", "no",
        "在吗", "你好", "您好", "嗨", "hi", "hello",
    }
    return plain.lower() in noise


# ---------------------------------------------------------------------------
# 二、标量评分（情绪/严重度/重要度）
# ---------------------------------------------------------------------------

_POSITIVE = {
    "开心", "高兴", "喜欢", "爱", "棒", "可爱", "惊喜", "期待", "成功", "通过",
    "谢谢", "温暖", "舒服", "放心", "不错", "厉害", "满足", "笑", "好玩", "金毛",
    "好", "幸福", "安心", "骄傲", "夸", "奖励", "顺利",
}
_NEGATIVE = {
    "难过", "伤心", "烦", "累", "讨厌", "生气", "害怕", "焦虑", "痛", "哭",
    "失败", "糟糕", "压力", "孤单", "委屈", "崩溃", "吵架", "被放鸽子", "失望",
    "担心", "紧张", "不舒服", "生病", "疼", "失眠", "失业", "分手",
}
_SEVERE = {"崩溃", "自杀", "自残", "重大", "去世", "死亡", "住院", "手术", "家暴", "被开除"}
_MEDIUM = {"难过", "吵架", "失败", "被放鸽子", "生病", "分手", "失业", "失眠", "焦虑", "委屈"}
_PREF_WORDS = {"喜欢", "不喜欢", "讨厌", "爱吃", "不吃", "怕", "习惯", "想要", "希望"}
_EVENT_WORDS = {"今天", "昨天", "那天", "上次", "去了", "看到", "遇到", "发生", "记得", "一起"}
_PROMISE_WORDS = {"答应", "承诺", "一定", "约好", "下次", "提醒"}
_PIN_HINTS = {"我叫", "我的名字", "我住在", "我的生日", "我不吃", "我过敏", "我的项目", "我养了", "我的猫", "我的狗"}


def estimate_valence(text: str) -> float:
    t = text or ""
    pos = sum(1 for w in _POSITIVE if w in t)
    neg = sum(1 for w in _NEGATIVE if w in t)
    if pos + neg == 0:
        return 0.0
    return max(-1.0, min(1.0, (pos - neg) / (pos + neg)))


def estimate_severity(text: str) -> int:
    t = text or ""
    if any(w in t for w in _SEVERE):
        return 3
    if any(w in t for w in _MEDIUM):
        return 2
    return 1


def estimate_importance(text: str, valence: float, severity: int) -> float:
    t = text or ""
    score = 0.25
    score += min(0.35, abs(valence) * 0.35)
    score += {1: 0.0, 2: 0.12, 3: 0.25}.get(severity, 0.0)
    if any(w in t for w in ("第一次", "永远", "答应", "承诺", "生日", "秘密", "去世", "健康", "项目", "人生")):
        score += 0.2
    if len(t) >= 40:
        score += 0.05
    return round(max(0.05, min(1.0, score)), 3)


def base_score(valence: float, severity: int, cfg: ScoringConfig) -> float:
    weight = cfg.severity_weights.get(int(severity), 0.6)
    return round(cfg.base_score_scale * valence * weight, 3)


def classify_function(text: str) -> str:
    t = text or ""
    if any(w in t for w in _PREF_WORDS) or any(w in t for w in ("我叫", "我住在", "我的生日", "过敏")):
        return "directive"
    if any(w in t for w in _EVENT_WORDS) or any(w in t for w in ("我们", "一起", "那天")):
        return "social"
    return "self"


def classify_category(text: str) -> str:
    t = text or ""
    if any(w in t for w in _PROMISE_WORDS):
        return "PROMISE"
    if any(w in t for w in _PREF_WORDS):
        return "PREFERENCE"
    if any(w in t for w in ("我叫", "我住在", "我是", "我的生日", "我不吃", "我过敏")):
        return "FACT"
    if any(w in t for w in _EVENT_WORDS):
        return "EVENT"
    if any(w in t for w in ("项目", "工作", "代码", "论文", "考试")):
        return "PROJECT"
    return "OTHER"


def is_pin_candidate(text: str) -> bool:
    return any(w in (text or "") for w in _PIN_HINTS)


# ---------------------------------------------------------------------------
# 三、遗忘曲线 / 半衰期 / 降级
# ---------------------------------------------------------------------------

def retention(days: float, half_life: float) -> float:
    if half_life <= 0:
        return 1.0
    return 0.5 ** (max(0.0, days) / half_life)


def half_life_for(
    category: str,
    valence: float,
    severity: int,
    importance: float,
    anchor: bool,
    cfg: ScoringConfig,
) -> float:
    """按类别 + 情绪 + 重要度 + 锚点给半衰期（天）。"""
    cat = (category or "OTHER").upper()
    if cat in {"CORE_FACT"}:
        h = cfg.half_life_core
    elif cat in {"PREFERENCE", "BOUNDARY"}:
        h = cfg.half_life_constraint
    elif anchor:
        h = cfg.half_life_anchor
    elif cat in {"PROMISE", "PROJECT"}:
        h = cfg.half_life_important_positive
    elif cat == "FACT":
        h = cfg.half_life_constraint
    elif valence <= -0.7 and severity >= 3:
        # 严重负面要先于 EVENT 判断，否则 EVENT 会覆盖严重负面分支
        h = cfg.half_life_severe_negative
    elif valence <= -0.3:
        h = cfg.half_life_minor_negative
    elif cat == "EVENT":
        h = cfg.half_life_episodic
    elif valence >= 0.5 and importance >= 0.6:
        h = cfg.half_life_important_positive
    elif importance <= 0.2:
        h = cfg.half_life_low_info
    else:
        h = cfg.half_life_episodic
    # 重要度/情绪加成（最多约 2 倍）
    h *= 1.0 + 0.5 * max(0.0, importance - 0.3) + 0.2 * abs(valence)
    if anchor:
        h = max(h, cfg.half_life_anchor)
    return float(max(1.0, h))


def retention_stage(score: float, cfg: ScoringConfig) -> str:
    if score >= 1.0 - 1e-9:
        return "full"
    if score >= 0.8:
        return "full"
    if score >= cfg.forget_summary_below:
        return "summary"
    if score >= cfg.forget_gist_below:
        return "gist"
    if score >= cfg.forget_archive_below:
        return "archive"
    return "forgotten"


def recency_score(days: float, half_life_days: float = 14.0) -> float:
    return retention(days, half_life_days)


def affect_match(memory_valence: float, current_valence: float) -> float:
    return max(0.0, min(1.0, 1.0 - abs(memory_valence - current_valence) / 2.0))


_EMOTION_CUES: dict[str, tuple[str, ...]] = {
    "joy": ("开心", "高兴", "太好了", "棒", "喜欢", "笑", "幸福", "惊喜", "满足"),
    "sad": ("难过", "伤心", "哭", "失望", "委屈", "孤独", "孤单", "想哭"),
    "anxious": ("焦虑", "紧张", "担心", "害怕", "压力", "睡不着", "慌"),
    "angry": ("生气", "烦", "讨厌", "气死", "火大", "吵架"),
    "tired": ("累", "疲惫", "没力气", "困", "加班", "熬夜"),
    "fear": ("害怕", "恐惧", "吓", "不敢"),
}
_AROUSAL = {"joy": 0.7, "sad": 0.3, "anxious": 0.8, "angry": 0.8, "tired": 0.2, "fear": 0.8, "neutral": 0.3}


def infer_interlocutor_state(text: str) -> InterlocutorState:
    """轻量情绪推断：M1 规则版，输出标签+置信度；低置信度时提示「先问一句」。

    真实模型可用结构化输出替代；无论哪种，置信度都要随结果落日志。
    """
    t = text or ""
    scores: dict[str, int] = {}
    for label, cues in _EMOTION_CUES.items():
        hit = sum(1 for cue in cues if cue in t)
        if hit:
            scores[label] = hit
    if not scores:
        # 有感叹/问号但无情绪词，视为不确定
        ambiguous = any(ch in t for ch in ("！", "!", "？", "?")) and len(t) >= 6
        return InterlocutorState(
            emotion="neutral",
            valence=0.0,
            arousal=0.3,
            confidence=0.25 if ambiguous else 0.4,
            should_ask=ambiguous,
        )
    label = max(scores, key=scores.get)
    hits = scores[label]
    confidence = min(0.9, 0.45 + 0.15 * hits + (0.05 if len(t) >= 14 else 0.0))
    valence = classify_valence(label)
    return InterlocutorState(
        emotion=label,
        valence=valence,
        arousal=_AROUSAL.get(label, 0.4),
        confidence=round(confidence, 2),
        should_ask=confidence < 0.55,
    )


def classify_valence(label: str) -> float:
    return {"joy": 0.8, "sad": -0.8, "anxious": -0.6, "angry": -0.7, "tired": -0.3, "fear": -0.7}.get(label, 0.0)


# ---------------------------------------------------------------------------
# 四、关系更新
# ---------------------------------------------------------------------------

def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def update_relationship(
    state: RelationshipState,
    base: float,
    importance: float,
    cfg: ScoringConfig,
    event_type: str = "event",
) -> tuple[RelationshipState, list[dict]]:
    """按事件分更新多维关系；返回新状态与变更列表（写 relationship_events）。"""
    changes: list[dict] = []
    raw = cfg.favorability_alpha * base * importance
    delta = clamp(raw, -cfg.favorability_event_cap, cfg.favorability_event_cap)
    if abs(delta) >= 0.01:
        new_fav = clamp(state.favorability + delta, -100.0, 100.0)
        changes.append({"field": "favorability", "delta": round(new_fav - state.favorability, 3), "reason": event_type})
        state = replace(state, favorability=round(new_fav, 3))

    trust_delta = 0.0
    if event_type in {"promise_kept", "promise_made"}:
        trust_delta = cfg.trust_alpha
    elif event_type in {"conflict", "betrayal"}:
        trust_delta = -cfg.trust_alpha * 2
    elif event_type == "secret":
        trust_delta = cfg.trust_alpha * 0.5
    if abs(trust_delta) > 0:
        new_trust = clamp(state.trust + trust_delta, 0.0, 1.0)
        changes.append({"field": "trust", "delta": round(new_trust - state.trust, 3), "reason": event_type})
        state = replace(state, trust=new_trust)

    mood = clamp(0.6 * state.mood + 0.4 * (1.0 if base > 0 else -1.0) * min(1.0, abs(base) / 10.0), -1.0, 1.0)
    if abs(mood - state.mood) >= 0.01:
        changes.append({"field": "mood", "delta": round(mood - state.mood, 3), "reason": "event_aftereffect"})
        state = replace(state, mood=round(mood, 3))
    return state, changes


# ---------------------------------------------------------------------------
# 五、重排
# ---------------------------------------------------------------------------

def combine_scores(
    semantic: float,
    keyword: float,
    affect: float,
    importance: float,
    recency: float,
    relation: float,
    cfg: ScoringConfig,
) -> float:
    sem = max(semantic, keyword * 0.8)  # 关键词是语义的兜底
    total_w = cfg.w_sem + cfg.w_aff + cfg.w_imp + cfg.w_rec + cfg.w_rel
    if total_w <= 0:
        total_w = 1.0
    return round(
        (
            cfg.w_sem * sem
            + cfg.w_aff * affect
            + cfg.w_imp * importance
            + cfg.w_rec * recency
            + cfg.w_rel * relation
        )
        / total_w,
        6,
    )


def days_since(iso_time: str, now: datetime | None = None) -> float:
    if not iso_time:
        return 0.0
    try:
        dt = datetime.fromisoformat(iso_time)
    except ValueError:
        return 0.0
    now = now or datetime.now(dt.tzinfo)
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def recall_reward(base: float, count: int, cfg: ScoringConfig) -> float:
    return round(cfg.recall_k * base * math.exp(-cfg.recall_lambda * max(0, count)), 4)


def anticipation_reward(p_success: float, r_if_realize: float, count: int, cfg: ScoringConfig) -> float:
    p = clamp(p_success, cfg.anticipation_p_min, cfg.anticipation_p_max)
    return round(p * r_if_realize * cfg.recall_k * math.exp(-cfg.recall_lambda * max(0, count)), 4)


def touchable_channels(*channels: str) -> list[str]:
    return [c for c in channels if c]


def unique_keep_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
