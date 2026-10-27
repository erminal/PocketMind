"""对话主流程：消息 → 事件 → 检索 → 人格化回复 → 记忆抽取 → 关系/日记/日志。

这是 M1 的「闭环」：
一句话进来，会同时产生：消息、事件、检索日志、回复、记忆、关系变化、
内心话（仅日志）、日记（独立文件）。所有步骤都可回放。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from .agent_state import AgentStateService
from .config import Settings
from .db import Database, new_id, now_iso
from .diary import DiaryService
from .dream import DreamService
from .llm import FallbackLLM, build_llm
from .media import ALLOWED_MIME, MAX_BYTES, MediaService
from .memory import MemoryService
from .models import PersonaCard, TurnResult
from .night import NightPipeline
from .persona import PersonaService
from .persona_diary import PersonaDiaryService
from .prompt_store import get_template
from .prompts import (
    build_inner_thought_messages,
    build_system_prompt,
    estimate_tokens,
    trim_recent_messages,
)
from .retrieval import RetrievalService
from .scoring import base_score, classify_category, infer_interlocutor_state
from .skill import SkillService
from .transient import TransientService

_VISION_CUES = (
    "看到", "看见", "看得到", "看看", "看一下", "看下", "你看", "镜头", "画面", "摄像头",
    "这里", "面前", "手里", "手上", "桌上", "周围", "房间", "背景", "我穿",
    "样子", "长什么样", "这是我", "我拿", "你能看", "你在干",
)


class ChatEngine:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.db_path)
        self.db.init_schema()
        # 首次启动也确保 Prompt 模板版本元数据存在（不改变模板内容）
        get_template(self.settings)
        self.persona = PersonaService(self.db, self.settings)
        self.llm: FallbackLLM = build_llm(self.settings, self.db)
        self.memory = MemoryService(self.db, self.settings, self.llm)
        self.retrieval = RetrievalService(self.db, self.settings, self.llm, self.memory, self.persona)
        self.diary = DiaryService(self.db, self.settings)
        self.persona_diary = PersonaDiaryService(self.db, self.settings, self.llm)
        self.media = MediaService(self.db, self.settings, self.llm)
        self.transient = TransientService(self.db, self.settings)
        self.agent_state = AgentStateService(self.db, self.settings)
        self.dream = DreamService(self.db, self.settings, self.llm)
        self.skills = SkillService(self.db, self.settings)
        self.night = NightPipeline(self.db, self.settings, self.llm, self.dream, self.skills, self.memory)
        self.persona_card: PersonaCard = self.persona.ensure_entity()
        # 启动时处理关机缺口（只迁移状态，不在这里调模型）
        self.agent_state.reconcile_on_start(self.persona_card.entity_id)
        self._record_embedding_meta()

    def _record_embedding_meta(self, dim: int | None = None) -> dict[str, Any]:
        """记录当前 embedding 模型/维度，便于换模型后判断是否需要重嵌入。"""
        model = str(self.settings.embedding_model or "mock")
        actual_dim = int(dim or self.settings.embedding_dim_hint or 0)
        old_model = self.db.get_meta("embedding_model", "")
        old_dim = self.db.get_meta("embedding_dim", "")
        changed = model != old_model or str(actual_dim) != str(old_dim)
        if model != old_model:
            self.db.set_meta("embedding_model", model)
        if not old_dim or str(actual_dim) != str(old_dim):
            self.db.set_meta("embedding_dim", str(actual_dim))
        if changed:
            self.db.set_meta("embedding_updated_at", now_iso())
        return {"model": model, "dim": actual_dim, "changed": changed}

    def reembed_memories(self, limit: int = 0, dry_run: bool = False) -> dict[str, Any]:
        """用当前 embedding 模型重算 active 记忆向量；用于换模型/维度后修复检索。"""
        entity_id = self.persona_card.entity_id
        sql = "SELECT id,content FROM memories WHERE entity_id=? AND status='active' ORDER BY created_at DESC"
        params: list[Any] = [entity_id]
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self.db.fetchall(sql, tuple(params))
        texts = [str(r["content"]) for r in rows]
        if dry_run or not texts:
            return {"ok": True, "dry_run": dry_run, "count": len(texts), "dim": 0, "model": self.settings.embedding_model}
        vectors = self.llm.embed(texts)
        now = now_iso()
        with self.db.transaction() as conn:
            for row, vec in zip(rows, vectors):
                conn.execute(
                    "UPDATE memories SET embedding_json=?, embedding_model=?, updated_at=? WHERE id=?",
                    (json.dumps(vec, ensure_ascii=False), self.settings.embedding_model, now, row["id"]),
                )
        dim = len(vectors[0]) if vectors else 0
        self._record_embedding_meta(dim=dim)
        return {"ok": True, "dry_run": False, "count": len(vectors), "dim": dim, "model": self.settings.embedding_model}

    def rebuild_scores(self, apply: bool = False) -> dict[str, Any]:
        """按当前 base_score 公式重算已有记忆的基础分；不自动执行，需显式 apply。"""
        entity_id = self.persona_card.entity_id
        rows = self.db.fetchall(
            "SELECT id,valence,severity,base_score FROM memories WHERE entity_id=? AND status='active'",
            (entity_id,),
        )
        changed = 0
        with self.db.transaction() as conn:
            for row in rows:
                new = round(base_score(float(row["valence"]), int(row["severity"]), self.settings.scoring), 4)
                if abs(new - float(row["base_score"])) >= 1e-6:
                    changed += 1
                    if apply:
                        conn.execute("UPDATE memories SET base_score=?, updated_at=? WHERE id=?", (new, now_iso(), row["id"]))
        return {"ok": True, "apply": apply, "count": len(rows), "changed": changed}

    # ------------------------------------------------------------------
    # 会话
    # ------------------------------------------------------------------
    def new_conversation(self, title: str = "") -> str:
        cid = new_id("conv")
        now = now_iso()
        self.db.execute(
            "INSERT INTO conversations(id,entity_id,title,created_at,updated_at) VALUES(?,?,?,?,?)",
            (cid, self.persona_card.entity_id, title, now, now),
        )
        return cid

    def get_or_create_conversation(self, conversation_id: str | None = None) -> str:
        if conversation_id:
            row = self.db.fetchone("SELECT id FROM conversations WHERE id=?", (conversation_id,))
            if row:
                return conversation_id
        return self.new_conversation()

    def _save_message(self, conversation_id: str, role: str, content: str, model: str = "", mock: bool = True, metadata: dict | None = None, turn_id: str = "") -> str:
        mid = new_id("msg")
        now = now_iso()
        self.db.execute(
            "INSERT INTO messages(id,conversation_id,role,content,created_at,model,mock,metadata_json,turn_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (mid, conversation_id, role, content, now, model, 1 if mock else 0, json.dumps(metadata or {}, ensure_ascii=False), turn_id),
        )
        self.db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
        if role == "user":
            conv = self.db.fetchone("SELECT title FROM conversations WHERE id=?", (conversation_id,))
            if conv and not conv["title"]:
                self.db.execute("UPDATE conversations SET title=? WHERE id=?", (content[:24], conversation_id))
        return mid

    def recent_messages(
        self,
        conversation_id: str,
        limit: int | None = None,
        window_hours: int | None = None,
    ) -> list[dict[str, str]]:
        limit = max(1, int(limit or self.settings.scoring.recent_messages))
        window = max(0, int(window_hours or 0))
        # 时间窗存在时多查一些候选，再由时间过滤；字符预算会在后面再裁。
        fetch_limit = max(limit, 500) if window else limit
        rows = self.db.fetchall(
            "SELECT role,content,created_at FROM messages WHERE conversation_id=? ORDER BY created_at DESC LIMIT ?",
            (conversation_id, fetch_limit),
        )
        cutoff = datetime.now().astimezone() - timedelta(hours=window) if window else None
        kept: list[dict[str, str]] = []
        for row in rows:
            if cutoff is not None:
                ts = self._parse_time(str(row["created_at"] or ""))
                if ts is None or ts < cutoff:
                    continue
            kept.append({"role": row["role"], "content": row["content"]})
            if len(kept) >= limit:
                break
        return list(reversed(kept))

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    def send(
        self,
        text: str,
        conversation_id: str | None = None,
        image_bytes: bytes | None = None,
        image_filename: str = "",
        image_mime: str = "",
        live_frame: bytes | None = None,
        live_frame_mime: str = "image/jpeg",
    ) -> TurnResult:
        text = (text or "").strip()
        if not text and not image_bytes and not live_frame:
            raise ValueError("消息不能为空")
        entity_id = self.persona_card.entity_id
        cid = self.get_or_create_conversation(conversation_id)
        turn_id = new_id("turn")
        state_before = self.agent_state.snapshot(entity_id)

        media: dict | None = None
        caption = ""
        camera_caption = ""
        if image_bytes:
            media = self.media.save_bytes(entity_id, cid, turn_id, image_bytes, image_filename, image_mime)
            caption = media["caption"]
            user_display = (text + "\n[图片]") if text else "（发来一张图片）"
            event_text = text or "（发来一张图片）"
        else:
            user_display = text
            event_text = text
            if live_frame and self._vision_relevant(text):
                # 本轮临时摄像头帧：只做视觉描述，不保存图片、不建媒体记忆。
                try:
                    camera_caption = self.llm.describe_image_bytes(
                        live_frame, live_frame_mime,
                        prompt="用一句自然、具体的中文描述这个摄像头画面；只描述看得到的内容，不要识别具体人物身份、不要编造故事。",
                    )
                except Exception:
                    camera_caption = ""
        self._save_message(cid, "user", user_display, turn_id=turn_id)

        event = self.memory.ingest_user_event(entity_id, cid, turn_id, event_text)
        interlocutor = infer_interlocutor_state(event_text)
        self.db.execute(
            "UPDATE events SET interlocutor_json=?, valence=? WHERE id=?",
            (json.dumps(interlocutor.to_dict(), ensure_ascii=False), interlocutor.valence, event.id),
        )
        agent_state = self.agent_state.settle(entity_id)
        if agent_state.get("sleeping") or agent_state.get("processing"):
            self.agent_state.queue_unread(entity_id, user_display, cid)
            agent_state = self.agent_state.snapshot(entity_id, settle=False)
            wake = str(agent_state.get("wake_at", ""))[:16].replace("T", " ")
            if agent_state.get("processing"):
                reply = "（她还在做睡后的整理和梦，手机亮了一下；你的消息已经放在她枕边，整理完会看。）"
            else:
                reply = f"（她还在睡着，手机亮了一下；你的消息我放在枕边了，她醒来会看。预计 {wake} 左右醒。）"
            return TurnResult(
                conversation_id=cid,
                turn_id=turn_id,
                reply=reply,
                used_memories=[],
                retrieval_log_id="",
                inner_thought="",
                diary_path="",
                interlocutor=interlocutor.to_dict(),
                gate="processing" if agent_state.get("processing") else "sleeping",
                image_caption=caption,
                media_path=media["path"] if media else "",
                context_meta={},
                energy=float(agent_state.get("energy", 0)),
                sleeping=bool(agent_state.get("sleeping")),
                sleep_state=str(agent_state.get("sleep_state", "sleeping")),
                unread_count=int(agent_state.get("unread_count", 0)),
                mock=self.llm.is_mock,
            )
        # 醒来后的第一条消息必须先由她发出：若 dashboard 还没生成，就在本轮回复前补上
        wake_greeting: dict[str, Any] = {}
        last_wake = str(agent_state.get("last_wake_at", "") or "")
        if last_wake:
            latest_greeting = self.latest_wake_greeting()
            if not latest_greeting or str(latest_greeting.get("created_at", "")) < last_wake:
                greeting = self.generate_wake_greeting(
                    duration=float(agent_state.get("sleep_duration_hours", 0) or 0),
                    backfilled=bool(agent_state.get("sleep_backfilled")),
                )
                if greeting:
                    wake_greeting = greeting
        retrieval_query = f"{event_text} {caption} {camera_caption}".strip()
        retrieval = self.retrieval.retrieve(
            entity_id=entity_id,
            conversation_id=cid,
            turn_id=turn_id,
            query=retrieval_query,
            current_valence=interlocutor.valence,
            current_function=event.function,
        )
        system_prompt, system_meta = build_system_prompt(
            self.persona_card,
            retrieval.relationship,
            retrieval.core_facts,
            retrieval.hits,
            self.settings.scoring,
            gate=retrieval.gate,
            interlocutor=interlocutor,
            settings=self.settings,
            return_meta=True,
        )
        mention_dream = self.dream.latest_unmentioned(entity_id)
        if mention_dream:
            system_prompt += (
                "\n\n【你昨晚做过的梦（只作为梦记得，不要说成真实发生的事）】\n"
                + str(mention_dream.get("content", ""))[:500]
                + "\n如果合适，可以自然提起“我昨晚做梦了”，不必每轮都提。"
            )
            system_meta["system_chars"] = len(system_prompt)
            system_meta["system_estimated_tokens"] = estimate_tokens(system_prompt)
        skill_hits = self.skills.match(entity_id, text)
        if skill_hits:
            skill_lines = []
            for s in skill_hits:
                steps = "；".join(str(x) for x in s.get("steps", [])[:5])
                skill_lines.append(f"- {s.get('name')}：{steps}（约束：{s.get('constraints') or '无'}）")
            system_prompt += "\n\n【可复用技能（怎么做，不是事实记忆）】\n" + "\n".join(skill_lines)
            system_meta["system_chars"] = len(system_prompt)
            system_meta["system_estimated_tokens"] = estimate_tokens(system_prompt)
        # 把最近一次摄像头观察接入主对话；只有用户这句话确实在问画面/场景时才注入，
        # 避免“聊别的事也强行扯回摄像头”。
        vision_obs = self.latest_vision_observation()
        if vision_obs and self._vision_relevant(text):
            obs_at = self._parse_time(str(vision_obs.get("created_at", "")))
            age_s = max(0.0, (datetime.now().astimezone() - obs_at).total_seconds()) if obs_at else 999999.0
            if age_s <= max(0, int(self.settings.vision_context_seconds)):
                when = "刚刚" if age_s <= 20 else f"{int(age_s)} 秒前"
                system_prompt += (
                    f"\n\n【你最近通过摄像头看到的画面（{when}；只是最近一次，不代表你一直盯着看）】\n"
                    + str(vision_obs.get("caption", ""))[:500]
                    + "\n只有在用户问题确实与画面/当前场景有关时才提及；如果用户在聊别的事，不要强行描述画面、不要把话题拉回摄像头。"
                    + "不要声称自己持续实时观看，不要把画面内容当成长期记忆，画面没有依据的内容不要编造。"
                )
                system_meta["system_chars"] = len(system_prompt)
                system_meta["system_estimated_tokens"] = estimate_tokens(system_prompt)
        context = {
            "memories": [
                {
                    "id": hit.memory.id,
                    "content": hit.memory.content,
                    "category": hit.memory.category,
                    "valence": hit.memory.valence,
                    "importance": hit.memory.importance,
                }
                for hit in retrieval.hits
            ],
            "core_facts": [{"key": f.key, "value": f.value} for f in retrieval.core_facts],
            "relationship": retrieval.relationship.to_dict(),
            "event": {"valence": event.valence, "severity": event.severity, "importance": event.importance},
            "interlocutor": interlocutor.to_dict(),
            "image_caption": caption if media else "",
        }
        messages = [{"role": "system", "content": system_prompt}]
        recent_raw = [
            msg for msg in self.recent_messages(
                cid,
                self.settings.scoring.recent_messages,
                self.settings.scoring.recent_window_hours,
            )
            if msg["role"] in {"user", "assistant"}
        ]
        recent_messages, recent_meta = trim_recent_messages(
            recent_raw, self.settings.scoring.recent_context_budget_chars
        )
        messages.extend(recent_messages)
        if messages[-1].get("content") != (user_display):
            messages.append({"role": "user", "content": event_text})
        # 真实视觉 caption 必须进本轮 Prompt，否则模型只能看到“图片”占位。
        # 手动图片和本轮临时摄像头帧都走这里；原始描述不上屏，只作为模型上下文。
        vision_caption = camera_caption or caption
        if vision_caption and not vision_caption.startswith(("（收到", "（mock")):
            note = f"\n[图片内容：{vision_caption}]"
            if messages[-1].get("role") == "user":
                messages[-1]["content"] = str(messages[-1].get("content", "")) + note
            else:
                messages.append({"role": "user", "content": f"[图片内容：{vision_caption}]"})
        recent_chars = sum(len(str(m.get("content", ""))) for m in messages[1:])
        context_meta = {
            **system_meta,
            **recent_meta,
            "recent_window_hours": self.settings.scoring.recent_window_hours,
            "recent_message_limit": self.settings.scoring.recent_messages,
            "recent_chars": recent_chars,
            "total_chars": system_meta["system_chars"] + recent_chars,
            "estimated_tokens": system_meta["system_estimated_tokens"] + sum(
                estimate_tokens(str(m.get("content", ""))) for m in messages[1:]
            ),
        }
        llm_resp = self.llm.chat(messages, purpose="respond", context=context, max_tokens=320, temperature=0.85)
        reply = (llm_resp.text or "").strip() or "唔……我刚刚有点走神，你再说一遍好不好？"
        if mention_dream:
            self.dream.mark_mentioned(str(mention_dream.get("id", "")))
        for skill_hit in skill_hits:
            self.skills.mark_used(str(skill_hit.get("id", "")), success=True)
        self._save_message(
            cid,
            "assistant",
            reply,
            model=llm_resp.model,
            mock=llm_resp.mock,
            metadata={
                "retrieval_log_id": retrieval.log_id,
                "tier": "T0",
                "fallback": llm_resp.fallback_reason,
                "media_path": media["path"] if media else "",
            },
            turn_id=turn_id,
        )
        self.retrieval.mark_selected_used(retrieval)

        # 关系变化（用事件分；少量事件才动信任）
        category = classify_category(text)
        event_type = "event"
        if category == "PROMISE":
            event_type = "promise_made"
        elif event.valence <= -0.3 and event.severity >= 2:
            event_type = "conflict"
        elif event.base_score > 3 and event.importance >= 0.5:
            event_type = "positive_event"
        relationship_before = retrieval.relationship
        relationship_after = self.persona.apply_event_to_relationship(
            relationship_before, event.base_score, event.importance, event_type
        )
        relationship_delta = {
            field: round(getattr(relationship_after, field) - getattr(relationship_before, field), 4)
            for field in ("favorability", "trust", "closeness", "boundary_respect", "mood")
        }
        transient = self.transient.record_event(entity_id, event, category, retrieval.hits)

        # 记忆抽取（与回复分离；失败不阻塞）
        saved: list = []
        try:
            saved = self.memory.extract_and_store(event)
        except Exception:
            saved = []
        if media:
            try:
                media_mem = self.memory.add_media_memory(event, caption, media["path"])
                if media_mem:
                    self.media.attach_memory(media["id"], media_mem.id)
                    saved.append(media_mem)
            except Exception:
                pass
        try:
            self.memory.apply_forgetting(entity_id)
        except Exception:
            pass

        # 内心话：只进本地日志，不显示、不检索
        inner = ""
        try:
            inner_resp = self.llm.chat(
                build_inner_thought_messages(self.persona_card, text),
                purpose="inner_thought",
                context=context,
                max_tokens=60,
                temperature=0.7,
            )
            inner = (inner_resp.text or "").strip()
            if inner:
                self.db.execute(
                    "INSERT INTO inner_thoughts(id,entity_id,conversation_id,turn_id,at,content,scope) VALUES(?,?,?,?,?,?,?)",
                    (new_id("think"), entity_id, cid, turn_id, now_iso(), inner, "dev"),
                )
        except Exception:
            inner = ""

        diary_path = ""
        try:
            diary_path = self.diary.append_turn(entity_id, event, reply, inner)
        except Exception:
            diary_path = ""

        agent_after = self.agent_state.consume_event(entity_id, event.valence, event.importance)
        if live_frame and camera_caption:
            self.db.execute(
                "INSERT INTO vision_observations(id,entity_id,caption,comment,created_at) VALUES(?,?,?,?,?)",
                (new_id("vision"), entity_id, camera_caption, "", now_iso()),
            )
        # 睡前日记：刚进入睡眠时尽量先写当天日记，再进入 8h 睡眠
        if agent_after.get("sleeping") and not state_before.get("sleeping") and not state_before.get("processing"):
            try:
                if not self.persona_diary.exists(self.persona_diary.today()):
                    self.generate_persona_diary()
            except Exception:
                pass
        return TurnResult(
            conversation_id=cid,
            turn_id=turn_id,
            reply=reply,
            used_memories=[h.to_log() for h in retrieval.hits],
            retrieval_log_id=retrieval.log_id,
            saved_memories=len(saved),
            inner_thought=inner,
            diary_path=diary_path,
            interlocutor=interlocutor.to_dict(),
            gate=retrieval.gate,
            image_caption=caption,
            media_path=media["path"] if media else "",
            relationship_before=relationship_before.to_dict(),
            relationship_after=relationship_after.to_dict(),
            relationship_delta=relationship_delta,
            event_scores={
                "valence": event.valence,
                "severity": event.severity,
                "importance": event.importance,
                "base_score": event.base_score,
                "category": category,
            },
            transient=transient,
            context_meta=context_meta,
            energy=float(agent_after.get("energy", 100)),
            sleeping=bool(agent_after.get("sleeping", False)),
            sleep_state=str(agent_after.get("sleep_state", "awake")),
            unread_count=int(agent_after.get("unread_count", 0)),
            wake_greeting=wake_greeting,
            mock=llm_resp.mock,
            error="",
        )

    # ------------------------------------------------------------------
    # 查询/标注
    # ------------------------------------------------------------------
    def history(self, conversation_id: str, limit: int = 50) -> list[dict]:
        rows = self.db.fetchall(
            "SELECT role,content,created_at,model,mock,turn_id FROM messages WHERE conversation_id=? ORDER BY created_at ASC LIMIT ?",
            (conversation_id, limit),
        )
        return rows

    def memories(self, status: str = "active", limit: int = 50) -> list[dict]:
        return [m.to_dict() for m in self.memory.list_memories(self.persona_card.entity_id, status, limit)]

    def core_facts(self) -> list[dict]:
        return [f.__dict__ for f in self.memory.core_facts(self.persona_card.entity_id)]

    def diary_today(self) -> str:
        """兼容旧名：实际返回工作日志。"""
        return self.diary.get_today()

    def work_log_today(self) -> str:
        return self.diary.get_today()

    def persona_diary_today(self) -> str:
        return self.persona_diary.today()

    def persona_diary_meta(self) -> dict[str, Any]:
        return self.persona_diary.meta(entity_id=self.persona_card.entity_id)

    def delete_persona_diary(self, date: str | None = None) -> dict[str, Any]:
        return self.persona_diary.delete(self.persona_card.entity_id, date=date)

    def generate_persona_diary(self, date: str | None = None, force: bool = False) -> dict[str, Any]:
        return self.persona_diary.generate(self.persona_card.entity_id, self.persona_card, date=date, force=force)

    def relationship(self) -> dict[str, Any]:
        return self.persona.relationship(self.persona_card.entity_id).to_dict()

    def transient_state(self) -> dict[str, Any]:
        return self.transient.current(self.persona_card.entity_id)

    def agent_snapshot(self) -> dict[str, Any]:
        return self.agent_state.snapshot(self.persona_card.entity_id)

    def latest_dream(self) -> dict[str, Any] | None:
        return self.dream.latest(self.persona_card.entity_id)

    def latest_night_run(self) -> dict[str, Any] | None:
        return self.night.latest(self.persona_card.entity_id)

    def latest_vision_observation(self) -> dict[str, Any] | None:
        entity_id = self.persona_card.entity_id
        row = self.db.fetchone(
            "SELECT * FROM vision_observations WHERE entity_id=? ORDER BY created_at DESC LIMIT 1",
            (entity_id,),
        )
        return dict(row) if row else None

    def vision_frame(
        self, image_data: bytes, mime: str = "image/jpeg", conversation_id: str = "", force_chat: bool = False,
    ) -> dict[str, Any]:
        """摄像头一帧：视觉描述+一句回应；评论进主对话，但不保存图片/不写长期记忆。"""
        if not image_data:
            return {"ok": False, "error": "空画面"}
        if len(image_data) > MAX_BYTES:
            return {"ok": False, "error": "画面过大"}
        mime = (mime or "").lower().split(";")[0].strip()
        if mime not in ALLOWED_MIME:
            mime = "image/jpeg"
        caption = self.llm.describe_image_bytes(
            image_data, mime,
            prompt="用一句自然、具体的中文描述这个摄像头画面；只描述看得到的内容，不要识别具体人物身份、不要编造故事。",
        )
        if not caption or caption.startswith(("（mock", "（收到", "（图片")):
            return {"ok": False, "error": "当前视觉模型不可用"}
        entity_id = self.persona_card.entity_id
        relationship = self.persona.relationship(entity_id)
        core = self.memory.core_facts(entity_id)
        system_prompt = build_system_prompt(self.persona_card, relationship, core, [], self.settings.scoring)
        instruction = (
            f"你正通过摄像头看一眼。画面客观描述：{caption}\n"
            "请用一两句话自然回应（可以好奇/关心/轻声描述），不要编造画面里没有的东西，"
            "不要提到“图像识别/模型”等技术词，不要每次都重复“我看到了”。"
        )
        try:
            resp = self.llm.chat(
                [{"role": "system", "content": system_prompt}, {"role": "user", "content": instruction}],
                purpose="vision_reply", max_tokens=120, temperature=0.8,
            )
        except Exception as exc:
            return {"ok": False, "error": f"视觉回应失败：{exc}"}
        comment = (resp.text or "").strip()
        now = now_iso()
        obs_id = new_id("vision")
        self.db.execute(
            "INSERT INTO vision_observations(id,entity_id,caption,comment,created_at) VALUES(?,?,?,?,?)",
            (obs_id, entity_id, caption, comment, now),
        )
        # 摄像头评论写进主对话；按频率限流，避免刷屏/挤占最近上下文。
        message_id = ""
        cid = self.get_or_create_conversation(conversation_id or None)
        if comment:
            last_cam = self.db.fetchone(
                "SELECT created_at FROM messages WHERE conversation_id=? AND turn_id LIKE 'camera_%' "
                "ORDER BY created_at DESC LIMIT 1",
                (cid,),
            )
            last_at = self._parse_time(str(last_cam["created_at"])) if last_cam else None
            age_s = (datetime.now().astimezone() - last_at).total_seconds() if last_at else 999999
            if force_chat or age_s >= max(0, int(self.settings.vision_chat_min_seconds)):
                message_id = self._save_message(
                    cid, "assistant", comment,
                    model=resp.model, mock=resp.mock,
                    metadata={"vision": True, "caption": caption, "observation_id": obs_id},
                    turn_id=f"camera_{obs_id}",
                )
        return {
            "ok": True,
            "id": obs_id,
            "caption": caption,
            "comment": comment,
            "created_at": now,
            "model": resp.model,
            "mock": resp.mock,
            "conversation_id": cid,
            "message_id": message_id,
            "synced_to_chat": bool(message_id),
        }

    def _parse_time(self, value: str) -> datetime | None:
        try:
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo else dt.astimezone()
        except Exception:
            return None

    @staticmethod
    def _vision_relevant(text: str) -> bool:
        t = (text or "").strip()
        return bool(t) and any(cue in t for cue in _VISION_CUES)

    def _night_window(self, state: dict[str, Any]) -> tuple[str, str]:
        end = str(state.get("wake_triggered_at", "")) or now_iso()
        start = str(state.get("last_consolidated_at", "")) or str(state.get("last_wake_at", ""))
        if not start:
            # 首次整理没有上一个清醒周期锚点：最多回看一个窗口（默认24h）。
            # 不能从 sleep_start-24h 起算，否则会把“睡前24h+睡眠”拼成30h+，
            # 容易把很久以前的内容拉进梦里。
            end_dt = self._parse_time(end) or datetime.now().astimezone()
            start = (end_dt - timedelta(hours=max(1.0, float(self.settings.dream_window_hours)))).isoformat(timespec="seconds")
        return start, end

    def maybe_process_night(self) -> dict[str, Any] | None:
        """到点/强制唤醒后：整理 → 技能维护 → 回验 → 梦/直接醒 → 醒来先由她发言。"""
        entity_id = self.persona_card.entity_id
        state = self.agent_state.snapshot(entity_id)
        if not state.get("processing"):
            return None
        sleep_started = str(state.get("sleep_started_at", ""))
        existing = self.db.fetchone(
            "SELECT * FROM night_runs WHERE entity_id=? AND sleep_started_at=? ORDER BY created_at DESC LIMIT 1",
            (entity_id, sleep_started),
        )
        if existing:
            self.agent_state.finish_night(entity_id)
            return dict(existing)
        window_start, window_end = self._night_window(state)
        duration = float(state.get("sleep_duration_hours", 0) or 0)
        trigger = "backfill" if state.get("sleep_backfilled") else (
            "timer" if str(state.get("wake_triggered_at")) == str(state.get("wake_at")) else "forced"
        )
        report = self.night.run(
            entity_id,
            sleep_started,
            window_start,
            window_end,
            triggered_by=trigger,
            backfilled=bool(state.get("sleep_backfilled")),
            sleep_duration_hours=duration,
            persona_name=self.persona_card.name,
        )
        # 不足 8h 保留精算精力；正常 8h/长睡眠回满
        keep_energy = float(state.get("energy", 100)) if duration < float(self.settings.energy_sleep_hours) else None
        self.agent_state.finish_night(entity_id, energy=keep_energy, consolidated_at=window_end)
        greeting = self.generate_wake_greeting(duration=duration, backfilled=bool(state.get("sleep_backfilled")))
        if greeting:
            report["greeting"] = greeting
        return report

    def latest_wake_greeting(self) -> dict[str, Any] | None:
        row = self.db.fetchone(
            "SELECT id,conversation_id,content,created_at,turn_id FROM messages "
            "WHERE role='assistant' AND turn_id LIKE 'wake_greeting_%' ORDER BY created_at DESC LIMIT 1"
        )
        if not row:
            return None
        return {
            "id": row["id"], "conversation_id": row["conversation_id"],
            "reply": row["content"], "created_at": row["created_at"], "turn_id": row["turn_id"],
        }

    def generate_wake_greeting(self, duration: float, backfilled: bool = False) -> dict[str, Any] | None:
        """醒来后的第一条消息必须由她主动发出；注入本次睡眠时长。"""
        if not self.settings.wake_greeting_enabled:
            return None
        entity_id = self.persona_card.entity_id
        relationship = self.persona.relationship(entity_id)
        core = self.memory.core_facts(entity_id)
        system_prompt = build_system_prompt(self.persona_card, relationship, core, [], self.settings.scoring)
        hours = max(0.0, float(duration))
        instruction = (
            f"你刚睡醒。本次实际睡了约 {hours:.1f} 小时"
            + ("（这是根据关机/离线时间补录的睡眠）" if backfilled else "")
            + "。请主动给用户发一条醒来后的第一条消息：先自然说明你醒了，并提到这次睡了多久；"
            "不要等用户先开口，不要编造没发生的事，不要机械报时。"
        )
        try:
            resp = self.llm.chat(
                [{"role": "system", "content": system_prompt}, {"role": "user", "content": instruction}],
                purpose="respond", max_tokens=220, temperature=0.85,
            )
        except Exception:
            return None
        reply = (resp.text or "").strip() or "我醒啦，睡了好几个小时。你还在吗？"
        cid = self.get_or_create_conversation()
        row = self.db.fetchone("SELECT id FROM conversations WHERE entity_id=? ORDER BY updated_at DESC LIMIT 1", (entity_id,))
        if row:
            cid = str(row["id"])
        turn_id = f"wake_greeting_{new_id('turn')}"
        mid = self._save_message(
            cid, "assistant", reply, model=resp.model, mock=resp.mock,
            metadata={"wake_greeting": True, "sleep_hours": round(hours, 2), "backfilled": backfilled},
            turn_id=turn_id,
        )
        return {
            "id": mid, "reply": reply, "sleep_hours": round(hours, 2),
            "backfilled": backfilled, "conversation_id": cid, "turn_id": turn_id,
            "model": resp.model, "mock": resp.mock,
        }

    def force_wake(self) -> dict[str, Any]:
        """强制叫醒：不足 8h 也进入整理，整理后醒来。"""
        entity_id = self.persona_card.entity_id
        state = self.agent_state.force_wake(entity_id)
        if state.get("processing"):
            report = self.maybe_process_night()
            return {"ok": True, "report": report, "state": self.agent_snapshot()}
        return {"ok": False, "error": "当前不在睡眠中", "state": state}

    def latest_wake_reply(self) -> dict[str, Any] | None:
        row = self.db.fetchone(
            "SELECT id,conversation_id,content,created_at,turn_id FROM messages "
            "WHERE role='assistant' AND turn_id LIKE 'wake_%' "
            "AND turn_id NOT LIKE 'wake_greeting_%' ORDER BY created_at DESC LIMIT 1"
        )
        if not row:
            return None
        return {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "reply": row["content"],
            "created_at": row["created_at"],
            "turn_id": row["turn_id"],
        }

    def wake_batch_reply(self) -> dict[str, Any] | None:
        """C4：醒来后把睡眠中的未读合并成一次回复，并标记已读。"""
        entity_id = self.persona_card.entity_id
        state = self.agent_state.settle(entity_id)
        if state.get("sleeping") or state.get("processing"):
            return None
        rows = self.agent_state.fetch_unread(entity_id, limit=20)
        if not rows:
            return None
        cid = str(rows[-1].get("conversation_id") or "") or self.get_or_create_conversation()
        if not self.db.fetchone("SELECT id FROM conversations WHERE id=?", (cid,)):
            cid = self.get_or_create_conversation()
        lines = []
        for row in rows:
            ts = str(row.get("created_at", ""))[11:16]
            lines.append(f"[{ts}] {row['content']}")
        batch_text = (
            "【你刚睡醒。你睡着的时候，用户给你发了这些消息：】\n"
            + "\n".join(lines)
            + "\n\n请用你平时的语气回应用户：先自然说明你刚醒/看到消息；可以合并回应，但不要机械逐条复述；不要编造没发生的事。"
        )
        relationship = self.persona.relationship(entity_id)
        core = self.memory.core_facts(entity_id)
        system_prompt = build_system_prompt(
            self.persona_card, relationship, core, [], self.settings.scoring
        )
        mention_dream = self.dream.latest_unmentioned(entity_id)
        if mention_dream:
            batch_text += (
                "\n\n【你昨晚做过的梦（只作为梦记得，不要说成真实发生的事）】\n"
                + str(mention_dream.get("content", ""))[:500]
                + "\n你可以在醒来回复里自然提起这个梦。"
            )
        try:
            resp = self.llm.chat(
                [{"role": "system", "content": system_prompt}, {"role": "user", "content": batch_text}],
                purpose="respond",
                context={"wake_batch": len(rows), "event": {"valence": 0.0, "severity": 1, "importance": 0.3}},
                max_tokens=320,
                temperature=0.85,
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc), "count": len(rows)}
        reply = (resp.text or "").strip() or "唔……我醒了，刚看到你的消息。"
        turn_id = f"wake_{new_id('turn')}"
        mid = self._save_message(
            cid, "assistant", reply, model=resp.model, mock=resp.mock,
            metadata={"wake_batch": len(rows)}, turn_id=turn_id,
        )
        self.agent_state.mark_unread_answered([str(r["id"]) for r in rows])
        if mention_dream:
            self.dream.mark_mentioned(str(mention_dream.get("id", "")))
        return {
            "ok": True,
            "id": mid,
            "reply": reply,
            "count": len(rows),
            "conversation_id": cid,
            "turn_id": turn_id,
            "model": resp.model,
            "mock": resp.mock,
        }

    def retrieval_logs(self, limit: int = 20) -> list[dict]:
        return self.db.fetchall(
            "SELECT * FROM retrieval_logs WHERE entity_id=? ORDER BY at DESC, rowid DESC LIMIT ?",
            (self.persona_card.entity_id, limit),
        )

    def inner_thoughts(self, limit: int = 20) -> list[dict]:
        return self.db.fetchall(
            "SELECT * FROM inner_thoughts WHERE entity_id=? ORDER BY at DESC LIMIT ?",
            (self.persona_card.entity_id, limit),
        )

    def recent_turns(self, limit: int = 30) -> list[dict]:
        """供人工标注页使用：把同一 turn 的 user/assistant 配对。"""
        rows = self.db.fetchall(
            """SELECT u.turn_id, u.conversation_id, u.content AS user_text,
                      a.content AS assistant_text, a.created_at AS at,
                      (SELECT COUNT(*) FROM annotations x WHERE x.turn_id=u.turn_id) AS annotated
               FROM messages u
               JOIN messages a ON a.conversation_id=u.conversation_id AND a.turn_id=u.turn_id
               JOIN conversations c ON c.id=u.conversation_id
               WHERE u.role='user' AND a.role='assistant' AND c.entity_id=? AND u.turn_id != ''
               ORDER BY u.created_at DESC LIMIT ?""",
            (self.persona_card.entity_id, limit),
        )
        return rows

    def annotations(self, limit: int = 50) -> list[dict]:
        return self.db.fetchall(
            "SELECT * FROM annotations WHERE entity_id=? ORDER BY created_at DESC LIMIT ?",
            (self.persona_card.entity_id, limit),
        )

    def annotate(self, conversation_id: str, turn_id: str, scores: dict[str, Any], notes: str = "", annotator: str = "user") -> str:
        aid = new_id("ann")
        self.db.execute(
            "INSERT INTO annotations(id,entity_id,conversation_id,turn_id,annotator,scores_json,notes,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                aid,
                self.persona_card.entity_id,
                conversation_id,
                turn_id,
                annotator,
                json.dumps(scores, ensure_ascii=False),
                notes,
                now_iso(),
            ),
        )
        return aid

    def stats(self) -> dict:
        stats = self.memory.stats(self.persona_card.entity_id)
        stats.update(
            {
                "entity_id": self.persona_card.entity_id,
                "persona": self.persona_card.name,
                "mock": self.llm.is_mock,
                "conversations": (self.db.fetchone("SELECT COUNT(*) AS n FROM conversations WHERE entity_id=?", (self.persona_card.entity_id,)) or {}).get("n", 0),
                "messages": (self.db.fetchone("SELECT COUNT(*) AS n FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE c.entity_id=?", (self.persona_card.entity_id,)) or {}).get("n", 0),
                "diary_days": len(self.diary.list_days()),
            }
        )
        return stats
