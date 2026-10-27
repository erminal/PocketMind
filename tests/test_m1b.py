from __future__ import annotations

from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from pocketmind.config import Settings
from pocketmind.pipeline import ChatEngine
from pocketmind.scoring import infer_interlocutor_state
from pocketmind.web import create_app


def _png(color: tuple[int, int, int] = (200, 120, 80)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (32, 32), color).save(buf, format="PNG")
    return buf.getvalue()


def test_memory_gate(engine: ChatEngine) -> None:
    assert engine.retrieval.decide_gate("你好呀")[0] == "casual"
    assert engine.retrieval.decide_gate("你还记得我喜欢吃什么吗？")[0] == "explicit"
    assert engine.retrieval.decide_gate("今天下午我去公园散步，看到一只猫")[0] == "normal"

    casual = engine.send("你好呀")
    assert casual.gate == "casual"
    explicit = engine.send("你还记得我吗？", casual.conversation_id)
    assert explicit.gate == "explicit"
    logs = engine.retrieval_logs(limit=2)
    assert any("explicit" in log.get("gate", "") for log in logs)


def test_emotion_inference_and_log(engine: ChatEngine) -> None:
    sad = infer_interlocutor_state("我有点难过，作业没写完")
    assert sad.emotion == "sad"
    assert sad.valence < 0
    assert sad.confidence > 0.4
    neutral = infer_interlocutor_state("今天天气不错")
    assert neutral.emotion == "neutral"
    assert neutral.should_ask is False

    result = engine.send("我有点难过，作业没写完")
    assert result.interlocutor["emotion"] == "sad"
    row = engine.db.fetchone("SELECT interlocutor_json FROM events ORDER BY rowid DESC LIMIT 1")
    assert row is not None and row["interlocutor_json"] != "{}"


def test_image_creates_media_memory(engine: ChatEngine) -> None:
    result = engine.send(
        "你看这个",
        image_bytes=_png(),
        image_filename="test.png",
        image_mime="image/png",
    )
    assert result.image_caption
    assert result.media_path
    memory_contents = [m["content"] for m in engine.memories()]
    assert any(content.startswith("[图片]") for content in memory_contents)
    media_rows = engine.db.fetchall("SELECT * FROM media")
    assert len(media_rows) == 1
    assert media_rows[0]["memory_id"]
    assert media_rows[0]["caption"] == result.image_caption
    assert engine.media.list_media(engine.persona_card.entity_id)


def test_image_caption_enters_respond_prompt(engine: ChatEngine, monkeypatch) -> None:
    caption = "浅灰背景上有一个红色正方形和一个蓝色圆形。"
    monkeypatch.setattr(engine.llm, "describe_image", lambda *args, **kwargs: caption)
    captured: list[list[dict]] = []
    original_chat = engine.llm.chat

    def capture_chat(messages, **kwargs):
        if kwargs.get("purpose") == "respond":
            captured.append(messages)
        return original_chat(messages, **kwargs)

    monkeypatch.setattr(engine.llm, "chat", capture_chat)
    result = engine.send(
        "你看到了什么？",
        image_bytes=_png(),
        image_filename="test.png",
        image_mime="image/png",
    )
    assert result.image_caption == caption
    assert captured
    assert any(caption in str(m.get("content", "")) for m in captured[0] if m.get("role") == "user")


def test_vision_frame_saves_text_only(engine: ChatEngine, monkeypatch) -> None:
    caption = "桌上有一个白色杯子和一盆绿植。"
    monkeypatch.setattr(engine.llm, "describe_image_bytes", lambda *args, **kwargs: caption)
    before_media = engine.db.fetchone("SELECT COUNT(*) AS n FROM media")["n"]
    before_memories = engine.db.fetchone("SELECT COUNT(*) AS n FROM memories")["n"]
    result = engine.vision_frame(_png(), "image/png")
    assert result["ok"] is True
    assert result["caption"] == caption
    assert result["comment"]
    assert engine.db.fetchone("SELECT COUNT(*) AS n FROM media")["n"] == before_media
    assert engine.db.fetchone("SELECT COUNT(*) AS n FROM memories")["n"] == before_memories
    obs = engine.db.fetchone("SELECT * FROM vision_observations ORDER BY created_at DESC LIMIT 1")
    assert obs is not None and obs["caption"] == caption
    assert engine.latest_vision_observation()["id"] == result["id"]
    assert result["synced_to_chat"] is True
    camera_msg = engine.db.fetchone(
        "SELECT * FROM messages WHERE turn_id LIKE 'camera_%' ORDER BY created_at DESC LIMIT 1"
    )
    assert camera_msg is not None and camera_msg["content"] == result["comment"]
    forced = engine.vision_frame(_png(), "image/png", force_chat=True)
    assert forced["ok"] is True
    assert forced["synced_to_chat"] is True


def test_vision_context_relevance_gate(engine: ChatEngine) -> None:
    from pocketmind.db import new_id, now_iso

    entity = engine.persona_card.entity_id
    caption = "桌上有一杯热茶和一个白色杯子。"
    engine.db.execute(
        "INSERT INTO vision_observations(id,entity_id,caption,comment,created_at) VALUES(?,?,?,?,?)",
        (new_id("vision"), entity, caption, "", now_iso()),
    )
    captured: list[list[dict]] = []
    original_chat = engine.llm.chat

    def capture_chat(messages, **kwargs):
        if kwargs.get("purpose") == "respond":
            captured.append(messages)
        return original_chat(messages, **kwargs)

    engine.llm.chat = capture_chat  # type: ignore[method-assign]
    try:
        engine.send("我今天有点累。")
        assert caption not in str(captured[-1][0].get("content", ""))
        engine.send("你能看到桌上有什么吗？")
        assert caption in str(captured[-1][0].get("content", ""))
    finally:
        engine.llm.chat = original_chat  # type: ignore[method-assign]


def test_live_camera_frame_used_in_chat_without_media(engine: ChatEngine, monkeypatch) -> None:
    caption = "桌上有一个白色杯子和一盆绿植。"
    monkeypatch.setattr(engine.llm, "describe_image_bytes", lambda *args, **kwargs: caption)
    captured: list[list[dict]] = []
    original_chat = engine.llm.chat

    def capture_chat(messages, **kwargs):
        if kwargs.get("purpose") == "respond":
            captured.append(messages)
        return original_chat(messages, **kwargs)

    engine.llm.chat = capture_chat  # type: ignore[method-assign]
    try:
        result = engine.send("你看看我的桌子", live_frame=_png(), live_frame_mime="image/png")
    finally:
        engine.llm.chat = original_chat  # type: ignore[method-assign]
    assert result.image_caption == ""
    assert engine.db.fetchone("SELECT COUNT(*) AS n FROM media")["n"] == 0
    assert captured
    assert caption in str(captured[0][-1].get("content", ""))
    obs = engine.db.fetchone("SELECT * FROM vision_observations ORDER BY created_at DESC LIMIT 1")
    assert obs is not None and obs["caption"] == caption


def test_latest_vision_observation_enters_chat_prompt(engine: ChatEngine) -> None:
    from pocketmind.db import new_id, now_iso

    entity = engine.persona_card.entity_id
    caption = "镜头里有一张木桌和一杯热茶。"
    engine.db.execute(
        "INSERT INTO vision_observations(id,entity_id,caption,comment,created_at) VALUES(?,?,?,?,?)",
        (new_id("vision"), entity, caption, "桌上有一杯热茶呢。", now_iso()),
    )
    captured: list[list[dict]] = []
    original_chat = engine.llm.chat

    def capture_chat(messages, **kwargs):
        if kwargs.get("purpose") == "respond":
            captured.append(messages)
        return original_chat(messages, **kwargs)

    engine.llm.chat = capture_chat  # type: ignore[method-assign]
    try:
        engine.send("你能看到我吗？")
    finally:
        engine.llm.chat = original_chat  # type: ignore[method-assign]
    assert captured
    system_text = str(captured[0][0].get("content", ""))
    assert caption in system_text
    assert "最近通过摄像头" in system_text


def test_web_image_and_review(tmp_path) -> None:
    app = create_app(Settings(data_dir=tmp_path / "data", mock=True))
    client = TestClient(app)

    chat = client.post(
        "/api/chat_image",
        files={"file": ("test.png", _png(), "image/png")},
        data={"text": "你看这个"},
    )
    assert chat.status_code == 200
    payload = chat.json()
    assert payload["image_caption"]
    assert payload["turn_id"]

    turns = client.get("/api/turns").json()["turns"]
    assert turns
    turn = turns[0]
    assert turn["turn_id"] == payload["turn_id"]

    review = client.get("/review")
    assert review.status_code == 200
    assert "人工标注台" in review.text

    ann = client.post(
        "/api/annotations",
        json={
            "conversation_id": turn["conversation_id"],
            "turn_id": turn["turn_id"],
            "scores": {"naturalness": 4, "memory_accuracy": 5, "age": 4, "continuity": 4, "overall": 4},
            "notes": "测试标注",
        },
    )
    assert ann.status_code == 200
    annotations = client.get("/api/annotations").json()["annotations"]
    assert any(a["notes"] == "测试标注" for a in annotations)
