"""工作日志（原“日记”实现）。

注意：这里写的是**系统工作日志**（给用户/研究者看），不是她的人格化日记。
人格化日记见 `persona_diary.py`，写到 `data/diary/persona/`。

历史迁移：
- 旧版 `data/diary/*.md` 实际是工作日志；首次初始化时复制到
  `data/logs/daily_report/`，旧文件保留不删。
- 页面/接口旧字段名 `diary_today` 仍保留为工作日志，避免破坏兼容。
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from .config import Settings
from .db import Database, new_id, now_iso, today_str
from .models import Event


class DiaryService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.dir: Path = settings.data_dir / "logs" / "daily_report"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.persona_dir: Path = settings.data_dir / "diary" / "persona"
        self.persona_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_legacy()

    def _migrate_legacy(self) -> None:
        legacy = self.settings.data_dir / "diary"
        if not legacy.is_dir():
            return
        try:
            files = [p for p in legacy.glob("*.md") if p.is_file()]
            if files and not any(self.dir.glob("*.md")):
                for p in files:
                    shutil.copy2(p, self.dir / p.name)
        except Exception:
            pass

    def append_turn(self, entity_id: str, event: Event, reply: str, inner: str = "") -> str:
        date = today_str()
        path = self.dir / f"{date}.md"
        time_str = datetime.now().astimezone().strftime("%H:%M")
        line = f"- {time_str} 你说：{event.summary.strip()} ｜ 我心里：{(inner or reply).strip()[:80]}"
        if path.exists():
            content = path.read_text(encoding="utf-8").rstrip() + "\n" + line + "\n"
        else:
            content = f"# {date} · 工作日志\n\n" + line + "\n"
        path.write_text(content, encoding="utf-8")
        existing = self.db.fetchone("SELECT id FROM diary_entries WHERE entity_id=? AND date=?", (entity_id, date))
        now = now_iso()
        if existing:
            self.db.execute(
                "UPDATE diary_entries SET content=?, updated_at=? WHERE id=?",
                (content, now, existing["id"]),
            )
        else:
            self.db.execute(
                "INSERT INTO diary_entries(id,entity_id,date,content,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (new_id("log"), entity_id, date, content, now, now),
            )
        return str(path)

    def get_today(self) -> str:
        path = self.dir / f"{today_str()}.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def list_days(self, limit: int = 30) -> list[str]:
        return [p.stem for p in sorted(self.dir.glob("*.md"), reverse=True)[:limit]]
