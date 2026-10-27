from __future__ import annotations

from pathlib import Path

from pocketmind.db import SCHEMA_VERSION
from pocketmind.pipeline import ChatEngine
from pocketmind.prompt_store import get_template, reset_template, save_template, template_meta


def test_schema_version_and_migration_log(engine: ChatEngine) -> None:
    assert engine.db.schema_version() == SCHEMA_VERSION
    rows = engine.db.fetchall("SELECT * FROM schema_migrations ORDER BY version")
    assert rows
    assert any(int(r["version"]) == SCHEMA_VERSION for r in rows)


def test_prompt_template_version_and_hash(engine: ChatEngine) -> None:
    get_template(engine.settings)
    before = template_meta(engine.settings)
    assert before["version"] >= 1
    text = get_template(engine.settings)
    saved = save_template(engine.settings, text + "\n# test marker\n")
    assert saved["ok"] is True
    after = template_meta(engine.settings)
    assert after["version"] == before["version"] + 1
    assert after["sha256"] != before["sha256"]
    reset_template(engine.settings)
    assert template_meta(engine.settings)["version"] > after["version"]


def test_reembed_updates_embedding_metadata(engine: ChatEngine) -> None:
    engine.send("我叫小明，我喜欢吃草莓。")
    report = engine.reembed_memories()
    assert report["ok"] is True
    assert report["count"] > 0
    assert report["dim"] > 0
    assert engine.db.get_meta("embedding_model") == str(engine.settings.embedding_model)
    assert int(engine.db.get_meta("embedding_dim", "0")) > 0


def test_rebuild_scores_dry_run_does_not_apply(engine: ChatEngine) -> None:
    engine.send("今天发生了很难过的事情，我哭了。")
    before = engine.db.fetchall("SELECT id,base_score FROM memories WHERE entity_id=?", (engine.persona_card.entity_id,))
    report = engine.rebuild_scores(apply=False)
    after = engine.db.fetchall("SELECT id,base_score FROM memories WHERE entity_id=?", (engine.persona_card.entity_id,))
    assert report["ok"] is True
    assert [r["base_score"] for r in before] == [r["base_score"] for r in after]


def test_backup_contains_versions(engine: ChatEngine) -> None:
    from pocketmind.backup import BackupService

    export = BackupService(engine.settings).create(prefix="test")
    path = Path(export["path"])
    assert (path / "prompt_template.meta.json").exists()
    assert (path / "manifest.json").exists()
    assert (path / "pocketmind.db").exists()
