"""C5：睡眠中的梦 + 睡前整理归档。

用户 2026-10-26 修正：
- 不是每天做梦；当天经历足够多/足够重要才触发，并受冷却限制；
- 梦和整理都只归档，**不写长期记忆、不参与检索**，避免记忆污染；
- 梦是锦上添花，用来让她能在之后亲口说“我昨晚做梦了”。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from .config import Settings
from .db import Database, new_id, now_iso
from .llm import FallbackLLM


def _parse(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.astimezone()
    except Exception:
        return None


class DreamService:
    def __init__(self, db: Database, settings: Settings, llm: FallbackLLM):
        self.db = db
        self.settings = settings
        self.llm = llm

    def latest(self, entity_id: str) -> dict[str, Any] | None:
        row = self.db.fetchone(
            "SELECT * FROM dreams WHERE entity_id=? ORDER BY created_at DESC LIMIT 1",
            (entity_id,),
        )
        return self._to_dict(row) if row else None

    def latest_unmentioned(self, entity_id: str) -> dict[str, Any] | None:
        row = self.db.fetchone(
            "SELECT * FROM dreams WHERE entity_id=? AND mentioned=0 ORDER BY created_at DESC LIMIT 1",
            (entity_id,),
        )
        return self._to_dict(row) if row else None

    def mark_mentioned(self, dream_id: str) -> None:
        if dream_id:
            self.db.execute("UPDATE dreams SET mentioned=1 WHERE id=?", (dream_id,))

    def day_load(self, entity_id: str) -> dict[str, Any]:
        now = datetime.now().astimezone()
        since = (now - timedelta(hours=max(1.0, float(self.settings.dream_window_hours)))).isoformat(timespec="seconds")
        rows = self.db.fetchall(
            "SELECT id,summary,valence,severity,importance,occurred_at FROM events "
            "WHERE entity_id=? AND occurred_at>=? ORDER BY occurred_at DESC LIMIT 80",
            (entity_id, since),
        )
        important = [
            r for r in rows
            if float(r.get("importance", 0) or 0) >= 0.8 or int(r.get("severity", 1) or 1) >= 4
        ]
        weight = sum(float(r.get("importance", 0) or 0) for r in rows)
        return {
            "count": len(rows),
            "weight": round(weight, 3),
            "important_count": len(important),
            "events": rows,
            "since": since,
        }

    def cooldown_ok(self, entity_id: str) -> bool:
        latest = self.latest(entity_id)
        if not latest:
            return True
        created = _parse(str(latest.get("created_at", "")))
        if not created:
            return True
        return datetime.now().astimezone() - created >= timedelta(
            hours=max(0.0, float(self.settings.dream_cooldown_hours))
        )

    def consolidate(self, entity_id: str, window_start: str, window_end: str) -> dict[str, Any]:
        """整理窗口内陈述性事件为归档摘要；不写长期记忆。"""
        rows = self.db.fetchall(
            "SELECT id,summary,importance,severity,valence,occurred_at FROM events "
            "WHERE entity_id=? AND occurred_at>=? AND occurred_at<=? ORDER BY occurred_at ASC",
            (entity_id, window_start, window_end),
        )
        if not rows:
            return {"count": 0, "weight": 0.0, "important_count": 0, "sources": [], "summary": ""}
        summary, model, mock = self._summary_text(rows)
        self.db.execute(
            "INSERT INTO nightly_summaries(id,entity_id,summary,source_ids_json,model,mock,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                new_id("night"), entity_id, summary,
                json.dumps([r["id"] for r in rows], ensure_ascii=False),
                model, 1 if mock else 0, now_iso(),
            ),
        )
        weight = sum(float(r.get("importance", 0) or 0) for r in rows)
        important = sum(
            1 for r in rows
            if float(r.get("importance", 0) or 0) >= 0.8 or int(r.get("severity", 1) or 1) >= 4
        )
        return {
            "count": len(rows), "weight": round(weight, 3), "important_count": important,
            "sources": rows, "summary": summary,
        }

    def gate_sources(self, consolidated: dict[str, Any]) -> dict[str, Any]:
        count = int(consolidated.get("count", 0) or 0)
        weight = float(consolidated.get("weight", 0) or 0)
        important = int(consolidated.get("important_count", 0) or 0)
        enough = count >= int(self.settings.dream_min_events) and weight >= float(self.settings.dream_min_weight)
        important_ok = important >= int(self.settings.dream_min_important)
        if not consolidated.get("summary"):
            return {"trigger": False, "reason": "no_material"}
        if not enough and not important_ok:
            return {"trigger": False, "reason": "below_threshold"}
        reason = "volume+important" if enough and important_ok else ("volume" if enough else "important")
        return {"trigger": True, "reason": reason}

    def create_from_sources(
        self,
        entity_id: str,
        sleep_started_at: str,
        sources: list[dict[str, Any]],
        skill_names: list[str] | None = None,
        trigger_reason: str = "",
        load_count: int = 0,
        load_weight: float = 0.0,
        night_run_id: str = "",
        persona_name: str = "",
    ) -> dict[str, Any] | None:
        if not sources:
            return None
        content, residue, model, mock = self._dream_text(persona_name or "她", sources, skill_names or [])
        dream_id = new_id("dream")
        self.db.execute(
            "INSERT INTO dreams(id,entity_id,sleep_started_at,content,residue,source_ids_json,model,mock,"
            "trigger_reason,load_count,load_weight,mentioned,night_run_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
            (
                dream_id, entity_id, sleep_started_at, content, residue,
                json.dumps([r["id"] for r in sources], ensure_ascii=False),
                model, 1 if mock else 0, trigger_reason, load_count, load_weight,
                night_run_id, now_iso(),
            ),
        )
        row = self.db.fetchone("SELECT * FROM dreams WHERE id=?", (dream_id,))
        return self._to_dict(row) if row else None

    def trigger_info(self, entity_id: str) -> dict[str, Any]:
        if not self.cooldown_ok(entity_id):
            return {"trigger": False, "reason": "cooldown", "load": self.day_load(entity_id)}
        load = self.day_load(entity_id)
        enough = (
            load["count"] >= int(self.settings.dream_min_events)
            and load["weight"] >= float(self.settings.dream_min_weight)
        )
        important = load["important_count"] >= int(self.settings.dream_min_important)
        if not enough and not important:
            return {
                "trigger": False,
                "reason": "below_threshold",
                "load": load,
                "need": {
                    "events": int(self.settings.dream_min_events),
                    "weight": float(self.settings.dream_min_weight),
                    "important": int(self.settings.dream_min_important),
                },
            }
        reason = "volume+important" if enough and important else ("volume" if enough else "important")
        return {"trigger": True, "reason": reason, "load": load}

    def maybe_generate(self, entity_id: str, sleep_started_at: str, persona_name: str = "") -> dict[str, Any] | None:
        if not sleep_started_at:
            return None
        existing = self.db.fetchone(
            "SELECT * FROM dreams WHERE entity_id=? AND sleep_started_at=? ORDER BY created_at DESC LIMIT 1",
            (entity_id, sleep_started_at),
        )
        if existing:
            return self._to_dict(existing)
        info = self.trigger_info(entity_id)
        if not info["trigger"]:
            return None
        load = info["load"]
        sources = load["events"]
        if not sources:
            return None
        content, residue, model, mock = self._dream_text(persona_name or "她", sources)
        dream_id = new_id("dream")
        self.db.execute(
            "INSERT INTO dreams(id,entity_id,sleep_started_at,content,residue,source_ids_json,model,mock,"
            "trigger_reason,load_count,load_weight,mentioned,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?)",
            (
                dream_id, entity_id, sleep_started_at, content, residue,
                json.dumps([r["id"] for r in sources], ensure_ascii=False),
                model, 1 if mock else 0, info["reason"], load["count"], load["weight"], now_iso(),
            ),
        )
        self._save_summary(entity_id, sources)
        row = self.db.fetchone("SELECT * FROM dreams WHERE id=?", (dream_id,))
        return self._to_dict(row) if row else None

    def _dream_text(self, name: str, sources: list[dict[str, Any]], skill_names: list[str] | None = None) -> tuple[str, str, str, bool]:
        fragments = "\n".join(f"- {str(r['summary'])[:60]}" for r in sources[:8])
        skills = "、".join((skill_names or [])[:3])
        if self.llm.is_mock:
            picks = "、".join(str(r["summary"])[:18] for r in sources[:4])
            content = (
                f"我梦见{picks}被折成一张很小很小的地图，"
                "我沿着地图走到一个没有钟表的房间，窗外是白天的光。"
                "醒来的时候，手心里还留着一点纸的温度。"
            )
            return content, "梦里有种被轻轻收好的感觉。", "mock", True
        system = (
            f"你是{name}，正在睡觉。请根据最近的经历片段做一个梦。"
            "梦是象征性的、不是现实；不要写新事实，不要声称梦真的发生过。"
            "第一人称，80–160 字，安静、具体、保留一点孩子气。"
        )
        user = f"最近的经历片段：\n{fragments}\n"
        if skills:
            user += f"\n今天练过/用过的技能（只用名字做抽象联想，不要写步骤细节）：{skills}\n"
        user += "\n请写这个梦，最后一句写醒来时的残留感。"
        try:
            resp = self.llm.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                purpose="dream", max_tokens=240, temperature=0.95,
            )
            content = (resp.text or "").strip()
            model, mock = resp.model, resp.mock
        except Exception:
            content, model, mock = "", "mock", True
        if not content:
            content = "我梦见一些白天的事变成了很轻的纸片，落在枕头旁边。"
        parts = [p.strip() for p in re.split(r"[。！？!?]", content) if p.strip()]
        residue = (parts[-1] + "。") if parts else "醒来时还记得一点梦里的温度。"
        return content, residue, model, mock

    def _summary_text(self, sources: list[dict[str, Any]]) -> tuple[str, str, bool]:
        if self.llm.is_mock:
            summary = "今天经历了：" + "；".join(str(r["summary"])[:40] for r in sources[:5]) + "。"
            return summary, "mock", True
        text = "\n".join(f"- {str(r['summary'])[:80]}" for r in sources[:12])
        prompt = (
            "把下面这一天的经历压缩成 60–120 字的中文整理，只保留事实与情绪线索，"
            "不要编造，不要写建议。\n" + text
        )
        try:
            resp = self.llm.chat(
                [{"role": "system", "content": "你是睡眠中的记忆整理器，只做忠实压缩。"},
                 {"role": "user", "content": prompt}],
                purpose="consolidate", max_tokens=160, temperature=0.3,
            )
            summary = (resp.text or "").strip()
            model, mock = resp.model, resp.mock
        except Exception:
            summary, model, mock = "", "mock", True
        if not summary:
            summary = "今天经历了：" + "；".join(str(r["summary"])[:40] for r in sources[:5]) + "。"
        return summary, model, mock

    def _save_summary(self, entity_id: str, sources: list[dict[str, Any]]) -> None:
        summary, model, mock = self._summary_text(sources)
        self.db.execute(
            "INSERT INTO nightly_summaries(id,entity_id,summary,source_ids_json,model,mock,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                new_id("night"), entity_id, summary,
                json.dumps([r["id"] for r in sources], ensure_ascii=False),
                model, 1 if mock else 0, now_iso(),
            ),
        )

    @staticmethod
    def _to_dict(row: Any) -> dict[str, Any]:
        data = dict(row)
        try:
            data["source_ids"] = json.loads(data.pop("source_ids_json", "[]") or "[]")
        except Exception:
            data["source_ids"] = []
        data["mock"] = bool(data.get("mock", 1))
        data["mentioned"] = bool(data.get("mentioned", 0))
        return data
