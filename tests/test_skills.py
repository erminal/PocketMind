from __future__ import annotations

from datetime import datetime, timedelta

from pocketmind.pipeline import ChatEngine


def test_skill_create_match_and_reinforce(engine: ChatEngine) -> None:
    entity = engine.persona_card.entity_id
    skill = engine.skills.create(
        entity,
        name="周报流程",
        trigger_conditions="周报,总结",
        steps=["先列本周完成", "再列风险", "最后写下周计划"],
        constraints="不编造成果",
    )
    assert skill["name"] == "周报流程"
    assert len(skill["steps"]) == 3
    hits = engine.skills.match(entity, "帮我写这周的周报")
    assert any(h["id"] == skill["id"] for h in hits)
    before = skill["proficiency"]
    updated = engine.skills.mark_used(skill["id"], success=True)
    assert updated is not None
    assert updated["proficiency"] > before
    assert updated["success_count"] == 1


def test_skill_decay_has_floor(engine: ChatEngine) -> None:
    entity = engine.persona_card.entity_id
    skill = engine.skills.create(
        entity, "调试流程", "报错,debug", ["先看日志", "再定位"],
        half_life_days=10, floor=0.4,
    )
    engine.db.execute(
        "UPDATE skills SET proficiency=0.9, last_used_at=? WHERE id=?",
        ((datetime.now().astimezone() - timedelta(days=365)).isoformat(timespec="seconds"), skill["id"]),
    )
    engine.skills.apply_decay(entity)
    row = engine.db.fetchone("SELECT proficiency FROM skills WHERE id=?", (skill["id"],))
    assert float(row["proficiency"]) >= 0.4


def test_night_pipeline_records_run_and_skill_maintenance(engine: ChatEngine) -> None:
    entity = engine.persona_card.entity_id
    engine.skills.create(entity, "复盘流程", "复盘,总结", ["看事实", "写结论"])
    engine.send("今天发生了一件很开心的事，我考试通过了。")
    now = datetime.now().astimezone()
    engine.agent_state.ensure(entity)
    engine.db.execute(
        "UPDATE agent_state SET energy=0, sleep_state='sleeping', sleep_started_at=?, "
        "wake_at=?, last_update_at=? WHERE entity_id=?",
        (
            (now - timedelta(hours=8)).isoformat(timespec="seconds"),
            (now - timedelta(seconds=1)).isoformat(timespec="seconds"),
            (now - timedelta(hours=8)).isoformat(timespec="seconds"),
            entity,
        ),
    )
    engine.agent_state.settle(entity)
    report = engine.maybe_process_night()
    assert report is not None
    assert report["status"] in {"dreamed", "no_dream"}
    assert report["consolidated_count"] >= 1
    assert report["skill_count"] >= 1
    assert engine.agent_state.snapshot(entity)["sleep_state"] == "awake"
    row = engine.db.fetchone("SELECT * FROM night_runs WHERE id=?", (report["id"],))
    assert row is not None
    assert row["window_start"] and row["window_end"]
