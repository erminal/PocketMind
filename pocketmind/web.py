"""本地 Web 实验台（FastAPI）。

- 单页聊天 + 记忆/核心事实/日记/统计；
- `/review` 人工标注；`/admin` 管理台（API/人格/Prompt/参数/数据）；
- 所有请求走本地 ChatEngine；无外部静态资源；
- 内心话不展示，只提供 /api/inner 供开发者本地查看。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .admin import ADMIN_TEMPLATE
from .admin import router as admin_router
from .backup import BackupService
from .config import PACKAGE_ROOT, Settings
from .db import today_str
from .pipeline import ChatEngine
from .visualizer import build_projection

TEMPLATE = PACKAGE_ROOT / "templates" / "index.html"
REVIEW_TEMPLATE = PACKAGE_ROOT / "templates" / "review.html"
SPACE_TEMPLATE = PACKAGE_ROOT / "templates" / "space.html"


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None


class AnnotationIn(BaseModel):
    conversation_id: str
    turn_id: str
    scores: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""
    annotator: str = "user"


def _public_memory(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": m["id"],
        "content": m["content"],
        "category": m["category"],
        "topic": m.get("topic", ""),
        "importance": round(float(m["importance"]), 3),
        "valence": round(float(m["valence"]), 3),
        "half_life_days": round(float(m["half_life_days"]), 1),
        "distinctiveness": round(float(m["distinctiveness"]), 3),
        "pinned": bool(m["pinned"]),
        "created_at": m["created_at"],
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="PocketMind M1", version="0.2.0")
    app.state.engine = ChatEngine(settings)
    app.state.settings = settings
    app.state.last_activity = time.monotonic()
    app.state.diary_lock = threading.Lock()
    app.state.wake_lock = threading.Lock()
    app.state.night_lock = threading.Lock()
    app.include_router(admin_router)

    def _start_diary_scheduler() -> None:
        if not settings.diary_scheduler:
            return

        def loop() -> None:
            interval = 60
            while True:
                time.sleep(interval)
                try:
                    e = app.state.engine
                    # C1：23:00 后每天尝试一次；当天无真实活动时服务会拒绝生成。
                    # 不再依赖“空闲 30 分钟”，也不在白天提前写。
                    if datetime.now().hour < e.settings.diary_trigger_hour:
                        continue
                    if e.persona_diary.exists(today_str()):
                        continue
                    with app.state.diary_lock:
                        if e.persona_diary.exists(today_str()):
                            continue
                        e.generate_persona_diary()
                except Exception:
                    continue

        threading.Thread(target=loop, daemon=True, name="pocketmind-diary").start()

    _start_diary_scheduler()

    def engine() -> ChatEngine:
        return app.state.engine

    def latest_conversation() -> str:
        row = engine().db.fetchone(
            "SELECT id FROM conversations WHERE entity_id=? ORDER BY updated_at DESC LIMIT 1",
            (engine().persona_card.entity_id,),
        )
        return row["id"] if row else engine().new_conversation("默认会话")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return TEMPLATE.read_text(encoding="utf-8")

    @app.get("/admin", response_class=HTMLResponse)
    def admin() -> str:
        return ADMIN_TEMPLATE.read_text(encoding="utf-8")

    @app.get("/health")
    def health() -> dict[str, Any]:
        e = engine()
        return {"ok": True, "mock": e.llm.is_mock, "entity_id": e.persona_card.entity_id}

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        e = engine()
        cid = latest_conversation()
        history = e.history(cid, 50)
        return {
            "conversation_id": cid,
            "entity_id": e.persona_card.entity_id,
            "persona": {
                "name": e.persona_card.name,
                "age": e.persona_card.age,
                "origin": e.persona_card.origin,
                "brief": e.persona_card.brief(),
            },
            "mock": e.llm.is_mock,
            "memories": [_public_memory(m) for m in e.memories()],
            "core_facts": e.core_facts(),
            "diary_today": e.diary_today(),
            "persona_diary_today": e.persona_diary_today(),
            "persona_diary_meta": e.persona_diary_meta(),
            "relationship": e.relationship(),
            "transient": e.transient_state(),
            "agent_state": e.agent_snapshot(),
            "dream_latest": e.latest_dream(),
            "night_latest": e.latest_night_run(),
            "vision_latest": e.latest_vision_observation(),
            "wake_greeting": e.latest_wake_greeting(),
            "wake_reply": e.latest_wake_reply(),
            "history": history,
            "stats": e.stats(),
        }

    @app.post("/api/wake")
    def force_wake() -> dict[str, Any]:
        """强制叫醒：不足 8h 也先跑整理流程，完成后才醒。"""
        e = engine()
        with app.state.night_lock:
            return e.force_wake()

    @app.get("/api/dashboard")
    def dashboard() -> dict[str, Any]:
        e = engine()
        # M2a：到点后先整理 → 技能维护 → 回验 → 梦/直接醒；完成后再处理未读批量回复。
        with app.state.night_lock:
            e.maybe_process_night()
        with app.state.wake_lock:
            e.wake_batch_reply()
        return {
            "relationship": e.relationship(),
            "transient": e.transient_state(),
            "agent_state": e.agent_snapshot(),
            "dream_latest": e.latest_dream(),
            "night_latest": e.latest_night_run(),
            "wake_greeting": e.latest_wake_greeting(),
            "wake_reply": e.latest_wake_reply(),
            "stats": e.stats(),
        }

    @app.post("/api/export")
    def export_data() -> dict[str, Any]:
        e = engine()
        return BackupService(e.settings).create(prefix="backup")

    @app.get("/api/exports")
    def list_exports() -> dict[str, Any]:
        e = engine()
        return {"exports": BackupService(e.settings).list()}

    @app.post("/api/import")
    def import_data(body: dict[str, Any]) -> dict[str, Any]:
        e = engine()
        result = BackupService(e.settings).restore(str(body.get("name") or ""))
        if result.get("ok"):
            new_settings = Settings(data_dir=e.settings.data_dir)
            app.state.engine = ChatEngine(new_settings)
            app.state.settings = new_settings
        return result

    def _chat_payload(result) -> dict[str, Any]:
        return {
            "conversation_id": result.conversation_id,
            "turn_id": result.turn_id,
            "reply": result.reply,
            "used_memories": [
                {"id": m["memory_id"], "content": m["content"], "final": m["final"]}
                for m in result.used_memories
            ],
            "saved_memories": result.saved_memories,
            "mock": result.mock,
            "retrieval_log_id": result.retrieval_log_id,
            "gate": result.gate,
            "interlocutor": result.interlocutor,
            "image_caption": result.image_caption,
            "media_path": result.media_path,
            "relationship_before": result.relationship_before,
            "relationship_after": result.relationship_after,
            "relationship_delta": result.relationship_delta,
            "event_scores": result.event_scores,
            "transient": result.transient,
            "context_meta": result.context_meta,
            "energy": result.energy,
            "sleeping": result.sleeping,
            "sleep_state": result.sleep_state,
            "unread_count": result.unread_count,
            "wake_greeting": result.wake_greeting,
        }

    @app.post("/api/chat")
    def chat(body: ChatIn) -> dict[str, Any]:
        app.state.last_activity = time.monotonic()
        result = engine().send(body.text, body.conversation_id or latest_conversation())
        return _chat_payload(result)

    @app.post("/api/chat_image")
    async def chat_image(
        file: Annotated[UploadFile, File()],
        text: Annotated[str, Form()] = "",
        conversation_id: Annotated[str, Form()] = "",
    ) -> dict[str, Any]:
        data = await file.read()
        app.state.last_activity = time.monotonic()
        try:
            result = engine().send(
                text,
                conversation_id or latest_conversation(),
                image_bytes=data,
                image_filename=file.filename or "",
                image_mime=file.content_type or "",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _chat_payload(result)

    @app.post("/api/vision/frame")
    async def vision_frame(
        file: Annotated[UploadFile, File()],
        conversation_id: Annotated[str, Form()] = "",
        force: Annotated[str, Form()] = "",
    ) -> dict[str, Any]:
        data = await file.read()
        result = engine().vision_frame(
            data, file.content_type or "image/jpeg",
            conversation_id=conversation_id, force_chat=force.lower() in {"1", "true", "yes"},
        )
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error") or "视觉分析失败")
        return result

    @app.post("/api/chat_vision")
    async def chat_vision(
        file: Annotated[UploadFile, File()],
        text: Annotated[str, Form()] = "",
        conversation_id: Annotated[str, Form()] = "",
    ) -> dict[str, Any]:
        data = await file.read()
        app.state.last_activity = time.monotonic()
        try:
            result = engine().send(
                text,
                conversation_id or latest_conversation(),
                live_frame=data,
                live_frame_mime=file.content_type or "image/jpeg",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _chat_payload(result)

    @app.post("/api/new_conversation")
    def new_conversation() -> dict[str, str]:
        return {"conversation_id": engine().new_conversation()}

    @app.get("/api/memories")
    def memories(status: str = "active", limit: int = 50) -> dict[str, Any]:
        e = engine()
        return {
            "memories": [_public_memory(m) for m in e.memory.list_memories(e.persona_card.entity_id, status, limit)],
            "core_facts": e.core_facts(),
        }

    @app.get("/api/diary")
    def diary() -> dict[str, Any]:
        e = engine()
        return {
            "today": e.work_log_today(),
            "persona_today": e.persona_diary_today(),
            "persona_meta": e.persona_diary_meta(),
            "days": e.diary.list_days(),
        }

    @app.post("/api/diary/generate")
    def generate_diary() -> dict[str, Any]:
        app.state.last_activity = time.monotonic()
        return engine().generate_persona_diary(force=True)

    @app.post("/api/diary/delete")
    def delete_diary(body: dict[str, Any] | None = None) -> dict[str, Any]:
        body = body or {}
        return engine().delete_persona_diary(body.get("date"))

    @app.get("/api/logs")
    def logs(limit: int = 20) -> dict[str, Any]:
        return {"logs": engine().retrieval_logs(limit)}

    @app.get("/api/inner")
    def inner(limit: int = 20) -> dict[str, Any]:
        """内心话：仅开发者本地查看，不进入界面/检索。"""
        return {"thoughts": engine().inner_thoughts(limit)}

    @app.post("/api/annotations")
    def annotate(body: AnnotationIn) -> dict[str, str]:
        aid = engine().annotate(body.conversation_id, body.turn_id, body.scores, body.notes, body.annotator)
        return {"id": aid}

    @app.get("/api/turns")
    def turns(limit: int = 30) -> dict[str, Any]:
        return {"turns": engine().recent_turns(limit)}

    @app.get("/api/annotations")
    def annotations(limit: int = 50) -> dict[str, Any]:
        return {"annotations": engine().annotations(limit)}

    @app.get("/review", response_class=HTMLResponse)
    def review() -> str:
        return REVIEW_TEMPLATE.read_text(encoding="utf-8")

    @app.get("/space", response_class=HTMLResponse)
    def space() -> str:
        return SPACE_TEMPLATE.read_text(encoding="utf-8")

    @app.get("/api/viz")
    def viz(status: str = "active", limit: int = 500) -> dict[str, Any]:
        e = engine()
        return build_projection(e.db, e.persona_card.entity_id, status=status, limit=limit)

    return app


app = create_app()
