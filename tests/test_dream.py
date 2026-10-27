from __future__ import annotations

import json
from datetime import datetime, timedelta

from pocketmind.pipeline import ChatEngine


def _sleep_and_process(engine: ChatEngine) -> dict:
    entity = engine.persona_card.entity_id
    engine.agent_state.ensure(entity)
    now = datetime.now().astimezone()
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
    return engine.maybe_process_night() or {}


def test_dream_and_consolidation_writeback(engine: ChatEngine) -> None:
    entity = engine.persona_card.entity_id
    engine.send("今天我在海边捡到一块很圆的石头，想带给你看。")
    before_memories = engine.db.fetchone(
        "SELECT COUNT(*) AS n FROM memories WHERE entity_id=?", (entity,)
    )["n"]
    # 测试触发流程：把阈值临时调低；生产默认要求当天内容够多/够重要
    engine.settings.dream_min_events = 1
    engine.settings.dream_min_weight = 0.0
    engine.settings.dream_min_important = 99
    report = _sleep_and_process(engine)
    assert report["status"] == "dreamed"
    dream = report["dream"]
    assert dream is not None
    assert dream["content"]
    assert dream["source_ids"]
    assert dream["trigger_reason"] in {"volume", "important", "volume+important"}
    # 整理归档存在；写回式巩固额外生成一条 SUMMARY 记忆（不是新事实）
    assert engine.db.fetchone("SELECT COUNT(*) AS n FROM nightly_summaries WHERE entity_id=?", (entity,))["n"] >= 1
    after_memories = engine.db.fetchone(
        "SELECT COUNT(*) AS n FROM memories WHERE entity_id=?", (entity,)
    )["n"]
    assert after_memories == before_memories + 1
    summary_mem = engine.db.fetchone(
        "SELECT * FROM memories WHERE entity_id=? AND category='SUMMARY' ORDER BY created_at DESC LIMIT 1",
        (entity,),
    )
    assert summary_mem is not None
    assert summary_mem["reality_status"] == "summary"
    assert summary_mem["memory_pool"] == "declarative"
    assert len(json.loads(summary_mem["source_ids_json"])) >= 1
    night = engine.db.fetchone("SELECT memory_id FROM night_runs WHERE id=?", (report["id"],))
    assert night is not None and night["memory_id"] == summary_mem["id"]
    same = engine.maybe_process_night()
    assert same is None
    latest = engine.latest_dream()
    assert latest is not None and latest["id"] == dream["id"]


def test_dream_not_triggered_without_enough_day_load(engine: ChatEngine) -> None:
    # 默认阈值 8 条/权重 5；空的一天不触发，夜间流水线仍完成（无梦）
    report = _sleep_and_process(engine)
    assert report["status"] == "no_dream"
    assert report["reason"] == "no_material"
    assert engine.latest_dream() is None


def test_dream_is_mentioned_once(engine: ChatEngine) -> None:
    engine.send("今天发生了很多事，我有点累。")
    engine.settings.dream_min_events = 1
    engine.settings.dream_min_weight = 0.0
    engine.settings.dream_min_important = 99
    report = _sleep_and_process(engine)
    dream = report["dream"]
    assert dream is not None
    engine.send("你醒了吗？")
    row = engine.db.fetchone("SELECT mentioned FROM dreams WHERE id=?", (dream["id"],))
    assert int(row["mentioned"]) == 1
