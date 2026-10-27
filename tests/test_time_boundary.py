from __future__ import annotations

from datetime import datetime, timedelta

from pocketmind.config import Settings
from pocketmind.pipeline import ChatEngine


def _force_state(engine: ChatEngine, **fields) -> None:
    entity = engine.persona_card.entity_id
    engine.agent_state.ensure(entity)
    keys = ", ".join(f"{k}=?" for k in fields)
    engine.db.execute(f"UPDATE agent_state SET {keys} WHERE entity_id=?", (*fields.values(), entity))


def test_forced_wake_before_8h_processes_then_greets(engine: ChatEngine) -> None:
    entity = engine.persona_card.entity_id
    now = datetime.now().astimezone()
    start = (now - timedelta(hours=3)).isoformat(timespec="seconds")
    future = (now + timedelta(hours=5)).isoformat(timespec="seconds")
    _force_state(
        engine, energy=0.0, sleep_state="sleeping", sleep_started_at=start,
        wake_at=future, last_update_at=start,
    )
    state = engine.agent_state.force_wake(entity)
    assert state["processing"] is True
    assert 20 <= state["energy"] <= 50
    report = engine.maybe_process_night()
    assert report is not None
    assert engine.agent_state.snapshot(entity)["sleep_state"] == "awake"
    greeting = engine.latest_wake_greeting()
    assert greeting is not None
    assert greeting["turn_id"].startswith("wake_greeting_")
    row = engine.db.fetchone("SELECT metadata_json FROM messages WHERE id=?", (greeting["id"],))
    assert '"wake_greeting": true' in row["metadata_json"]


def test_startup_backfills_sleep_when_previously_awake(settings: Settings) -> None:
    engine = ChatEngine(settings)
    entity = engine.persona_card.entity_id
    now = datetime.now().astimezone()
    old = (now - timedelta(hours=2)).isoformat(timespec="seconds")
    _force_state(engine, energy=100.0, sleep_state="awake", last_update_at=old)
    engine.settings.short_sleep_wake_probability = 0.0
    engine.agent_state.reconcile_on_start(entity)
    state = engine.agent_state.snapshot(entity, settle=False)
    assert state["sleeping"] is True
    assert state["sleep_backfilled"] is True
    assert state["sleep_started_at"] == old


def test_startup_long_gap_goes_to_processing_not_awake(settings: Settings) -> None:
    engine = ChatEngine(settings)
    entity = engine.persona_card.entity_id
    now = datetime.now().astimezone()
    old = (now - timedelta(hours=9)).isoformat(timespec="seconds")
    _force_state(engine, energy=100.0, sleep_state="awake", last_update_at=old)
    engine.agent_state.reconcile_on_start(entity)
    state = engine.agent_state.snapshot(entity, settle=False)
    assert state["processing"] is True
    assert state["sleep_backfilled"] is True
    assert state["energy"] == 100.0


def _process_night(engine: ChatEngine, offset_seconds: int = 0) -> dict:
    entity = engine.persona_card.entity_id
    now = datetime.now().astimezone() - timedelta(seconds=offset_seconds)
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
    return engine.maybe_process_night() or {}


def test_next_night_window_starts_at_last_consolidation(engine: ChatEngine) -> None:
    engine.send("第一段清醒周期里发生了一件事。")
    first = _process_night(engine)
    assert first["window_start"] and first["window_end"]
    engine.send("第二段清醒周期里又发生了一件事。")
    second = _process_night(engine, offset_seconds=2)
    assert second["window_start"] == first["window_end"]


def test_send_after_wake_generates_greeting_if_missing(engine: ChatEngine) -> None:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    _force_state(
        engine, energy=80.0, sleep_state="awake", last_wake_at=now,
        sleep_duration_hours=3.0, last_update_at=now,
    )
    result = engine.send("你醒了吗？")
    assert result.wake_greeting
    assert result.wake_greeting["turn_id"].startswith("wake_greeting_")
    assert result.wake_greeting["sleep_hours"] == 3.0


def test_first_night_window_is_capped_to_one_window(engine: ChatEngine) -> None:
    now = datetime.now().astimezone()
    state = {
        "sleep_started_at": (now - timedelta(hours=8)).isoformat(timespec="seconds"),
        "wake_triggered_at": now.isoformat(timespec="seconds"),
        "last_wake_at": "",
        "last_consolidated_at": "",
    }
    start, end = engine._night_window(state)
    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)
    assert end_dt - start_dt <= timedelta(hours=float(engine.settings.dream_window_hours) + 1 / 3600)
    assert end_dt - start_dt < timedelta(hours=25)
