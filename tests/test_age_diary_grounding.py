from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from pocketmind.config import Settings
from pocketmind.models import PersonaCard
from pocketmind.web import create_app


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(Settings(data_dir=tmp_path / "data", mock=True)))


def test_age_tracking_anniversary() -> None:
    p = PersonaCard(
        entity_id="e",
        name="小可",
        age=7,
        age_tracking=True,
        birthday="10-26",
        age_anchor_date="2026-10-26",
        age_base=7,
    )
    assert p.effective_age(date(2026, 10, 26)) == 7
    assert p.effective_age(date(2027, 10, 25)) == 7
    assert p.effective_age(date(2027, 10, 26)) == 8
    assert p.effective_age(date(2029, 10, 26)) == 10
    off = PersonaCard(entity_id="e", name="小可", age=7, age_tracking=False, birthday="10-26")
    assert off.effective_age(date(2030, 1, 1)) == 7


def test_admin_age_switch_and_birthday(tmp_path: Path) -> None:
    client = _client(tmp_path)
    r = client.put(
        "/api/admin/persona",
        json={"name": "小可", "age": 7, "age_tracking": True, "birthday": "10-26"},
    )
    assert r.status_code == 200
    p = client.get("/api/admin/persona").json()["persona"]
    assert p["age_tracking"] is True
    assert p["birthday"] == "10-26"
    assert p["age_anchor_date"]
    assert p["effective_age"] == 7
    bad = client.put("/api/admin/persona", json={"age_tracking": True, "birthday": "13-40"})
    assert bad.status_code == 400
    no_birthday = client.put("/api/admin/persona", json={"age_tracking": True, "birthday": ""})
    assert no_birthday.status_code == 400


def test_diary_is_grounded_in_real_activities(tmp_path: Path) -> None:
    client = _client(tmp_path)
    empty = client.post("/api/diary/generate", json={})
    assert empty.status_code == 200
    assert empty.json()["ok"] is False

    client.post("/api/chat", json={"text": "我今天在公园看到一只大金毛，特别开心。"})
    client.post("/api/chat", json={"text": "晚上我吃了一碗热乎乎的牛肉面。"})
    r = client.post("/api/diary/generate", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["source_count"] > 0
    assert body["activity_count"] > 0
    assert "大金毛" in body["content"] or "牛肉面" in body["content"]
    meta = client.get("/api/diary").json()["persona_meta"]
    assert meta["source_count"] > 0
    assert "source_ids" in meta


def test_admin_persona_page_has_age_controls(tmp_path: Path) -> None:
    page = _client(tmp_path).get("/admin").text
    assert "年龄追踪" in page
    assert "生日（MM-DD）" in page
    assert "真实活动来源ID" in page  # 数据表头中文化映射


def test_delete_persona_diary(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.post("/api/chat", json={"text": "今天下午我在公园看到一只大金毛。"})
    made = client.post("/api/diary/generate", json={})
    assert made.status_code == 200 and made.json()["ok"] is True
    assert client.get("/api/state").json()["persona_diary_today"]
    deleted = client.post("/api/diary/delete", json={})
    assert deleted.status_code == 200
    assert deleted.json()["ok"] is True
    assert deleted.json()["deleted"] is True
    assert not client.get("/api/state").json()["persona_diary_today"]
    assert not client.get("/api/state").json()["persona_diary_meta"]["source_count"]
