from __future__ import annotations

from pocketmind.config import ScoringConfig
from pocketmind.db import new_id, now_iso
from pocketmind.pipeline import ChatEngine
from pocketmind.scoring import half_life_for


def test_retrieval_hits_relevant_memory(engine: ChatEngine) -> None:
    engine.send("我叫小明，我住在杭州，我喜欢吃草莓。")
    result = engine.retrieval.retrieve(
        entity_id=engine.persona_card.entity_id,
        conversation_id="c",
        turn_id="t",
        query="我喜欢吃什么？",
        current_valence=0.2,
    )
    assert result.hits
    contents = [h.memory.content for h in result.hits]
    assert any("草莓" in c for c in contents)
    assert result.core_facts


def test_half_life_severe_negative_event_uses_severe_branch() -> None:
    cfg = ScoringConfig()
    h = half_life_for("EVENT", valence=-0.9, severity=3, importance=0.5, anchor=False, cfg=cfg)
    assert h > cfg.half_life_episodic
    assert h >= cfg.half_life_severe_negative


def test_recall_reinforcement_extends_half_life(engine: ChatEngine) -> None:
    entity_id = engine.persona_card.entity_id
    now = now_iso()
    mid = "mem_recall_reinforce"
    with engine.db.transaction() as conn:
        conn.execute(
            """INSERT INTO memories(id,entity_id,content,created_at,updated_at,importance,valence,half_life_days)
               VALUES(?,?,?,?,?,?,?,?)""",
            (mid, entity_id, "一条会被反复回忆的记忆", now, now, 0.8, 0.2, 30.0),
        )
    engine.memory.mark_used(mid)
    row = engine.db.fetchone("SELECT half_life_days, recall_count FROM memories WHERE id=?", (mid,))
    assert row["half_life_days"] > 30.0
    assert row["recall_count"] == 1
    # 多次回忆不应超过配置上限
    for _ in range(1000):
        engine.memory.mark_used(mid)
    row = engine.db.fetchone("SELECT half_life_days FROM memories WHERE id=?", (mid,))
    assert row["half_life_days"] <= engine.settings.scoring.recall_half_life_max + 1e-6


def test_candidate_pool_includes_old_pinned_and_recency_uses_memory_half_life(engine: ChatEngine) -> None:
    entity_id = engine.persona_card.entity_id
    old_id = "mem_old_important"
    old_at = "2020-01-01T00:00:00+08:00"
    now = now_iso()
    with engine.db.transaction() as conn:
        conn.execute(
            """INSERT INTO memories(id,entity_id,content,created_at,updated_at,importance,valence,pinned,half_life_days)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (old_id, entity_id, "长期重要记忆：关于海边的一个约定", old_at, old_at, 1.0, 0.5, 1, 3650),
        )
        for i in range(310):
            conn.execute(
                """INSERT INTO memories(id,entity_id,content,created_at,updated_at,importance,valence)
                   VALUES(?,?,?,?,?,?,?)""",
                (new_id("mem"), entity_id, f"最近的普通记忆 {i}", now, now, 0.1, 0.0),
            )

    rows = engine.retrieval._candidate_rows(entity_id)
    assert old_id in {r["id"] for r in rows}

    result = engine.retrieval.retrieve(
        entity_id=entity_id,
        conversation_id="c",
        turn_id="t-old",
        query="长期重要记忆 海边 约定",
    )
    hit = next((h for h in result.hits if h.memory.id == old_id), None)
    assert hit is not None
    # 100 天以上 + 3650 天半衰期，不应被固定 14 天尺度压到接近 0
    assert hit.recency > 0.5



def test_retrieval_log_written(engine: ChatEngine) -> None:
    engine.send("我喜欢吃草莓。")
    engine.send("你还记得我喜欢吃什么吗？")
    logs = engine.retrieval_logs()
    assert logs
    assert logs[0]["query"]
    assert logs[0]["candidates_json"] != "[]"


def test_retrieval_does_not_return_superseded(engine: ChatEngine) -> None:
    first = engine.send("我住在杭州。")
    engine.send("我住在上海。", first.conversation_id)
    result = engine.retrieval.retrieve(
        entity_id=engine.persona_card.entity_id,
        conversation_id=first.conversation_id,
        turn_id="t2",
        query="我住在哪里？",
    )
    contents = [h.memory.content for h in result.hits]
    assert any("上海" in c for c in contents)
    assert not any("杭州" in c for c in contents)
