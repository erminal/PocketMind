from __future__ import annotations

from pathlib import Path

from pocketmind.pipeline import ChatEngine


def test_turn_creates_message_memory_diary_and_inner_thought(engine: ChatEngine) -> None:
    result = engine.send("我叫小明，我喜欢吃草莓。")
    assert result.mock is True
    assert result.reply
    assert result.conversation_id
    assert result.retrieval_log_id
    assert engine.memories()
    assert engine.inner_thoughts()
    assert result.diary_path
    assert Path(result.diary_path).exists()
    history = engine.history(result.conversation_id)
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[1]["role"] == "assistant"
    assert "根据我的记忆" not in result.reply


def test_stats_and_annotation(engine: ChatEngine) -> None:
    result = engine.send("你好呀。")
    stats = engine.stats()
    assert stats["messages"] == 2
    assert stats["conversations"] == 1
    ann = engine.annotate(
        result.conversation_id,
        result.turn_id,
        {"naturalness": 5, "memory_accuracy": 4, "age": 5},
        notes="自然，但略短",
    )
    assert ann
    row = engine.db.fetchone("SELECT * FROM annotations WHERE id=?", (ann,))
    assert row is not None
    assert row["scores_json"]


def test_two_turns_share_conversation(engine: ChatEngine) -> None:
    first = engine.send("我喜欢吃草莓。")
    second = engine.send("你还记得吗？", first.conversation_id)
    assert second.conversation_id == first.conversation_id
    assert len(engine.history(first.conversation_id)) == 4
