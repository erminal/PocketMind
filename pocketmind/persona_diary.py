"""她的人格化日记（与工作日志分开）。

关键原则（2026-10-26 修正）：
- 日记必须能追溯到**当天真实活动**（消息、事件、记忆、关系变化、媒体）；
- 禁止模型凭空编造清单外的人物/地点/事件/物品/承诺；
- 生成时要求模型返回 source_ids（活动编号），校验不过则使用“真实活动清单”生成兜底日记；
- 不进入主记忆检索，不自动展示给用户，生成失败不影响聊天。

输出：
- data/diary/persona/YYYY-MM-DD.md
- persona_diaries.content + source_ids_json
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .config import Settings
from .db import Database, new_id, now_iso, today_str
from .llm import FallbackLLM
from .models import PersonaCard

MAX_ACTIVITIES = 60


class PersonaDiaryService:
    def __init__(self, db: Database, settings: Settings, llm: FallbackLLM):
        self.db = db
        self.settings = settings
        self.llm = llm
        self.dir: Path = settings.data_dir / "diary" / "persona"
        self.dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, date: str) -> Path:
        return self.dir / f"{date}.md"

    def today(self) -> str:
        path = self.path_for(today_str())
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def get(self, date: str) -> str:
        path = self.path_for(date)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def exists(self, date: str) -> bool:
        return self.path_for(date).exists()

    def delete(self, entity_id: str, date: str | None = None) -> dict:
        """删除某天的人格化日记（文件 + DB 记录）；工作日志不受影响。"""
        date = date or today_str()
        path = self.path_for(date)
        existed = path.exists()
        if existed:
            try:
                path.unlink()
            except OSError as exc:
                return {"ok": False, "deleted": False, "error": str(exc), "date": date}
        rows = self.db.execute("DELETE FROM persona_diaries WHERE entity_id=? AND date=?", (entity_id, date))
        return {"ok": True, "deleted": existed or bool(rows), "date": date, "path": str(path)}

    def meta(self, date: str | None = None, entity_id: str | None = None) -> dict:
        date = date or today_str()
        if entity_id:
            row = self.db.fetchone(
                "SELECT source_ids_json, model, updated_at FROM persona_diaries WHERE entity_id=? AND date=?",
                (entity_id, date),
            )
        else:
            row = self.db.fetchone(
                "SELECT source_ids_json, model, updated_at FROM persona_diaries WHERE entity_id=(SELECT value FROM meta WHERE key='default_entity_id') AND date=?",
                (date,),
            )
        if not row:
            return {"source_ids": [], "source_count": 0, "model": "", "updated_at": ""}
        try:
            ids = json.loads(row.get("source_ids_json") or "[]")
        except Exception:
            ids = []
        return {
            "source_ids": ids if isinstance(ids, list) else [],
            "source_count": len(ids) if isinstance(ids, list) else 0,
            "model": row.get("model", ""),
            "updated_at": row.get("updated_at", ""),
        }

    # ------------------------------------------------------------------
    # 真实活动清单
    # ------------------------------------------------------------------
    def collect_activities(self, entity_id: str, date: str) -> list[dict]:
        activities: list[dict] = []

        def add(kind: str, at: str, text: str, source_id: str) -> None:
            text = " ".join(str(text or "").split()).strip()
            if not text:
                return
            activities.append({"id": source_id, "kind": kind, "at": str(at or ""), "text": text[:240]})

        for row in self.db.fetchall(
            """SELECT m.id,m.role,m.content,m.created_at FROM messages m
               JOIN conversations c ON c.id=m.conversation_id
               WHERE c.entity_id=? AND substr(m.created_at,1,10)=?
               ORDER BY m.created_at ASC LIMIT 80""",
            (entity_id, date),
        ):
            who = "用户说" if row["role"] == "user" else "我说"
            add("message", row["created_at"], f"{who}：{row['content']}", f"msg:{row['id']}")

        for row in self.db.fetchall(
            """SELECT id,summary,detail,occurred_at FROM events
               WHERE entity_id=? AND substr(occurred_at,1,10)=?
               ORDER BY occurred_at ASC LIMIT 60""",
            (entity_id, date),
        ):
            text = row["summary"] or row["detail"]
            add("event", row["occurred_at"], f"发生：{text}", f"evt:{row['id']}")

        for row in self.db.fetchall(
            """SELECT id,content,category,created_at FROM memories
               WHERE entity_id=? AND substr(created_at,1,10)=?
               ORDER BY created_at ASC LIMIT 60""",
            (entity_id, date),
        ):
            add("memory", row["created_at"], f"我记住了：{row['content']}", f"mem:{row['id']}")

        for row in self.db.fetchall(
            """SELECT id,at,field,delta,reason FROM relationship_events
               WHERE entity_id=? AND substr(at,1,10)=?
               ORDER BY at ASC LIMIT 40""",
            (entity_id, date),
        ):
            try:
                delta = float(row["delta"])
            except Exception:
                delta = 0.0
            if abs(delta) >= 0.01:
                add(
                    "relationship",
                    row["at"],
                    f"关系变化：{row['field']} {'+' if delta > 0 else ''}{delta:.2f}（{row['reason']}）",
                    f"rel:{row['id']}",
                )

        for row in self.db.fetchall(
            """SELECT id,caption,created_at FROM media
               WHERE entity_id=? AND substr(created_at,1,10)=?
               ORDER BY created_at ASC LIMIT 20""",
            (entity_id, date),
        ):
            if row["caption"]:
                add("media", row["created_at"], f"图片：{row['caption']}", f"med:{row['id']}")

        activities.sort(key=lambda x: x["at"])
        # 去重：同一条消息可能同时被抽成事件/记忆，保留最具体的前两条
        seen: set[str] = set()
        unique: list[dict] = []
        for item in activities:
            key = item["text"][:80]
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique[:MAX_ACTIVITIES]

    @staticmethod
    def _activity_line(item: dict) -> str:
        at = item.get("at", "")
        time_text = at[11:16] if len(at) >= 16 and at[10] == "T" else at
        return f"[{item['id']}] {time_text} {item['text']}"

    def _fallback_diary(self, persona: PersonaCard, activities: list[dict]) -> tuple[str, list[str]]:
        """模型不合格时的兜底：只用真实活动清单，不添加清单外事实。"""
        ids = [a["id"] for a in activities[:10]]
        lines = []
        for item in activities[:8]:
            text = item["text"]
            if item["kind"] == "message":
                lines.append(text)
            elif item["kind"] == "event":
                lines.append(f"今天{text}")
            elif item["kind"] == "memory":
                lines.append(text)
            elif item["kind"] == "relationship":
                lines.append(f"我感觉{text.replace('关系变化：', '')}")
            else:
                lines.append(text)
        body = "；".join(lines)
        diary = (
            f"今天我记得这些真实的事：{body}。"
            f"我把它们写下来。\n"
        )
        return diary, ids

    @staticmethod
    def _parse_model_json(text: str) -> dict | None:
        raw = text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
        try:
            data = json.loads(raw)
        except Exception:
            match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
            if not match:
                return None
            try:
                data = json.loads(match.group(0))
            except Exception:
                return None
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------------
    def generate(self, entity_id: str, persona: PersonaCard, date: str | None = None, force: bool = False) -> dict:
        date = date or today_str()
        path = self.path_for(date)
        if path.exists() and not force:
            meta = self.meta(date, entity_id)
            # 旧版日记没有 source_ids，视为未追溯版本；有真实活动时自动重生成，
            # 避免用户继续看到“按人设编”的旧内容。
            if meta.get("source_count", 0) > 0:
                return {
                    "ok": True,
                    "created": False,
                    "path": str(path),
                    "content": path.read_text(encoding="utf-8"),
                    "meta": meta,
                }

        activities = self.collect_activities(entity_id, date)
        if not activities:
            return {
                "ok": False,
                "created": False,
                "error": f"{date} 没有可依据的真实活动，未生成日记（避免写假日记）",
                "activity_count": 0,
            }

        activity_text = "\n".join(self._activity_line(a) for a in activities)
        messages = [
            {
                "role": "system",
                "content": (
                    f"你是{persona.name}，{persona.effective_age()} 岁（心智年龄），{persona.species}。"
                    "请用第一人称写今天的日记，100–300 字；像真实的小孩/小生物，可以写心情和感受。\n"
                    "硬性规则：你只能使用下面「当天真实活动清单」里的事实；不得新增清单外的"
                    "人物、地点、物品、承诺、天气、数字或事件。每一条 diary 内容都必须能对应至少一个活动编号。\n"
                    "禁止写成任务报告，禁止提 AI/模型/系统。\n"
                    "只输出严格 JSON，不要 Markdown："
                    '{"source_ids":["活动编号"],"diary":"日记正文"}'
                ),
            },
            {
                "role": "user",
                "content": f"日期：{date}\n当天真实活动清单（只能据此写）：\n{activity_text}",
            },
        ]
        model = ""
        source_ids: list[str] = []
        content = ""
        try:
            resp = self.llm.chat(
                messages,
                purpose="diary",
                max_tokens=500,
                temperature=0.7,
                context={"activities": activities, "persona": persona.to_dict(), "date": date},
            )
            model = resp.model
            payload = self._parse_model_json(resp.text or "")
            if payload:
                raw_ids = payload.get("source_ids") or []
                if isinstance(raw_ids, list):
                    valid = {a["id"] for a in activities}
                    source_ids = [str(x) for x in raw_ids if str(x) in valid]
                content = str(payload.get("diary") or "").strip()
            else:
                content = (resp.text or "").strip()
        except Exception as exc:
            return {"ok": False, "created": False, "error": str(exc), "activity_count": len(activities)}

        # 模型没给合法来源/没写内容：用真实活动兜底，绝不写假日记
        if not content or not source_ids:
            content, source_ids = self._fallback_diary(persona, activities)
            model = (model or "fallback") + "+grounded-fallback"

        content = f"# {date} · {persona.name}的日记\n\n{content.rstrip()}\n"
        path.write_text(content, encoding="utf-8")
        now = now_iso()
        source_json = json.dumps(source_ids, ensure_ascii=False)
        existing = self.db.fetchone("SELECT id FROM persona_diaries WHERE entity_id=? AND date=?", (entity_id, date))
        if existing:
            self.db.execute(
                "UPDATE persona_diaries SET content=?, model=?, source_ids_json=?, updated_at=? WHERE id=?",
                (content, model, source_json, now, existing["id"]),
            )
        else:
            self.db.execute(
                "INSERT INTO persona_diaries(id,entity_id,date,content,model,source_ids_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (new_id("pdiary"), entity_id, date, content, model, source_json, now, now),
            )
        return {
            "ok": True,
            "created": True,
            "path": str(path),
            "content": content,
            "model": model,
            "source_ids": source_ids,
            "source_count": len(source_ids),
            "activity_count": len(activities),
        }
