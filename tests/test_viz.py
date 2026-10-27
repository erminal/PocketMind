from __future__ import annotations

from fastapi.testclient import TestClient

from pocketmind.config import Settings
from pocketmind.web import create_app


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(Settings(data_dir=tmp_path / "data", mock=True)))


def test_visualization_endpoint(tmp_path) -> None:
    client = _client(tmp_path)
    client.post("/api/chat", json={"text": "我叫小明，我喜欢吃草莓。"})
    client.post("/api/chat", json={"text": "今天下午我在公园看到一只大金毛，特别开心。"})
    resp = client.get("/api/viz?status=active")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 2
    assert data["method"] == "pca"
    assert len(data["points"]) == data["count"]
    for point in data["points"]:
        assert -1.01 <= point["x"] <= 1.01
        assert "content" in point
        assert "valence" in point
    assert len(data["variance"]) == 3
    assert "edges" in data

    page = client.get("/space")
    assert page.status_code == 200
    assert "记忆星图" in page.text
    assert "MEMORY CONSTELLATION" in page.text
    assert "导出 PNG" in page.text
    assert "深空渲染" in page.text
    assert "落地视角" in page.text
    assert "显示主坐标轴" in page.text
    assert "PC2 上" in page.text
    assert "切到 2D 平面视角" in page.text


def test_visualization_empty(tmp_path) -> None:
    client = _client(tmp_path)
    resp = client.get("/api/viz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 0
    assert data["points"] == []
