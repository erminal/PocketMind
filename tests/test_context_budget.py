from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from pocketmind.config import ScoringConfig, Settings
from pocketmind.db import new_id
from pocketmind.models import PersonaCard, RelationshipState
from pocketmind.pipeline import ChatEngine
from pocketmind.prompts import build_system_prompt, trim_recent_messages
from pocketmind.web import create_app


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(Settings(data_dir=tmp_path / "data", mock=True)))


def test_prompt_truncation_preserves_safety_tail() -> None:
    persona = PersonaCard(
        entity_id="e",
        name="小可",
        age=7,
        custom_instructions="很长的补充指令。" * 200,
    )
    rel = RelationshipState(entity_id="e", updated_at="2026-10-26T00:00:00+08:00")
    cfg = replace(
        ScoringConfig(),
        context_budget_chars=320,
        context_safety_tail_chars=140,
        recent_context_budget_chars=300,
    )
    prompt, meta = build_system_prompt(persona, rel, [], [], cfg, return_meta=True)
    assert isinstance(prompt, str)
    assert len(prompt) <= 320
    assert meta["system_truncated"] is True
    # 头部身份 + 尾部安全规则都要保留
    assert "小可" in prompt
    assert "身份基线" in prompt
    assert "排他" in prompt or "自称真人" in prompt
    assert "中间内容因上下文预算省略" in prompt


def test_recent_messages_budget_keeps_latest() -> None:
    messages = [
        {"role": "user", "content": "旧消息" * 100},
        {"role": "assistant", "content": "旧回复" * 100},
        {"role": "user", "content": "最新问题，必须保留"},
    ]
    trimmed, meta = trim_recent_messages(messages, 80)
    assert meta["recent_trimmed"] is True
    assert len("".join(m["content"] for m in trimmed)) <= 80
    assert trimmed[-1]["content"].startswith("最新问题")
    assert all(m["role"] in {"user", "assistant"} for m in trimmed)


def test_chat_response_has_context_meta(tmp_path: Path) -> None:
    client = _client(tmp_path)
    r = client.post("/api/chat", json={"text": "你好呀，我叫小明，我喜欢吃草莓。"})
    assert r.status_code == 200
    meta = r.json()["context_meta"]
    for key in (
        "system_chars", "system_budget", "recent_chars", "total_chars", "estimated_tokens",
        "system_truncated", "recent_window_hours", "recent_message_limit",
    ):
        assert key in meta
    assert meta["system_budget"] > 0
    assert meta["total_chars"] > 0


def test_recent_window_keeps_only_last_72h(settings: Settings) -> None:
    engine = ChatEngine(settings)
    cid = engine.new_conversation()
    now = datetime.now().astimezone()
    rows = [
        ("old-user", now - timedelta(hours=100)),
        ("old-assistant", now - timedelta(hours=90)),
        ("recent-user", now - timedelta(hours=2)),
        ("recent-assistant", now - timedelta(hours=1)),
    ]
    for i, (content, ts) in enumerate(rows):
        engine.db.execute(
            "INSERT INTO messages(id,conversation_id,role,content,created_at,model,mock,metadata_json,turn_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (new_id("msg"), cid, "user" if i % 2 == 0 else "assistant", content, ts.isoformat(), "", 1, "{}", ""),
        )
    got = engine.recent_messages(cid, 200, window_hours=72)
    assert [m["content"] for m in got] == ["recent-user", "recent-assistant"]
