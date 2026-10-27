from __future__ import annotations

import sqlite3
from pathlib import Path

from pocketmind.db import Database
from pocketmind.pipeline import ChatEngine


def test_core_facts_v1_migration_preserves_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "data" / "pocketmind.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE core_facts (
          id TEXT PRIMARY KEY, entity_id TEXT NOT NULL, key TEXT NOT NULL,
          value TEXT NOT NULL, source_event_id TEXT NOT NULL DEFAULT '',
          pinned_by TEXT NOT NULL DEFAULT 'auto', updated_at TEXT NOT NULL,
          UNIQUE(entity_id,key)
        )"""
    )
    conn.execute(
        "INSERT INTO core_facts(id,entity_id,key,value,updated_at) VALUES(?,?,?,?,?)",
        ("cf_old", "ent_old", "city", "杭州", "2026-10-01T00:00:00+08:00"),
    )
    conn.commit()
    conn.close()

    db = Database(db_path)
    db.init_schema()
    cols = {r[1] for r in sqlite3.connect(db_path).execute("PRAGMA table_info(core_facts)").fetchall()}
    assert {"valid_from", "valid_to", "confidence", "status", "superseded_by"} <= cols
    rows = db.fetchall("SELECT * FROM core_facts WHERE key='city'")
    assert len(rows) == 1
    assert rows[0]["value"] == "杭州"
    assert rows[0]["status"] == "active"
    legacy = db.fetchall("SELECT * FROM core_facts_legacy_v1")
    assert len(legacy) == 1


def test_core_fact_conflict_keeps_history(engine: ChatEngine) -> None:
    entity_id = engine.persona_card.entity_id
    first = engine.memory.supplement_with_fact(entity_id, "city", "杭州", pinned_by="user")
    second = engine.memory.supplement_with_fact(entity_id, "city", "上海", pinned_by="user")
    assert first.id != second.id
    current = engine.memory.core_facts(entity_id)
    assert [f.value for f in current if f.key == "city"] == ["上海"]
    history = engine.memory.core_fact_history(entity_id, "city")
    assert len(history) == 2
    old = next(r for r in history if r["id"] == first.id)
    assert old["status"] == "superseded"
    assert old["superseded_by"] == second.id
    assert old["valid_to"]
    conflicts = engine.db.fetchall(
        "SELECT * FROM core_fact_conflicts WHERE entity_id=? AND key='city'", (entity_id,)
    )
    assert len(conflicts) == 1
    assert conflicts[0]["old_value"] == "杭州"
    assert conflicts[0]["new_value"] == "上海"
