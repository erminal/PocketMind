"""M2a：程序性记忆（技能池）。

边界（用户 2026-10-26 定）：
- 只收可复用流程/方法/用户约定；“说话短一点”这类归 Persona，不进技能池；
- 技能慢衰、有 floor>0、调用加固；不进梦境、不被梦改写；
- 第一版手动创建，不自动抽取。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .config import Settings
from .db import Database, new_id, now_iso


def _parse(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.astimezone()
    except Exception:
        return None


class SkillService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    def create(
        self,
        entity_id: str,
        name: str,
        trigger_conditions: str,
        steps: list[str] | str,
        constraints: str = "",
        source: str = "manual",
        half_life_days: float | None = None,
        floor: float | None = None,
    ) -> dict[str, Any]:
        name = str(name).strip()
        triggers = str(trigger_conditions).strip()
        if not name or not triggers:
            raise ValueError("技能需要 name 和 trigger_conditions")
        if isinstance(steps, str):
            step_list = [s.strip() for s in steps.split("\n") if s.strip()]
        else:
            step_list = [str(s).strip() for s in steps if str(s).strip()]
        if not step_list:
            raise ValueError("技能至少需要一条步骤")
        now = now_iso()
        sid = new_id("skill")
        self.db.execute(
            """INSERT INTO skills(id,entity_id,name,trigger_conditions,steps_json,constraints,
               success_count,fail_count,last_used_at,proficiency,half_life_days,floor,version,
               status,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sid, entity_id, name, triggers, json.dumps(step_list, ensure_ascii=False),
                str(constraints).strip(), 0, 0, "", float(self.settings.skill_default_proficiency),
                float(half_life_days or self.settings.skill_default_half_life_days),
                float(floor if floor is not None else self.settings.skill_default_floor),
                1, "active", source, now, now,
            ),
        )
        self._event(sid, entity_id, "created", True, name)
        return self.get(sid) or {}

    def get(self, skill_id: str) -> dict[str, Any] | None:
        row = self.db.fetchone("SELECT * FROM skills WHERE id=?", (skill_id,))
        return self._to_dict(row) if row else None

    def list(self, entity_id: str, status: str = "active") -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            "SELECT * FROM skills WHERE entity_id=? AND status=? ORDER BY updated_at DESC",
            (entity_id, status),
        )
        return [self._to_dict(r) for r in rows]

    def match(self, entity_id: str, query: str, limit: int | None = None) -> list[dict[str, Any]]:
        text = (query or "").lower()
        if not text:
            return []
        rows = self.list(entity_id)
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            triggers = [t.strip().lower() for t in str(row.get("trigger_conditions", "")).replace("，", ",").replace("、", ",").split(",") if t.strip()]
            score = sum(1.0 for t in triggers if t and t in text)
            if score <= 0:
                # 触发器没命中时，用名字做弱匹配，避免技能完全搜不到
                name = str(row.get("name", "")).lower()
                if name and name in text:
                    score = 0.6
                elif any(t in text for t in triggers if t):
                    score = 0.4
            if score > 0:
                scored.append((score, row))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[: max(1, int(limit or self.settings.skill_inject_limit))]]

    def mark_used(self, skill_id: str, success: bool = True) -> dict[str, Any] | None:
        row = self.get(skill_id)
        if not row:
            return None
        proficiency = float(row.get("proficiency", 0.6) or 0.6)
        if success:
            gain = max(0.0, min(1.0, float(self.settings.skill_proficiency_gain)))
            proficiency = min(1.0, proficiency + gain * (1.0 - proficiency))
        else:
            proficiency = max(float(row.get("floor", 0.3) or 0.3), proficiency * 0.8)
        now = now_iso()
        if success:
            self.db.execute(
                "UPDATE skills SET success_count=success_count+1, proficiency=?, last_used_at=?, updated_at=? WHERE id=?",
                (round(proficiency, 4), now, now, skill_id),
            )
        else:
            self.db.execute(
                "UPDATE skills SET fail_count=fail_count+1, proficiency=?, updated_at=? WHERE id=?",
                (round(proficiency, 4), now, skill_id),
            )
        self._event(skill_id, str(row["entity_id"]), "used", success)
        return self.get(skill_id)

    def apply_decay(self, entity_id: str) -> dict[str, Any]:
        rows = self.list(entity_id)
        now = datetime.now().astimezone()
        changed = 0
        for row in rows:
            last = _parse(str(row.get("last_used_at", ""))) or _parse(str(row.get("created_at", ""))) or now
            days = max(0.0, (now - last).total_seconds() / 86400.0)
            half = max(1.0, float(row.get("half_life_days", 1800) or 1800))
            floor = max(0.0, float(row.get("floor", 0.3) or 0.3))
            new_p = max(floor, float(row.get("proficiency", 0.6) or 0.6) * (0.5 ** (days / half)))
            if abs(new_p - float(row.get("proficiency", 0.6))) >= 1e-4:
                self.db.execute(
                    "UPDATE skills SET proficiency=?, updated_at=? WHERE id=?",
                    (round(new_p, 4), now_iso(), row["id"]),
                )
                changed += 1
        return {"count": len(rows), "changed": changed}

    def _event(self, skill_id: str, entity_id: str, event_type: str, success: bool, note: str = "") -> None:
        self.db.execute(
            "INSERT INTO skill_events(id,skill_id,entity_id,event_type,success,at,note) VALUES(?,?,?,?,?,?,?)",
            (new_id("sev"), skill_id, entity_id, event_type, 1 if success else 0, now_iso(), note[:200]),
        )

    @staticmethod
    def _to_dict(row: Any) -> dict[str, Any]:
        data = dict(row)
        try:
            data["steps"] = json.loads(data.pop("steps_json", "[]") or "[]")
        except Exception:
            data["steps"] = []
        data["proficiency"] = float(data.get("proficiency", 0.6) or 0.6)
        data["floor"] = float(data.get("floor", 0.3) or 0.3)
        return data
