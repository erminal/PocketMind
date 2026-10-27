from __future__ import annotations

from fastapi.testclient import TestClient

from pocketmind.config import Settings
from pocketmind.prompt_store import get_template, validate_template
from pocketmind.web import create_app


def _client(tmp_path) -> TestClient:
    app = create_app(Settings(data_dir=tmp_path / "data", mock=True))
    return TestClient(app)


def test_default_template_and_persona_fields(tmp_path) -> None:
    client = _client(tmp_path)
    r = client.get("/api/admin/prompt")
    assert r.status_code == 200
    body = r.json()
    assert "{{name}}" in body["template"]
    assert "{{identity_note}}" in body["template"]
    assert body["placeholders"]["name"]

    # 性别/来源/来源故事现在通过模板注入
    p = client.put(
        "/api/admin/persona",
        json={
            "name": "小满",
            "age": 7,
            "gender": "女",
            "species": "海边小生物（设定）",
            "origin": "大海/海边",
            "origin_story": "我在海边长大（这是设定）",
            "values": "诚实、好奇",
            "traits": "天真、活泼",
            "boundaries": "不撒谎",
        },
    )
    assert p.status_code == 200
    prompt = client.get("/api/admin/prompt").json()["prompt"]
    assert "性别 女" in prompt
    assert "来源设定" in prompt
    assert "海边小生物" in prompt
    assert "来源故事" in prompt


def test_save_and_reset_prompt_template(tmp_path) -> None:
    client = _client(tmp_path)
    template = "你是{{name}}。\n{{identity_note}}\n我不吃辣。\n{{custom_section}}"
    saved = client.put("/api/admin/prompt", json={"template": template})
    assert saved.status_code == 200
    assert "我不吃辣" in saved.json()["prompt"]
    assert "我不吃辣" in client.get("/api/admin/prompt").json()["template"]

    reset = client.put("/api/admin/prompt", json={"reset": True})
    assert reset.status_code == 200
    assert "我不吃辣" not in reset.json()["template"]
    assert "{{name}}" in reset.json()["template"]


def test_invalid_template_rejected(tmp_path) -> None:
    client = _client(tmp_path)
    r = client.put("/api/admin/prompt", json={"template": "没有名字和身份占位符"})
    assert r.status_code == 400
    assert "必需" in r.json()["detail"]
    assert not validate_template("{{unknown}}")["ok"]


def test_persona_lists_accept_multiple_separators(tmp_path) -> None:
    client = _client(tmp_path)
    r = client.put(
        "/api/admin/persona",
        json={
            "name": "小满",
            "values": "诚实、好奇，温柔,坚定; 独立",
            "traits": "天真、活泼，小迷糊,小倔强",
            "boundaries": "不撒谎、不操控；不冒充人类",
        },
    )
    assert r.status_code == 200
    persona = client.get("/api/admin/persona").json()["persona"]
    assert len(persona["values"]) == 5
    assert "好奇" in persona["values"]
    assert len(persona["traits"]) == 4
    assert len(persona["boundaries"]) == 3


def test_template_file_is_external(tmp_path) -> None:
    client = _client(tmp_path)
    data_dir = tmp_path / "data"
    client.get("/api/admin/prompt")
    path = data_dir / "prompt_template.md"
    assert path.exists()
    assert "{{name}}" in path.read_text(encoding="utf-8")
    settings = Settings(data_dir=data_dir, mock=True)
    assert "{{identity_note}}" in get_template(settings)
