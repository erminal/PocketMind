"""多视图检索：语义 + 关键词/FTS + 情绪带 + 新近度 + 关系，最后重排并留痕。"""

from __future__ import annotations

import json

from .config import Settings
from .db import Database, new_id, now_iso
from .llm import FallbackLLM
from .memory import MemoryService
from .models import RetrievalHit, RetrievalResult
from .persona import PersonaService
from .scoring import (
    affect_match,
    combine_scores,
    cosine,
    days_since,
    recency_score,
    tokens,
)


class RetrievalService:
    def __init__(self, db: Database, settings: Settings, llm: FallbackLLM, memory: MemoryService, persona: PersonaService):
        self.db = db
        self.settings = settings
        self.llm = llm
        self.memory = memory
        self.persona = persona
        self.cfg = settings.scoring

    def decide_gate(self, query: str) -> tuple[str, str]:
        """记忆门：显式线索必查；短闲聊少查；普通陈述中等。

        M1 先做规则版（可解释、可回放）；后续可用小模型/分类器替换，
        但 gate 决策必须写进 retrieval_logs。
        """
        text = (query or "").strip()
        lowered = text.lower()
        for cue in self.cfg.gate_explicit_cues:
            if cue.lower() in lowered:
                return "explicit", f"cue:{cue}"
        greetings = {"你好", "嗨", "hi", "hello", "在吗", "早上好", "晚上好", "晚安", "哈哈", "嗯"}
        if len(text) <= self.cfg.gate_casual_max_len or text in greetings:
            return "casual", "short_or_greeting"
        return "normal", "default"

    def retrieve(
        self,
        entity_id: str,
        conversation_id: str,
        turn_id: str,
        query: str,
        current_valence: float = 0.0,
        current_function: str = "social",
        limit: int | None = None,
    ) -> RetrievalResult:
        gate, gate_reason = self.decide_gate(query)
        if limit is None:
            limit = {
                "casual": self.cfg.gate_casual_limit,
                "normal": self.cfg.gate_normal_limit,
                "explicit": self.cfg.recall_limit,
            }.get(gate, self.cfg.gate_normal_limit)
        rows = self._candidate_rows(entity_id)
        memories = [self.memory.row_to_memory(r) for r in rows]
        core = self.memory.core_facts(entity_id)
        relationship = self.persona.relationship(entity_id)
        query_vec: list[float] = []
        try:
            query_vec = self.llm.embed([query])[0]
        except Exception:
            query_vec = []

        fts_scores = self._fts_scores(query)
        q_tokens = tokens(query)
        hits: list[RetrievalHit] = []
        for mem in memories:
            semantic = 0.0
            if query_vec and mem.embedding and len(query_vec) == len(mem.embedding):
                semantic = max(0.0, cosine(query_vec, mem.embedding))
            kw = max(
                _jaccard(q_tokens, tokens(mem.content)),
                fts_scores.get(mem.id, 0.0),
            )
            aff = affect_match(mem.valence, current_valence)
            imp = mem.importance
            # 新近度使用记忆自身半衰期，而不是固定 14 天：
            # 远期重要/核心记忆不应被 14 天固定尺度系统性压低。
            rec = recency_score(days_since(mem.created_at), mem.half_life_days or 14.0)
            rel = 1.0 if mem.function == current_function else 0.55
            channels = []
            if semantic >= self.cfg.vector_min_score:
                channels.append("semantic")
            if kw > 0:
                channels.append("keyword")
            if abs(mem.valence - current_valence) <= 0.5 and (mem.valence != 0 or current_valence != 0):
                channels.append("affect")
            if rec >= 0.5:
                channels.append("recent")
            if not channels:
                # 仍保留作为候选（低分），便于日志观察；但最终阈值会过滤
                channels.append("recency_floor")
            final = combine_scores(semantic, kw, aff, imp, rec, rel, self.cfg)
            hits.append(
                RetrievalHit(
                    memory=mem,
                    semantic=round(semantic, 4),
                    keyword=round(kw, 4),
                    affect_match=round(aff, 4),
                    importance=round(imp, 4),
                    recency=round(rec, 4),
                    relation=round(rel, 4),
                    final=final,
                    channels=channels,
                )
            )
        hits.sort(key=lambda h: h.final, reverse=True)
        selected = [h for h in hits if h.final >= self.cfg.final_min_score][:limit]
        if not selected and hits and any(h.keyword > 0.25 or h.semantic > 0.5 for h in hits[:1]):
            selected = hits[:1]
        candidates = hits[: self.cfg.candidate_limit]
        log_id = new_id("rl")
        self.db.execute(
            """INSERT INTO retrieval_logs(id,entity_id,conversation_id,turn_id,at,query,candidates_json,
               selected_json,method,weights_version,embedding_model,gate)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                log_id,
                entity_id,
                conversation_id,
                turn_id,
                now_iso(),
                query,
                json.dumps([h.to_log() for h in candidates], ensure_ascii=False),
                json.dumps([h.to_log() for h in selected], ensure_ascii=False),
                "hybrid",
                f"w:{self.cfg.w_sem}/{self.cfg.w_aff}/{self.cfg.w_imp}/{self.cfg.w_rec}/{self.cfg.w_rel}",
                "",
                f"{gate}:{gate_reason}",
            ),
        )
        return RetrievalResult(
            hits=selected,
            core_facts=core,
            relationship=relationship,
            log_id=log_id,
            query=query,
            method="hybrid",
            gate=gate,
            gate_reason=gate_reason,
        )

    def _candidate_rows(self, entity_id: str) -> list[dict]:
        """候选池 = 最近 N ∪ 高重要度/高情绪/pin 的并集。

        只取最近 300 条会让 90 天前的重要记忆永远进不了候选；
        这里先用最近窗口保底，再补少量高优先记忆。
        """
        recent = self.db.fetchall(
            "SELECT * FROM memories WHERE entity_id=? AND status='active' ORDER BY created_at DESC LIMIT ?",
            (entity_id, max(1, int(self.cfg.candidate_recent_limit))),
        )
        priority = self.db.fetchall(
            """SELECT * FROM memories
               WHERE entity_id=? AND status='active'
               ORDER BY pinned DESC, importance DESC, abs(valence) DESC
               LIMIT ?""",
            (entity_id, max(1, int(self.cfg.candidate_priority_limit))),
        )
        emotional = self.db.fetchall(
            """SELECT * FROM memories
               WHERE entity_id=? AND status='active'
               ORDER BY abs(valence) DESC, importance DESC
               LIMIT ?""",
            (entity_id, max(1, int(self.cfg.candidate_priority_limit))),
        )
        merged: dict[str, dict] = {}
        for row in recent + priority + emotional:
            merged.setdefault(row["id"], row)
        return list(merged.values())

    def _fts_scores(self, query: str) -> dict[str, float]:
        q = " OR ".join(sorted(tokens(query)))
        if not q:
            return {}
        try:
            rows = self.db.fetchall(
                "SELECT memory_id, bm25(memory_fts) AS rank FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT 30",
                (q,),
            )
        except Exception:
            return {}
        if not rows:
            return {}
        # bm25 越小越相关；归一成 0~1（第一名 1.0，后面按序递减）
        out: dict[str, float] = {}
        for i, row in enumerate(rows):
            out[row["memory_id"]] = max(0.0, 1.0 - i / max(1, len(rows)))
        return out

    def mark_selected_used(self, result: RetrievalResult) -> None:
        for hit in result.hits:
            self.memory.mark_used(hit.memory.id)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
