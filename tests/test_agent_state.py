from __future__ import annotations

from datetime import datetime, timedelta

from pocketmind.pipeline import ChatEngine


def _force_state(engine: ChatEngine, **fields) -> None:
    entity = engine.persona_card.entity_id
    engine.agent_state.ensure(entity)
    keys = ", ".join(f"{k}=?" for k in fields)
    engine.db.execute(f"UPDATE agent_state SET {keys} WHERE entity_id=?", (*fields.values(), entity))


def test_energy_initial_and_sleep_wake(engine: ChatEngine) -> None:
    entity = engine.persona_card.entity_id
    state = engine.agent_state.snapshot(entity)
    assert state["energy"] == 100.0
    assert state["sleeping"] is False

    now = datetime.now().astimezone()
    _force_state(engine, energy=0.0, sleep_state="awake", wake_at="", last_update_at=now.isoformat(timespec="seconds"))
    state = engine.agent_state.settle(entity)
    assert state["sleeping"] is True
    assert state["wake_at"]

    sleep_start = (now - timedelta(hours=8, minutes=1)).isoformat(timespec="seconds")
    wake = (now - timedelta(minutes=1)).isoformat(timespec="seconds")
    _force_state(
        engine, energy=0.0, sleep_state="sleeping", wake_at=wake,
        sleep_started_at=sleep_start, last_update_at=sleep_start,
    )
    state = engine.agent_state.settle(entity)
    assert state["sleeping"] is False
    assert state["processing"] is True
    engine.maybe_process_night()
    assert engine.agent_state.snapshot(entity)["sleep_state"] == "awake"


def test_sleeping_chat_queues_unread(engine: ChatEngine) -> None:
    entity = engine.persona_card.entity_id
    now = datetime.now().astimezone()
    future = (now + timedelta(hours=8)).isoformat(timespec="seconds")
    _force_state(
        engine,
        energy=0.0,
        sleep_state="sleeping",
        wake_at=future,
        sleep_started_at=now.isoformat(timespec="seconds"),
        last_update_at=now.isoformat(timespec="seconds"),
    )
    result = engine.send("你睡了吗？我有个事想跟你说。")
    assert result.sleeping is True
    assert result.unread_count == 1
    assert "睡" in result.reply
    assert engine.agent_state.unread_count(entity) == 1


def test_wake_batch_reply(engine: ChatEngine) -> None:
    entity = engine.persona_card.entity_id
    now = datetime.now().astimezone()
    future = (now + timedelta(hours=8)).isoformat(timespec="seconds")
    _force_state(
        engine,
        energy=0.0,
        sleep_state="sleeping",
        wake_at=future,
        sleep_started_at=now.isoformat(timespec="seconds"),
        last_update_at=now.isoformat(timespec="seconds"),
    )
    assert engine.send("第一条：我有点想你。").sleeping is True
    assert engine.send("第二条：你醒了记得回我。").sleeping is True
    assert engine.agent_state.unread_count(entity) == 2

    past = (now - timedelta(minutes=1)).isoformat(timespec="seconds")
    sleep_start = (now - timedelta(hours=8, minutes=2)).isoformat(timespec="seconds")
    _force_state(engine, wake_at=past, sleep_started_at=sleep_start, last_update_at=sleep_start)
    engine.agent_state.settle(entity)  # sleeping -> processing
    engine.maybe_process_night()  # 整理/技能维护/回验后醒
    report = engine.wake_batch_reply()
    assert report is not None
    assert report["ok"] is True
    assert report["count"] == 2
    assert report["reply"]
    assert engine.agent_state.unread_count(entity) == 0
    latest = engine.latest_wake_reply()
    assert latest is not None and latest["turn_id"].startswith("wake_")


def test_awake_chat_consumes_energy(engine: ChatEngine) -> None:
    before = engine.agent_state.snapshot(engine.persona_card.entity_id)["energy"]
    result = engine.send("今天有件特别开心的事，我考试通过了！")
    assert result.sleeping is False
    assert result.energy <= before


def test_backfill_uses_last_update_not_last_message(engine: ChatEngine, monkeypatch) -> None:
    entity = engine.persona_card.entity_id
    now = datetime.now().astimezone()
    cid = engine.get_or_create_conversation()
    engine._save_message(cid, "user", "这是两个小时前的消息", turn_id="turn_backfill_old")
    engine.db.execute(
        "UPDATE messages SET created_at=? WHERE turn_id='turn_backfill_old'",
        ((now - timedelta(hours=2)).isoformat(timespec="seconds"),),
    )
    last_update = (now - timedelta(minutes=40)).isoformat(timespec="seconds")
    _force_state(
        engine,
        energy=60.0,
        sleep_state="awake",
        sleep_started_at="",
        wake_at="",
        wake_triggered_at="",
        last_update_at=last_update,
        sleep_backfilled=0,
        sleep_duration_hours=0.0,
    )

    # 65% 分支：继续睡；起点应该是 40 分钟前的 last_update，而不是 2 小时前的消息
    monkeypatch.setattr("pocketmind.agent_state.random.random", lambda: 0.99)
    state = engine.agent_state.reconcile_on_start(entity)
    assert state["sleeping"] is True
    assert state["sleep_backfilled"] is True
    started = datetime.fromisoformat(state["sleep_started_at"])
    assert abs((started - (now - timedelta(minutes=40))).total_seconds()) < 3
    assert state["energy"] > 60.0
    assert 0.5 < state["sleep_duration_hours"] < 0.8

    # 35% 分支：短睡被叫醒 -> processing；仍以 last_update 为睡眠起点
    _force_state(
        engine,
        energy=60.0,
        sleep_state="awake",
        sleep_started_at="",
        wake_at="",
        wake_triggered_at="",
        last_update_at=last_update,
        sleep_backfilled=0,
        sleep_duration_hours=0.0,
    )
    monkeypatch.setattr("pocketmind.agent_state.random.random", lambda: 0.01)
    state = engine.agent_state.reconcile_on_start(entity)
    assert state["processing"] is True
    assert state["sleep_backfilled"] is True
    started = datetime.fromisoformat(state["sleep_started_at"])
    assert abs((started - (now - timedelta(minutes=40))).total_seconds()) < 3
