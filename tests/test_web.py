from __future__ import annotations

from fastapi.testclient import TestClient

from pocketmind.config import Settings
from pocketmind.web import create_app


def test_web_chat_and_state(tmp_path) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data", mock=True))
    client = TestClient(app)

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True

    state = client.get("/api/state")
    assert state.status_code == 200
    body = state.json()
    assert body["persona"]["name"]
    assert "memories" in body

    chat = client.post("/api/chat", json={"text": "我叫小明，我喜欢吃草莓。"})
    assert chat.status_code == 200
    payload = chat.json()
    assert payload["reply"]
    assert payload["saved_memories"] >= 1
    assert payload["conversation_id"]

    state2 = client.get("/api/state").json()
    assert any("草莓" in m["content"] for m in state2["memories"])
