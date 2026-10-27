"""临时情绪/奖励状态：回忆奖励、期待奖励、恐惧/落空惩罚。

这些值只影响“当前会话的语气/心境”，不直接写回长期好感基线；
但会记录在 recall_logs，并按 τ 指数衰减，仪表盘可看到回落。

注意：当前 M1 只做“事件触发 + 衰减 + 展示”的闭环；
“期待是否落空”的判定（等事件结果）还没有自动解析，先按事件生成临时值。
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from .config import Settings
from .db import Database, new_id, now_iso
from .models import Event, RetrievalHit
from .scoring import anticipation_reward, recall_reward

TAU_DEFAULT_MINUTES = 30.0


class TransientService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.cfg = settings.scoring

    def _add(self, entity_id: str, kind: str, value: float, memory_id: str = "", trigger_count: int = 0) -> None:
        if abs(value) < 0.005:
            return
        self.db.execute(
            """INSERT INTO recall_logs(id,entity_id,memory_id,at,target_type,transient_reward,trigger_count)
               VALUES(?,?,?,?,?,?,?)""",
            (new_id("tr"), entity_id, memory_id or "", now_iso(), kind, round(float(value), 4), int(trigger_count)),
        )

    def recall_for_hits(self, entity_id: str, hits: list[RetrievalHit]) -> float:
        total = 0.0
        for hit in hits:
            count = int(hit.memory.replay_count) + 1
            value = recall_reward(hit.memory.base_score, count, self.cfg)
            if abs(value) >= 0.005:
                self._add(entity_id, "recall", value, hit.memory.id, count)
                total += value
        return round(total, 4)

    def anticipation_for_event(self, entity_id: str, event: Event) -> float:
        # P 先用重要度近似；等“事件结果”判定做好后再校准
        p = max(self.cfg.anticipation_p_min, min(self.cfg.anticipation_p_max, 0.45 + 0.4 * event.importance))
        r_if = abs(event.base_score) or 5.0
        value = anticipation_reward(p, r_if, 0, self.cfg)
        if value >= 0.005:
            self._add(entity_id, "anticipation", value, event.id, 0)
        return round(value, 4)

    def fear_for_event(self, entity_id: str, event: Event) -> float:
        if event.valence > -0.3 or event.severity < 2:
            return 0.0
        value = -abs(event.base_score) * self.cfg.disappointment_penalty
        if abs(value) >= 0.005:
            self._add(entity_id, "fear", value, event.id, 0)
        return round(value, 4)

    def current(self, entity_id: str, at: datetime | None = None) -> dict[str, Any]:
        now = at or datetime.now().astimezone()
        rows = self.db.fetchall(
            "SELECT * FROM recall_logs WHERE entity_id=? ORDER BY at DESC LIMIT 200",
            (entity_id,),
        )
        tau_seconds = max(60.0, float(self.cfg.recall_tau_minutes or TAU_DEFAULT_MINUTES) * 60.0)
        groups = {"recall": 0.0, "anticipation": 0.0, "fear": 0.0, "other": 0.0}
        items = []
        for row in rows:
            try:
                when = datetime.fromisoformat(row["at"])
                if when.tzinfo is None:
                    when = when.astimezone()
                elapsed = max(0.0, (now - when).total_seconds())
            except Exception:
                continue
            decay = math.exp(-elapsed / tau_seconds)
            remaining = float(row["transient_reward"]) * decay
            if abs(remaining) < 0.001:
                continue
            kind = row.get("target_type") or "other"
            groups[kind if kind in groups else "other"] += remaining
            items.append(
                {
                    "id": row["id"],
                    "kind": kind,
                    "value": round(float(row["transient_reward"]), 4),
                    "remaining": round(remaining, 4),
                    "at": row["at"],
                    "memory_id": row.get("memory_id", ""),
                }
            )
        groups = {k: round(v, 4) for k, v in groups.items()}
        groups["total"] = round(sum(v for k, v in groups.items() if k != "total"), 4)
        return {
            "recall": groups["recall"],
            "anticipation": groups["anticipation"],
            "fear": groups["fear"],
            "other": groups["other"],
            "total": groups["total"],
            "tau_minutes": round(tau_seconds / 60.0, 1),
            "items": items[:30],
        }

    def record_event(self, entity_id: str, event: Event, category: str, hits: list[RetrievalHit]) -> dict[str, Any]:
        if hits:
            self.recall_for_hits(entity_id, hits)
        if category == "PROMISE":
            self.anticipation_for_event(entity_id, event)
        if event.valence <= -0.3 and event.severity >= 2:
            self.fear_for_event(entity_id, event)
        return self.current(entity_id)
