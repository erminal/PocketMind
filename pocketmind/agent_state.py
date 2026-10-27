"""C2/C3 + 时间边界：精力、睡眠、唤醒、关机补录、整理触发。

用户 2026-10-27 定稿：
- 睡眠开始/醒来都必须有时间戳；8h 到点不是直接醒，而是进入 waking/processing；
- 整理完成才真正 awake；强制唤醒也走同一整理流程；
- 关机再开：若上次是睡眠，按睡眠时间戳精确算；若上次是醒着，补录睡眠（取最后会话时间）并标记；
- 整理窗口按上次清醒/整理时间戳，避免重复/漏整理；
- 醒来后第一条消息由她主动发出，并带本次睡眠时长。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

from .config import Settings
from .db import Database, new_id, now_iso


def _parse(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.astimezone()
    except Exception:
        return None


class AgentStateService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings

    def ensure(self, entity_id: str) -> dict[str, Any]:
        row = self.db.fetchone("SELECT * FROM agent_state WHERE entity_id=?", (entity_id,))
        if row:
            return row
        now = now_iso()
        self.db.execute(
            """INSERT INTO agent_state(entity_id,energy,sleep_state,sleep_started_at,wake_at,
               wake_triggered_at,last_wake_at,last_consolidated_at,sleep_backfilled,
               sleep_duration_hours,last_update_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (entity_id, 100.0, "awake", "", "", "", "", "", 0, 0.0, now, now),
        )
        return self.db.fetchone("SELECT * FROM agent_state WHERE entity_id=?", (entity_id,)) or {}

    def _save(
        self, entity_id: str, energy: float, sleep_state: str, sleep_started: str,
        wake_at: str, wake_triggered: str, last_wake: str, last_consolidated: str,
        backfilled: int, duration_hours: float, last_update: str,
    ) -> None:
        self.db.execute(
            """UPDATE agent_state SET energy=?,sleep_state=?,sleep_started_at=?,wake_at=?,
               wake_triggered_at=?,last_wake_at=?,last_consolidated_at=?,sleep_backfilled=?,
               sleep_duration_hours=?,last_update_at=?,updated_at=? WHERE entity_id=?""",
            (
                round(max(0.0, min(100.0, energy)), 3), sleep_state, sleep_started, wake_at,
                wake_triggered, last_wake, last_consolidated, int(backfilled),
                round(max(0.0, duration_hours), 3), last_update, now_iso(), entity_id,
            ),
        )

    def _save_row(self, entity_id: str, row: dict[str, Any], energy: float, state: str, **kwargs: Any) -> None:
        self._save(
            entity_id,
            energy,
            state,
            str(kwargs.get("sleep_started", row.get("sleep_started_at", "")) or ""),
            str(kwargs.get("wake_at", row.get("wake_at", "")) or ""),
            str(kwargs.get("wake_triggered", row.get("wake_triggered_at", "")) or ""),
            str(kwargs.get("last_wake", row.get("last_wake_at", "")) or ""),
            str(kwargs.get("last_consolidated", row.get("last_consolidated_at", "")) or ""),
            int(kwargs.get("backfilled", row.get("sleep_backfilled", 0)) or 0),
            float(kwargs.get("duration", row.get("sleep_duration_hours", 0)) or 0),
            str(kwargs.get("last_update", now_iso()) or now_iso()),
        )

    def snapshot(self, entity_id: str, settle: bool = True) -> dict[str, Any]:
        if settle:
            return self.settle(entity_id)
        row = self.ensure(entity_id)
        return {
            "energy": round(float(row.get("energy", 100) or 0), 2),
            "sleep_state": str(row.get("sleep_state", "awake") or "awake"),
            "sleeping": str(row.get("sleep_state", "awake")) == "sleeping",
            "processing": str(row.get("sleep_state", "awake")) == "processing",
            "sleep_started_at": str(row.get("sleep_started_at", "") or ""),
            "wake_at": str(row.get("wake_at", "") or ""),
            "wake_triggered_at": str(row.get("wake_triggered_at", "") or ""),
            "last_wake_at": str(row.get("last_wake_at", "") or ""),
            "last_consolidated_at": str(row.get("last_consolidated_at", "") or ""),
            "sleep_backfilled": bool(row.get("sleep_backfilled", 0)),
            "sleep_duration_hours": round(float(row.get("sleep_duration_hours", 0) or 0), 2),
            "last_update_at": str(row.get("last_update_at", "") or ""),
            "unread_count": self.unread_count(entity_id),
        }

    def settle(self, entity_id: str, now: datetime | None = None) -> dict[str, Any]:
        if not self.settings.energy_enabled:
            row = self.ensure(entity_id)
            self._save_row(entity_id, row, 100.0, "awake", sleep_started="", wake_at="", wake_triggered="")
            return self.snapshot(entity_id, settle=False)
        row = self.ensure(entity_id)
        now = now or datetime.now().astimezone()
        state = str(row.get("sleep_state", "awake") or "awake")
        if state == "processing":
            return self.snapshot(entity_id, settle=False)
        last = _parse(str(row.get("last_update_at", ""))) or now
        if now <= last and state != "sleeping":
            return self.snapshot(entity_id, settle=False)
        energy = float(row.get("energy", 100) or 0)
        base = max(0.0, float(self.settings.energy_base_drain_per_hour))
        recover = max(0.0, float(self.settings.energy_recover_per_hour))
        sleep_hours = max(0.1, float(self.settings.energy_sleep_hours))

        if state == "sleeping":
            start = _parse(str(row.get("sleep_started_at", ""))) or last
            elapsed = max(0.0, (now - start).total_seconds() / 3600.0)
            sleep_energy = min(100.0, recover * elapsed)
            if elapsed >= sleep_hours:
                wake_dt = start + timedelta(hours=sleep_hours)
                self._save_row(
                    entity_id, row, 100.0, "processing",
                    wake_triggered=wake_dt.isoformat(timespec="seconds"),
                    duration=sleep_hours, last_update=now.isoformat(timespec="seconds"),
                )
            else:
                self._save_row(
                    entity_id, row, sleep_energy, "sleeping",
                    duration=elapsed, last_update=now.isoformat(timespec="seconds"),
                )
            return self.snapshot(entity_id, settle=False)

        # awake：正常代谢；精力归零则入睡
        hours = max(0.0, (now - last).total_seconds() / 3600.0)
        if base > 0 and hours > 0:
            energy = max(0.0, energy - base * hours)
        if energy <= 0.001:
            start = last + timedelta(hours=(float(row.get("energy", 0) or 0) / base if base > 0 else 0))
            wake = start + timedelta(hours=sleep_hours)
            self._save_row(
                entity_id, row, 0.0, "sleeping",
                sleep_started=start.isoformat(timespec="seconds"),
                wake_at=wake.isoformat(timespec="seconds"),
                wake_triggered="", duration=0.0,
                last_update=now.isoformat(timespec="seconds"),
            )
        else:
            self._save_row(
                entity_id, row, energy, "awake",
                last_update=now.isoformat(timespec="seconds"),
            )
        return self.snapshot(entity_id, settle=False)

    def reconcile_on_start(self, entity_id: str) -> dict[str, Any]:
        """应用启动时处理关机缺口：睡眠按时间戳精算；醒着则补录睡眠。

        不在这里调模型/整理，只做状态迁移；整理由 dashboard 的 night pipeline 执行。
        """
        row = self.ensure(entity_id)
        now = datetime.now().astimezone()
        state = str(row.get("sleep_state", "awake") or "awake")
        if state == "processing":
            return self.snapshot(entity_id, settle=False)
        last = _parse(str(row.get("last_update_at", ""))) or now
        gap_min = max(0.0, (now - last).total_seconds() / 60.0)
        sleep_hours = max(0.1, float(self.settings.energy_sleep_hours))
        recover = max(0.0, float(self.settings.energy_recover_per_hour))

        if state == "sleeping":
            start = _parse(str(row.get("sleep_started_at", ""))) or last
            elapsed = max(0.0, (now - start).total_seconds() / 3600.0)
            if elapsed >= sleep_hours:
                wake_dt = start + timedelta(hours=sleep_hours)
                self._save_row(
                    entity_id, row, 100.0, "processing",
                    wake_triggered=wake_dt.isoformat(timespec="seconds"),
                    duration=sleep_hours, last_update=now.isoformat(timespec="seconds"),
                )
            else:
                self._save_row(
                    entity_id, row, min(100.0, recover * elapsed), "sleeping",
                    duration=elapsed, last_update=now.isoformat(timespec="seconds"),
                )
            return self.snapshot(entity_id, settle=False)

        if state == "awake" and gap_min >= max(0, int(self.settings.sleep_backfill_min_minutes)):
            # 补录睡眠：起点取 last_update_at（最后一次状态结算/心跳），
            # 不再取最后一条消息时间。否则“应用一直开着但很久没聊天”会把
            # 睡眠起点错误地拉到很早，导致离线 40 分钟被算成睡了几小时。
            start = last
            elapsed = max(0.0, (now - start).total_seconds() / 3600.0)
            prev_energy = float(row.get("energy", 100) or 100)
            if elapsed >= sleep_hours:
                wake_dt = start + timedelta(hours=sleep_hours)
                self._save_row(
                    entity_id, row, 100.0, "processing",
                    sleep_started=start.isoformat(timespec="seconds"),
                    wake_at=wake_dt.isoformat(timespec="seconds"),
                    wake_triggered=wake_dt.isoformat(timespec="seconds"),
                    backfilled=1, duration=sleep_hours,
                    last_update=now.isoformat(timespec="seconds"),
                )
            else:
                energy = min(100.0, prev_energy + recover * elapsed)
                # 短睡眠：按概率“被叫醒”；不唤醒则继续睡到 8h
                wake_prob = max(0.0, min(1.0, float(self.settings.short_sleep_wake_probability)))
                if random.random() < wake_prob:
                    self._save_row(
                        entity_id, row, energy, "processing",
                        sleep_started=start.isoformat(timespec="seconds"),
                        wake_at=(start + timedelta(hours=sleep_hours)).isoformat(timespec="seconds"),
                        wake_triggered=now.isoformat(timespec="seconds"),
                        backfilled=1, duration=elapsed,
                        last_update=now.isoformat(timespec="seconds"),
                    )
                else:
                    self._save_row(
                        entity_id, row, energy, "sleeping",
                        sleep_started=start.isoformat(timespec="seconds"),
                        wake_at=(start + timedelta(hours=sleep_hours)).isoformat(timespec="seconds"),
                        wake_triggered="", backfilled=1, duration=elapsed,
                        last_update=now.isoformat(timespec="seconds"),
                    )
            return self.snapshot(entity_id, settle=False)

        return self.settle(entity_id, now=now)

    def force_wake(self, entity_id: str) -> dict[str, Any]:
        """用户强制叫醒：不足 8h 也进入整理；精算当前精力，不算满。"""
        row = self.ensure(entity_id)
        now = datetime.now().astimezone()
        state = str(row.get("sleep_state", "awake") or "awake")
        if state != "sleeping":
            return self.snapshot(entity_id, settle=False)
        start = _parse(str(row.get("sleep_started_at", ""))) or now
        elapsed = max(0.0, (now - start).total_seconds() / 3600.0)
        energy = min(100.0, max(0.0, float(self.settings.energy_recover_per_hour)) * elapsed)
        self._save_row(
            entity_id, row, energy, "processing",
            wake_triggered=now.isoformat(timespec="seconds"),
            duration=elapsed, last_update=now.isoformat(timespec="seconds"),
        )
        return self.snapshot(entity_id, settle=False)

    def finish_night(self, entity_id: str, energy: float | None = None, consolidated_at: str = "") -> dict[str, Any]:
        row = self.ensure(entity_id)
        now = datetime.now().astimezone()
        duration = float(row.get("sleep_duration_hours", 0) or 0)
        start = _parse(str(row.get("sleep_started_at", "")))
        if start:
            duration = max(duration, (now - start).total_seconds() / 3600.0)
        self._save_row(
            entity_id, row, 100.0 if energy is None else float(energy), "awake",
            last_wake=now.isoformat(timespec="seconds"),
            last_consolidated=consolidated_at or now.isoformat(timespec="seconds"),
            sleep_started="", wake_at="", wake_triggered="", backfilled=0,
            duration=duration, last_update=now.isoformat(timespec="seconds"),
        )
        return self.snapshot(entity_id, settle=False)

    def consume_event(self, entity_id: str, valence: float, importance: float) -> dict[str, Any]:
        """对话产生情绪消耗；不改变临时值，只改精力。"""
        if not self.settings.energy_enabled:
            return self.snapshot(entity_id)
        self.settle(entity_id)
        row = self.ensure(entity_id)
        if str(row.get("sleep_state", "awake")) in {"sleeping", "processing"}:
            return self.snapshot(entity_id, settle=False)
        energy = float(row.get("energy", 100) or 0)
        sign = float(self.settings.energy_negative_factor if valence < 0 else self.settings.energy_positive_factor)
        drain = abs(float(valence)) * max(0.0, float(self.settings.energy_emotion_scale)) * sign * max(0.2, min(1.0, float(importance)))
        energy = max(0.0, energy - drain)
        sleep_started = ""
        wake_at = ""
        state = "awake"
        if energy <= 0.001:
            start = datetime.now().astimezone()
            state = "sleeping"
            sleep_started = start.isoformat(timespec="seconds")
            wake_at = (start + timedelta(hours=max(0.1, float(self.settings.energy_sleep_hours)))).isoformat(timespec="seconds")
        self._save_row(
            entity_id, row, energy, state,
            sleep_started=sleep_started, wake_at=wake_at,
            wake_triggered="", duration=0.0,
            last_update=now_iso(),
        )
        return self.snapshot(entity_id, settle=False)

    def queue_unread(self, entity_id: str, content: str, conversation_id: str = "") -> None:
        self.db.execute(
            "INSERT INTO unread_messages(id,entity_id,content,created_at,conversation_id,answered) VALUES(?,?,?,?,?,0)",
            (new_id("unread"), entity_id, str(content)[:2000], now_iso(), conversation_id),
        )

    def fetch_unread(self, entity_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self.db.fetchall(
            "SELECT * FROM unread_messages WHERE entity_id=? AND answered=0 ORDER BY created_at ASC LIMIT ?",
            (entity_id, max(1, int(limit))),
        )

    def mark_unread_answered(self, ids: list[str]) -> None:
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self.db.execute(f"UPDATE unread_messages SET answered=1 WHERE id IN ({placeholders})", tuple(ids))

    def unread_count(self, entity_id: str) -> int:
        row = self.db.fetchone(
            "SELECT COUNT(*) AS n FROM unread_messages WHERE entity_id=? AND answered=0", (entity_id,)
        )
        return int(row["n"]) if row else 0
