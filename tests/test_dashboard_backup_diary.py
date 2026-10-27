from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from pocketmind.config import Settings
from pocketmind.web import create_app


def _client(tmp_path: Path) -> TestClient:
    app = create_app(Settings(data_dir=tmp_path / "data", mock=True))
    return TestClient(app)


def test_dashboard_relationship_and_transient(tmp_path: Path) -> None:
    client = _client(tmp_path)
    r = client.post("/api/chat", json={"text": "我今天很难过，作业没写完，心里堵得慌。"})
    assert r.status_code == 200
    body = r.json()
    assert body["relationship_delta"]["favorability"] < 0
    assert body["relationship_delta"]["mood"] < 0
    assert body["transient"]["fear"] < 0
    assert body["transient"]["total"] < 0

    p = client.post("/api/chat", json={"text": "我答应你，明天一定告诉你考试结果。"})
    assert p.status_code == 200
    assert p.json()["transient"]["anticipation"] > 0

    dash = client.get("/api/dashboard").json()
    assert dash["relationship"]["favorability"] < 0
    assert "recall" in dash["transient"] and "fear" in dash["transient"]
    page = client.get("/")
    assert "整体好感度" in page.text
    assert "临时状态" in page.text


def test_persona_diary_is_separate(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.post("/api/chat", json={"text": "我今天在公园看到一只大金毛，特别开心。"})
    r = client.post("/api/diary/generate", json={})
    assert r.status_code == 200
    payload = r.json()
    assert payload["ok"] is True
    assert payload["created"] is True
    assert "她的日记" in payload["content"] or "日记" in payload["content"]

    state = client.get("/api/state").json()
    assert state["persona_diary_today"]
    assert "工作日志" in state["diary_today"]
    assert "日记" in state["persona_diary_today"]
    assert (tmp_path / "data" / "diary" / "persona").exists()
    assert (tmp_path / "data" / "logs" / "daily_report").exists()


def test_export_and_import_roundtrip(tmp_path: Path) -> None:
    client = _client(tmp_path)
    first = client.post("/api/chat", json={"text": "我叫小明，我喜欢吃草莓。"})
    assert first.status_code == 200
    exported = client.post("/api/export", json={})
    assert exported.status_code == 200
    name = exported.json()["name"]
    export_path = Path(exported.json()["path"])
    assert (export_path / "pocketmind.db").exists()
    assert (export_path / "export.json").exists()
    assert (export_path / "manifest.json").exists()
    assert (export_path / "logs").exists() or (export_path / "diary").exists()

    before = client.get("/api/state").json()["stats"]["messages"]
    imported = client.post("/api/import", json={"name": name})
    assert imported.status_code == 200
    assert imported.json()["ok"] is True
    after = client.get("/api/state").json()["stats"]["messages"]
    assert after == before


def test_data_rows_are_summarized(tmp_path: Path) -> None:
    client = _client(tmp_path)
    client.post("/api/chat", json={"text": "我叫小明，我喜欢吃草莓。"})
    r = client.get("/api/admin/data?kind=retrieval_logs")
    assert r.status_code == 200
    rows = r.json()["rows"]
    if rows:
        row = rows[0]
        assert "candidates_json" not in row
        assert "selected_json" not in row
        assert "candidates_count" in row
        detail = client.get(f"/api/admin/data_detail?kind=retrieval_logs&id={row['id']}")
        assert detail.status_code == 200
        assert "candidates_json" in detail.json()["row"]
    page = client.get("/admin")
    assert "data-table-wrap" in page.text
    assert "备份与导入" in page.text
