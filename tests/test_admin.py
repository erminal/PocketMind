from __future__ import annotations

from fastapi.testclient import TestClient

from pocketmind.config import Settings
from pocketmind.web import create_app


def _client(tmp_path) -> TestClient:
    app = create_app(Settings(data_dir=tmp_path / "data", mock=True))
    return TestClient(app)


def test_admin_page_and_settings(tmp_path) -> None:
    client = _client(tmp_path)
    page = client.get("/admin")
    assert page.status_code == 200
    assert "管理台" in page.text

    settings = client.get("/api/admin/settings")
    assert settings.status_code == 200
    body = settings.json()
    assert body["llm"]["provider"]
    assert len(body["scoring"]) >= 40
    assert body["scoring_groups"]
    assert body["paths"]["settings"].endswith("settings.json")


def test_admin_update_scoring_and_chat(tmp_path) -> None:
    client = _client(tmp_path)
    resp = client.put(
        "/api/admin/settings",
        json={"scoring": {"w_sem": 0.6, "recall_limit": 4, "gate_casual_limit": 0, "severity_weights": {"1": 0.4, "2": 0.7, "3": 1.0}}},
    )
    assert resp.status_code == 200
    updated = resp.json()["settings"]["scoring"]
    assert updated["w_sem"] == 0.6
    assert updated["recall_limit"] == 4
    assert updated["severity_weights"]["1"] == 0.4
    # 热重载后还能聊天
    chat = client.post("/api/chat", json={"text": "你好呀"})
    assert chat.status_code == 200
    assert chat.json()["reply"]


def test_admin_persona_and_prompt(tmp_path) -> None:
    client = _client(tmp_path)
    resp = client.put(
        "/api/admin/persona",
        json={
            "name": "小满",
            "age": 8,
            "custom_instructions": "回答尽量短，不要用专业术语。",
            "values": "诚实,好奇",
            "traits": "慢热,嘴硬心软",
            "speech_style": "{}",
            "knowledge_boundary": "{}",
        },
    )
    assert resp.status_code == 200
    persona = resp.json()["persona"]
    assert persona["age"] == 8
    assert persona["version"] >= 2
    prompt = client.get("/api/admin/prompt").json()["prompt"]
    assert "不要用专业术语" in prompt


def test_admin_data_and_export(tmp_path) -> None:
    client = _client(tmp_path)
    client.post("/api/chat", json={"text": "我叫小明，我喜欢吃草莓。"})
    data = client.get("/api/admin/data?kind=memories")
    assert data.status_code == 200
    rows = data.json()["rows"]
    assert rows
    assert any("草莓" in str(row["content"]) for row in rows)

    messages = client.get("/api/admin/data?kind=messages")
    assert messages.status_code == 200
    assert messages.json()["rows"]

    csv = client.get("/api/admin/export?kind=memories")
    assert csv.status_code == 200
    assert "草莓" in csv.text


def test_admin_models_and_test_without_key(tmp_path) -> None:
    client = _client(tmp_path)
    models = client.post("/api/admin/models", json={"kind": "llm", "provider": "deepseek"})
    assert models.status_code == 200
    assert models.json()["models"]

    test = client.post("/api/admin/test_llm", json={"provider": "deepseek"})
    assert test.status_code == 200
    assert test.json()["ok"] is False
