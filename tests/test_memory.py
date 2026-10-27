from __future__ import annotations

from datetime import datetime, timedelta

from pocketmind.pipeline import ChatEngine


def test_facts_are_stored_as_second_person(engine: ChatEngine) -> None:
    result = engine.send("你好呀，我叫小明，我住在杭州，我喜欢吃草莓。")
    assert result.saved_memories == 3
    contents = [m["content"] for m in engine.memories()]
    assert "你叫小明" in contents
    assert "你住在杭州" in contents
    assert "你喜欢吃草莓" in contents
    facts = {f["key"]: f["value"] for f in engine.core_facts()}
    assert facts["name"] == "你叫小明"
    assert facts["city"] == "你住在杭州"


def test_question_does_not_create_memory(engine: ChatEngine) -> None:
    first = engine.send("我叫小明，我喜欢吃草莓。")
    before = len(engine.memories())
    second = engine.send("你还记得我喜欢吃什么吗？", first.conversation_id)
    assert second.saved_memories == 0
    assert len(engine.memories()) == before
    assert "草莓" in second.reply


def test_conflict_supersedes_old(engine: ChatEngine) -> None:
    first = engine.send("我住在杭州。")
    second = engine.send("我住在上海。", first.conversation_id)
    assert second.saved_memories >= 1
    active = [m["content"] for m in engine.memories(status="active")]
    superseded = [m["content"] for m in engine.memories(status="superseded")]
    assert "你住在上海" in active
    assert "你住在杭州" in superseded


def test_duplicate_not_saved_twice(engine: ChatEngine) -> None:
    engine.send("我叫小明。")
    count = len(engine.memories())
    engine.send("我叫小明。")
    assert len(engine.memories()) == count


def test_forgetting_degrades_old_memory(engine: ChatEngine) -> None:
    engine.send("今天天气不错。")
    memories = engine.memories()
    assert memories
    memory_id = memories[0]["id"]
    old = (datetime.now().astimezone() - timedelta(days=1000)).isoformat(timespec="seconds")
    engine.db.execute(
        "UPDATE memories SET created_at=?, half_life_days=1 WHERE id=?",
        (old, memory_id),
    )
    changed = engine.memory.apply_forgetting(engine.persona_card.entity_id)
    assert changed
    row = engine.db.fetchone("SELECT status, retention_stage FROM memories WHERE id=?", (memory_id,))
    assert row is not None
    assert row["status"] in {"archived", "forgotten"}
    assert row["retention_stage"] in {"archive", "forgotten"}
    logs = engine.db.fetchall("SELECT * FROM forget_events WHERE memory_id=?", (memory_id,))
    assert logs
