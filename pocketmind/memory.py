"""记忆写入与生命周期：事件 → 抽取 → 去重/冲突 → 入库 → 遗忘降级 → 印象。

关键设计（对应 02/04/16 文档）：
- 归因 who：只从 user 消息抽取；agent/system 不进用户档案；
- 去重：本地 token Jaccard，不花 token；
- 冲突：同 topic 新值胜出，旧值 superseded（不物理删除）；
- 核心事实 pin：不衰减；写 core_facts；
- 半衰期按类别/情绪/重要度/锚点计算；遗忘只降级不删除；
- 印象/疤痕只存标签+强度+来源，不存细节向量。
"""

from __future__ import annotations

import json
from datetime import datetime

from .config import Settings
from .db import Database, new_id, now_iso
from .extraction import ExtractedFact, fact_from_llm_dict
from .llm import FallbackLLM, extract_with_llm
from .models import CoreFact, Event, MemoryItem
from .scoring import (
    base_score,
    classify_function,
    cosine,
    days_since,
    estimate_importance,
    estimate_severity,
    estimate_valence,
    half_life_for,
    is_duplicate,
    is_noise,
    retention,
    retention_stage,
)


class MemoryService:
    def __init__(self, db: Database, settings: Settings, llm: FallbackLLM):
        self.db = db
        self.settings = settings
        self.llm = llm
        self.cfg = settings.scoring

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def ingest_user_event(self, entity_id: str, conversation_id: str, turn_id: str, text: str) -> Event:
        text = (text or "").strip()
        valence = estimate_valence(text)
        severity = estimate_severity(text)
        importance = estimate_importance(text, valence, severity)
        event = Event(
            id=new_id("ev"),
            entity_id=entity_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            occurred_at=now_iso(),
            who="user",
            kind="statement",
            summary=text,
            detail=text,
            valence=valence,
            severity=severity,
            importance=importance,
            base_score=base_score(valence, severity, self.cfg),
            function=classify_function(text),
            anchor_flag=0,
            pinned=0,
            source_quote=text[:200],
            confidence=0.8,
        )
        self.db.execute(
            """INSERT INTO events(id,entity_id,conversation_id,turn_id,occurred_at,who,kind,summary,detail,
               valence,severity,importance,base_score,function,anchor_flag,pinned,source_quote,confidence)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event.id, event.entity_id, event.conversation_id, event.turn_id, event.occurred_at,
                event.who, event.kind, event.summary, event.detail, event.valence, event.severity,
                event.importance, event.base_score, event.function, event.anchor_flag,
                event.pinned, event.source_quote, event.confidence,
            ),
        )
        return event

    # ------------------------------------------------------------------
    # 抽取与写入
    # ------------------------------------------------------------------
    def extract_and_store(self, event: Event) -> list[MemoryItem]:
        """从用户事件抽取记忆；失败不阻塞聊天。"""
        existing_rows = self.db.fetchall(
            "SELECT id,content,topic,category,status,embedding_json FROM memories WHERE entity_id=? AND status='active' ORDER BY created_at DESC LIMIT 200",
            (event.entity_id,),
        )
        existing_texts = [r["content"] for r in existing_rows]
        raw_facts: list[dict] = []
        try:
            raw_facts = extract_with_llm(self.llm, event.summary, existing_texts)
        except Exception:
            raw_facts = []
        facts: list[ExtractedFact] = []
        if raw_facts:
            for item in raw_facts[: self.cfg.max_new_per_turn]:
                f = fact_from_llm_dict(item, self.cfg)
                if f:
                    facts.append(f)
        else:
            # 真实模型没给/失败时用启发式兜底；至少让「我叫/喜欢」能记
            from .extraction import extract_mock
            facts = extract_mock(event.summary, self.cfg)
        if not facts:
            return []

        # 批量算向量（失败不阻塞；无向量仍可关键词召回）
        vectors: list[list[float]] = []
        embedding_model = ""
        try:
            vectors = self.llm.embed([f.content for f in facts])
            embedding_model = "mock" if self.llm.is_mock else self.settings.embedding_model
        except Exception:
            vectors = []

        saved: list[MemoryItem] = []
        for idx, fact in enumerate(facts):
            if is_noise(fact.content, self.cfg):
                continue
            conflict = self._has_topic_conflict(fact, existing_rows)
            if self._is_duplicate(fact.content, existing_texts) and not conflict:
                continue
            vector = vectors[idx] if idx < len(vectors) else []
            memory = self._insert_memory(event, fact, vector, embedding_model, existing_rows)
            if memory:
                saved.append(memory)
                existing_texts.append(fact.content)
        return saved

    def _has_topic_conflict(self, fact: ExtractedFact, existing_rows: list[dict]) -> bool:
        return any(
            row["status"] == "active"
            and row.get("topic")
            and row["topic"] == fact.topic
            and row["content"].strip() != fact.content.strip()
            for row in existing_rows
        )

    def _is_duplicate(self, content: str, existing_texts: list[str]) -> bool:
        return any(is_duplicate(content, other, self.cfg.dedup_threshold) for other in existing_texts)

    def add_media_memory(self, event: Event, caption: str, media_path: str = "") -> MemoryItem | None:
        """图片 caption 作为一条 MEDIA 记忆；原图不注入 prompt，只留路径。"""
        content = f"[图片] {caption}".strip()
        if not content or content == "[图片]":
            return None
        fact = ExtractedFact(
            content=content,
            category="MEDIA",
            topic="",
            valence=0.0,
            severity=1,
            importance=0.5,
            function="social",
            pinned=False,
            title="一张图片",
            gist=caption[:80],
            confidence=0.8,
            metadata={"media_path": media_path},
        )
        vector: list[float] = []
        embedding_model = ""
        try:
            vectors = self.llm.embed([content])
            vector = vectors[0] if vectors else []
            embedding_model = "mock" if self.llm.is_mock else self.settings.embedding_model
        except Exception:
            vector = []
        return self._insert_memory(event, fact, vector, embedding_model, [])

    def _insert_memory(
        self,
        event: Event,
        fact: ExtractedFact,
        vector: list[float],
        embedding_model: str,
        existing_rows: list[dict],
    ) -> MemoryItem | None:
        now = now_iso()
        category = fact.category or "OTHER"
        pinned = 1 if fact.pinned or fact.core_key else 0
        anchor = 1 if fact.core_key in {"name", "birthday", "city", "allergy"} else 0
        half_life = half_life_for(category, fact.valence, fact.severity, fact.importance, bool(anchor), self.cfg)
        if pinned:
            half_life = self.cfg.half_life_core

        # 冲突：同 topic 的旧记忆标记 superseded（不物理删除）
        conflict_old = None
        if fact.topic:
            for row in existing_rows:
                if row["status"] != "active":
                    continue
                if row["topic"] == fact.topic and row["content"].strip() != fact.content.strip():
                    conflict_old = row
                    break

        memory_id = new_id("mem")
        density, distinctiveness = self._distinctiveness(vector, existing_rows)
        # 独特性加成：独特/重要/锚点 → 半衰期更长（避免「三座城市」被误删）
        half_life *= max(0.5, min(3.0, 0.8 + 0.8 * distinctiveness + 0.4 * fact.importance + 0.6 * anchor))

        title = fact.title or fact.content[:24]
        summary = fact.content
        gist = fact.gist or " ".join(list(dict.fromkeys([t for t in fact.content.split() if t]))[:5])
        memory = MemoryItem(
            id=memory_id,
            entity_id=event.entity_id,
            event_id=event.id,
            layer="L4" if pinned else "L2",
            title=title,
            summary=summary,
            gist=gist,
            content=fact.content,
            category=category,
            topic=fact.topic,
            function=fact.function,
            valence=fact.valence,
            severity=fact.severity,
            importance=fact.importance,
            base_score=base_score(fact.valence, fact.severity, self.cfg),
            half_life_days=float(half_life),
            distinctiveness=round(distinctiveness, 4),
            density=round(density, 4),
            status="active",
            pinned=pinned,
            embedding=vector,
            embedding_model=embedding_model,
            created_at=now,
            updated_at=now,
        )
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO memories(id,entity_id,event_id,layer,title,summary,gist,content,category,topic,function,
                   valence,severity,importance,base_score,half_life_days,distinctiveness,density,status,pinned,
                   embedding_json,embedding_model,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    memory.id, memory.entity_id, memory.event_id, memory.layer, memory.title,
                    memory.summary, memory.gist, memory.content, memory.category, memory.topic, memory.function,
                    memory.valence, memory.severity, memory.importance, memory.base_score,
                    memory.half_life_days, memory.distinctiveness, memory.density, memory.status,
                    memory.pinned, json.dumps(memory.embedding), memory.embedding_model,
                    memory.created_at, memory.updated_at,
                ),
            )
            if conflict_old is not None:
                conn.execute(
                    "UPDATE memories SET status='superseded', superseded_by=?, updated_at=? WHERE id=?",
                    (memory.id, now, conflict_old["id"]),
                )
            if pinned:
                self._upsert_core_fact_conn(conn, event, fact, memory.id)
            # FTS：中文先 jieba 分词，再存空格 token
            try:
                from .scoring import token_text
                conn.execute(
                    "INSERT INTO memory_fts(memory_id,tokens) VALUES(?,?)",
                    (memory.id, token_text(f"{memory.title} {memory.summary} {memory.content}")),
                )
            except Exception:
                pass
        if fact.severity >= 2 or abs(fact.valence) >= 0.6:
            self.add_impression(event, memory, fact)
            self._update_impression_strength(event.entity_id)
        return memory

    def _distinctiveness(self, vector: list[float], existing_rows: list[dict]) -> tuple[float, float]:
        if not vector:
            return 0.5, 0.5
        sims = []
        for row in existing_rows[:100]:
            try:
                other = json.loads(row.get("embedding_json") or "[]")
            except Exception:
                other = []
            if other and len(other) == len(vector):
                sims.append(cosine(vector, other))
        density = sum(sims) / len(sims) if sims else 0.0
        return density, max(0.0, min(1.0, 1.0 - density))

    def _upsert_core_fact_conn(self, conn, event: Event, fact: ExtractedFact, memory_id: str) -> None:
        key = fact.core_key
        if not key:
            # 没有显式 key 时，用 topic 兜底（如 diet:草莓）
            key = fact.topic or f"fact:{fact.content[:12]}"
        self._write_core_fact_version(
            conn,
            entity_id=event.entity_id,
            key=key,
            value=fact.content,
            source_event_id=event.id,
            pinned_by="auto",
            confidence=float(getattr(fact, "confidence", 0.8) or 0.8),
        )

    def _write_core_fact_version(
        self,
        conn,
        entity_id: str,
        key: str,
        value: str,
        source_event_id: str = "",
        pinned_by: str = "auto",
        confidence: float = 0.8,
    ) -> CoreFact:
        """版本化写核心事实：同值更新来源/置信度；异值保留旧版本并写冲突记录。"""
        now = now_iso()
        value = str(value).strip()
        key = str(key).strip()
        confidence = max(0.0, min(1.0, float(confidence)))
        old = conn.execute(
            "SELECT * FROM core_facts WHERE entity_id=? AND key=? AND status='active' "
            "ORDER BY updated_at DESC LIMIT 1",
            (entity_id, key),
        ).fetchone()
        if old is not None and str(old["value"]).strip() == value:
            conn.execute(
                "UPDATE core_facts SET source_event_id=?, confidence=MAX(confidence,?), updated_at=? WHERE id=?",
                (source_event_id, confidence, now, old["id"]),
            )
            return self._row_to_core_fact(
                dict(conn.execute("SELECT * FROM core_facts WHERE id=?", (old["id"],)).fetchone())
            )

        new_id_ = new_id("cf")
        if old is not None:
            conn.execute(
                "UPDATE core_facts SET valid_to=?, status='superseded', superseded_by=?, updated_at=? WHERE id=?",
                (now, new_id_, now, old["id"]),
            )
            conn.execute(
                """INSERT INTO core_fact_conflicts
                   (id,entity_id,key,old_id,new_id,old_value,new_value,resolution,at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    new_id("cfc"), entity_id, key, old["id"], new_id_,
                    str(old["value"]), value, "new_wins", now,
                ),
            )
        conn.execute(
            """INSERT INTO core_facts
               (id,entity_id,key,value,source_event_id,pinned_by,valid_from,valid_to,
                confidence,status,superseded_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id_, entity_id, key, value, source_event_id, pinned_by,
                now, "", confidence, "active", "", now, now,
            ),
        )
        return self._row_to_core_fact(
            dict(conn.execute("SELECT * FROM core_facts WHERE id=?", (new_id_,)).fetchone())
        )

    @staticmethod
    def _row_to_core_fact(row: dict) -> CoreFact:
        return CoreFact(
            id=row["id"], entity_id=row["entity_id"], key=row["key"], value=row["value"],
            source_event_id=row.get("source_event_id", ""), pinned_by=row.get("pinned_by", "auto"),
            valid_from=row.get("valid_from", ""), valid_to=row.get("valid_to", ""),
            confidence=float(row.get("confidence", 1.0) or 1.0), status=row.get("status", "active"),
            superseded_by=row.get("superseded_by", ""), created_at=row.get("created_at", ""),
            updated_at=row.get("updated_at", ""),
        )

    def add_impression(self, event: Event, memory: MemoryItem, fact: ExtractedFact) -> None:
        polarity = "positive" if fact.valence > 0 else "negative"
        strength = min(
            1.0,
            max(0.2, abs(base_score(fact.valence, fact.severity, self.cfg)) / 10.0),
        )
        self.db.execute(
            """INSERT INTO impressions(id,entity_id,event_id,label,polarity,strength_base,strength_current,floor,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id("imp"), event.entity_id, event.id, fact.content[:60], polarity,
                strength, strength, self.cfg.impression_floor, now_iso(), now_iso(),
            ),
        )

    def _update_impression_strength(self, entity_id: str) -> None:
        rows = self.db.fetchall("SELECT id,created_at,strength_base,floor FROM impressions WHERE entity_id=?", (entity_id,))
        now = datetime.now().astimezone()
        for row in rows:
            try:
                created = datetime.fromisoformat(row["created_at"])
                if created.tzinfo is None:
                    created = created.astimezone()
            except Exception:
                continue
            days = max(0.0, (now - created).total_seconds() / 86400)
            value = max(float(row["floor"]), float(row["strength_base"]) * retention(days, self.cfg.half_life_impression))
            self.db.execute("UPDATE impressions SET strength_current=?, updated_at=? WHERE id=?", (value, now_iso(), row["id"]))

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def row_to_memory(self, row: dict) -> MemoryItem:
        try:
            embedding = json.loads(row.get("embedding_json") or "[]")
        except Exception:
            embedding = []
        return MemoryItem(
            id=row["id"], entity_id=row["entity_id"], event_id=row.get("event_id", ""),
            layer=row["layer"], title=row["title"], summary=row["summary"], gist=row["gist"],
            content=row["content"], category=row["category"], topic=row.get("topic", ""), function=row["function"],
            valence=float(row["valence"]), severity=int(row["severity"]), importance=float(row["importance"]),
            base_score=float(row["base_score"]), half_life_days=float(row["half_life_days"]),
            distinctiveness=float(row["distinctiveness"]), density=float(row["density"]),
            status=row["status"], pinned=int(row["pinned"]), embedding=embedding,
            embedding_model=row["embedding_model"], created_at=row["created_at"], updated_at=row["updated_at"],
            last_recalled_at=row.get("last_recalled_at", ""), recall_count=int(row["recall_count"]),
            used_count=int(row["used_count"]), replay_count=int(row["replay_count"]),
            superseded_by=row.get("superseded_by", ""),
        )

    def list_memories(self, entity_id: str, status: str = "active", limit: int = 50) -> list[MemoryItem]:
        rows = self.db.fetchall(
            "SELECT * FROM memories WHERE entity_id=? AND status=? ORDER BY created_at DESC LIMIT ?",
            (entity_id, status, limit),
        )
        return [self.row_to_memory(r) for r in rows]

    def core_facts(self, entity_id: str) -> list[CoreFact]:
        rows = self.db.fetchall(
            "SELECT * FROM core_facts WHERE entity_id=? AND status='active' "
            "AND (valid_to='' OR valid_to > ?) ORDER BY updated_at DESC",
            (entity_id, now_iso()),
        )
        return [self._row_to_core_fact(r) for r in rows]

    def core_fact_history(self, entity_id: str, key: str = "") -> list[dict]:
        if key:
            return self.db.fetchall(
                "SELECT * FROM core_facts WHERE entity_id=? AND key=? ORDER BY valid_from ASC",
                (entity_id, key),
            )
        return self.db.fetchall(
            "SELECT * FROM core_facts WHERE entity_id=? ORDER BY valid_from DESC",
            (entity_id,),
        )

    def impressions(self, entity_id: str, limit: int = 20) -> list[dict]:
        return self.db.fetchall(
            "SELECT * FROM impressions WHERE entity_id=? ORDER BY strength_current DESC LIMIT ?",
            (entity_id, limit),
        )

    def stats(self, entity_id: str) -> dict:
        rows = self.db.fetchall(
            "SELECT status, COUNT(*) AS n FROM memories WHERE entity_id=? GROUP BY status", (entity_id,)
        )
        counts = {r["status"]: r["n"] for r in rows}
        return {
            "memories": sum(counts.values()),
            "active": counts.get("active", 0),
            "superseded": counts.get("superseded", 0),
            "archived": counts.get("archived", 0),
            "pinned": len(self.core_facts(entity_id)),
            "impressions": len(self.impressions(entity_id, limit=999)),
        }

    # ------------------------------------------------------------------
    # 遗忘/降级
    # ------------------------------------------------------------------
    def apply_forgetting(self, entity_id: str) -> list[dict]:
        rows = self.db.fetchall("SELECT * FROM memories WHERE entity_id=? AND status='active'", (entity_id,))
        changed: list[dict] = []
        for row in rows:
            if int(row["pinned"]):
                continue
            days = days_since(row["created_at"])
            score = retention(days, float(row["half_life_days"]))
            stage = retention_stage(score, self.cfg)
            old = row.get("retention_stage") or "full"
            if stage == old:
                continue
            new_status = row["status"]
            if stage in {"forgotten", "archive"}:
                new_status = "archived" if stage == "archive" else "forgotten"
            self.db.execute(
                "UPDATE memories SET retention_stage=?, status=?, updated_at=? WHERE id=?",
                (stage, new_status, now_iso(), row["id"]),
            )
            self.db.execute(
                "INSERT INTO forget_events(id,entity_id,memory_id,at,from_state,to_state,retention,reason) VALUES(?,?,?,?,?,?,?,?)",
                (new_id("fg"), entity_id, row["id"], now_iso(), old, stage, round(score, 4), "scheduled_decay"),
            )
            changed.append({"memory_id": row["id"], "from": old, "to": stage, "retention": round(score, 4)})
        return changed

    def write_consolidation_summary(
        self,
        entity_id: str,
        summary: str,
        source_ids: list[str],
        night_run_id: str,
        window_end: str = "",
    ) -> str:
        """写回式巩固：把夜间摘要作为“摘要记忆”写入陈述性池，不当作新事实。"""
        if not self.settings.consolidation_writeback_enabled:
            return ""
        summary = str(summary or "").strip()
        if not summary:
            return ""
        key = f"night:{night_run_id}"
        existing = self.db.fetchone(
            "SELECT id FROM memories WHERE entity_id=? AND consolidation_key=?", (entity_id, key)
        )
        if existing:
            return str(existing["id"])
        now = now_iso()
        importance = max(0.0, min(0.5, float(self.settings.consolidation_summary_importance)))
        content = f"（夜间整理摘要，不是新事实；来源 {len(source_ids)} 条）\n{summary}"
        vector: list[float] = []
        try:
            vector = self.llm.embed([content])[0]
        except Exception:
            vector = []
        model = "mock" if self.llm.is_mock else self.settings.embedding_model
        memory_id = new_id("mem")
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO memories(id,entity_id,event_id,layer,title,summary,gist,content,category,topic,function,
                   valence,severity,importance,base_score,half_life_days,distinctiveness,density,status,pinned,
                   embedding_json,embedding_model,created_at,updated_at,memory_pool,reality_status,
                   source_ids_json,consolidation_key)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    memory_id, entity_id, "", "L2",
                    f"夜间整理 · {window_end[:10]}", summary, summary[:80], content,
                    "SUMMARY", "", "social", 0.0, 1, importance,
                    base_score(0.0, 1, self.cfg), float(self.settings.consolidation_summary_half_life_days),
                    0.3, 0.3, "active", 0,
                    json.dumps(vector, ensure_ascii=False), model, now, now,
                    "declarative", "summary", json.dumps(source_ids, ensure_ascii=False), key,
                ),
            )
            try:
                from .scoring import token_text
                conn.execute(
                    "INSERT INTO memory_fts(memory_id,tokens) VALUES(?,?)",
                    (memory_id, token_text(f"{summary} {content}")),
                )
            except Exception:
                pass
        return memory_id

    def mark_used(self, memory_id: str) -> None:
        """被检索选中使用：计数 + 回忆加固（向最大半衰期渐进延长）。"""
        now = now_iso()
        row = self.db.fetchone("SELECT half_life_days, importance FROM memories WHERE id=?", (memory_id,))
        if not row:
            return
        old_h = float(row.get("half_life_days") or 30.0)
        importance = max(0.0, min(1.0, float(row.get("importance") or 0.0)))
        max_h = max(old_h, float(self.cfg.recall_half_life_max or old_h))
        gain = max(0.0, float(self.cfg.recall_half_life_gain or 0.0))
        new_h = min(max_h, old_h + (max_h - old_h) * gain * importance)
        self.db.execute(
            "UPDATE memories SET used_count=used_count+1, recall_count=recall_count+1, "
            "half_life_days=?, last_recalled_at=?, updated_at=? WHERE id=?",
            (round(new_h, 4), now, now, memory_id),
        )

    def supplement_with_fact(self, entity_id: str, key: str, value: str, pinned_by: str = "user") -> CoreFact:
        with self.db.transaction() as conn:
            fact = self._write_core_fact_version(
                conn, entity_id=entity_id, key=key, value=value,
                source_event_id="", pinned_by=pinned_by, confidence=1.0,
            )
        return fact
