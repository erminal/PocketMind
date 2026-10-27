"""M2a 夜间流水线：整理 → 技能维护 → 回验 → 做梦/直接醒。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import Settings
from .db import Database, new_id
from .dream import DreamService
from .llm import FallbackLLM
from .memory import MemoryService
from .skill import SkillService


class NightPipeline:
    def __init__(self, db: Database, settings: Settings, llm: FallbackLLM, dream: DreamService, skills: SkillService, memory: MemoryService):
        self.db = db
        self.settings = settings
        self.llm = llm
        self.dream = dream
        self.skills = skills
        self.memory = memory

    def latest(self, entity_id: str) -> dict[str, Any] | None:
        row = self.db.fetchone(
            "SELECT * FROM night_runs WHERE entity_id=? ORDER BY created_at DESC LIMIT 1",
            (entity_id,),
        )
        return dict(row) if row else None

    def run(
        self,
        entity_id: str,
        sleep_started_at: str,
        window_start: str,
        window_end: str,
        triggered_by: str = "timer",
        backfilled: bool = False,
        sleep_duration_hours: float = 0.0,
        persona_name: str = "",
    ) -> dict[str, Any]:
        """在 sleep 到点/强制唤醒后运行；窗口由调用方给，避免重复/漏整理。"""
        now = datetime.now().astimezone()
        run_id = new_id("night")
        consolidated = self.dream.consolidate(entity_id, window_start, window_end)
        memory_id = ""
        if consolidated.get("summary"):
            memory_id = self.memory.write_consolidation_summary(
                entity_id=entity_id,
                summary=str(consolidated.get("summary", "")),
                source_ids=[str(r["id"]) for r in consolidated.get("sources", [])],
                night_run_id=run_id,
                window_end=window_end,
            )
        skill_stats = self.skills.apply_decay(entity_id)
        skill_rows = self.skills.list(entity_id)
        skill_names = [str(r.get("name", "")) for r in skill_rows if r.get("name")]

        gate = self.dream.gate_sources(consolidated)
        cooldown_ok = self.dream.cooldown_ok(entity_id)
        if not cooldown_ok:
            gate = {"trigger": False, "reason": "cooldown"}
        dream_obj = None
        if gate.get("trigger"):
            dream_obj = self.dream.create_from_sources(
                entity_id=entity_id,
                sleep_started_at=sleep_started_at,
                sources=consolidated.get("sources", []),
                skill_names=skill_names,
                trigger_reason=str(gate.get("reason", "")),
                load_count=int(consolidated.get("count", 0) or 0),
                load_weight=float(consolidated.get("weight", 0) or 0),
                night_run_id=run_id,
                persona_name=persona_name,
            )
        status = "dreamed" if dream_obj else "no_dream"
        reason = str(gate.get("reason", ""))
        self.db.execute(
            """INSERT INTO night_runs(id,entity_id,sleep_started_at,sleep_duration_hours,
               triggered_by,backfilled,window_start,window_end,
               consolidated_count,consolidated_weight,important_count,skill_count,skill_maintained,
               summary,dream_id,memory_id,status,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, entity_id, sleep_started_at, float(sleep_duration_hours),
                triggered_by, 1 if backfilled else 0, window_start, window_end,
                int(consolidated.get("count", 0) or 0),
                float(consolidated.get("weight", 0) or 0),
                int(consolidated.get("important_count", 0) or 0),
                len(skill_rows), int(skill_stats.get("changed", 0) or 0),
                str(consolidated.get("summary", "")),
                str(dream_obj.get("id", "")) if dream_obj else "",
                memory_id,
                status, reason, now.isoformat(timespec="seconds"),
            ),
        )
        if dream_obj:
            self.db.execute("UPDATE dreams SET night_run_id=? WHERE id=?", (run_id, dream_obj["id"]))
        return {
            "id": run_id,
            "status": status,
            "reason": reason,
            "consolidated_count": int(consolidated.get("count", 0) or 0),
            "consolidated_weight": float(consolidated.get("weight", 0) or 0),
            "important_count": int(consolidated.get("important_count", 0) or 0),
            "summary": str(consolidated.get("summary", "")),
            "skill_count": len(skill_rows),
            "skill_maintained": int(skill_stats.get("changed", 0) or 0),
            "dream": dream_obj,
            "memory_id": memory_id,
            "window_start": window_start,
            "window_end": window_end,
            "triggered_by": triggered_by,
            "backfilled": bool(backfilled),
            "sleep_duration_hours": float(sleep_duration_hours),
        }
