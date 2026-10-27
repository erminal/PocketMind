"""数据导出/导入：给“新会话”与手动备份用。

导出内容（本地文件夹）：
- pocketmind.db（SQLite 一致性备份）
- export.json（人格/关系/记忆/核心事实/事件/消息等可读快照）
- prompt_template.md、settings.json
- secrets/（AES-256-GCM 加密的 API Key 表 + DPAPI 主密钥）
- diary/、media/（如果存在）
- manifest.json

注意：settings.json 不再含明文 API Key；secrets/ 绑定当前 Windows 用户，
换 Windows 用户/电脑后无法解密，需要在管理台重新填写 Key。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database

TABLES_FOR_SNAPSHOT = [
    "entities",
    "persona_versions",
    "relationship_state",
    "relationship_events",
    "memories",
    "core_facts",
    "core_fact_conflicts",
    "agent_state",
    "unread_messages",
    "dreams",
    "nightly_summaries",
    "vision_observations",
    "skills",
    "skill_events",
    "night_runs",
    "schema_migrations",
    "meta",
    "events",
    "messages",
    "diary_entries",
    "persona_diaries",
    "annotations",
    "impressions",
    "recall_logs",
    "forget_events",
    "retrieval_logs",
    "media",
]


class BackupService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.exports_dir = settings.data_dir / "exports"
        self.exports_dir.mkdir(parents=True, exist_ok=True)

    def _stamp(self) -> str:
        return datetime.now().strftime("%Y%m%d-%H%M%S")

    def _unique_dir(self, prefix: str) -> Path:
        base = f"{prefix}-{self._stamp()}"
        path = self.exports_dir / base
        n = 1
        while path.exists():
            path = self.exports_dir / f"{base}-{n}"
            n += 1
        path.mkdir(parents=True, exist_ok=False)
        return path

    def create(self, prefix: str = "backup") -> dict[str, Any]:
        out = self._unique_dir(prefix)
        db_path = self.settings.db_path
        if db_path.exists():
            src = sqlite3.connect(db_path)
            dst = sqlite3.connect(out / "pocketmind.db")
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        for name in ("prompt_template.md", "prompt_template.meta.json", "settings.json"):
            src_file = self.settings.data_dir / name
            if src_file.exists():
                shutil.copy2(src_file, out / name)
        for folder in ("diary", "logs", "media", "secrets"):
            src_dir = self.settings.data_dir / folder
            if src_dir.exists():
                shutil.copytree(src_dir, out / folder, dirs_exist_ok=True)

        snapshot: dict[str, Any] = {"created_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        try:
            conn = sqlite3.connect(out / "pocketmind.db")
            conn.row_factory = sqlite3.Row
            for table in TABLES_FOR_SNAPSHOT:
                try:
                    rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
                    snapshot[table] = rows
                except sqlite3.OperationalError:
                    snapshot[table] = []
            conn.close()
        except Exception as exc:
            snapshot["error"] = str(exc)
        (out / "export.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

        manifest = {
            "name": out.name,
            "created_at": snapshot["created_at"],
            "files": sorted(p.name for p in out.iterdir()),
            "counts": {
                "memories": len(snapshot.get("memories", [])),
                "events": len(snapshot.get("events", [])),
                "messages": len(snapshot.get("messages", [])),
                "persona_versions": len(snapshot.get("persona_versions", [])),
            },
            "warning": "secrets/ 使用 AES-256-GCM + Windows DPAPI 加密；换 Windows 用户/电脑后无法解密，需重新填写 API Key。",
        }
        (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "name": out.name, "path": str(out), "manifest": manifest}

    def list(self) -> list[dict[str, Any]]:
        items = []
        for path in sorted(self.exports_dir.iterdir(), reverse=True):
            if not path.is_dir():
                continue
            manifest = {}
            try:
                manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            except Exception:
                pass
            items.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "created_at": manifest.get("created_at", ""),
                    "counts": manifest.get("counts", {}),
                }
            )
        return items

    def restore(self, name: str) -> dict[str, Any]:
        name = (name or "").strip()
        if not name or "/" in name or "\\" in name or ".." in name:
            return {"ok": False, "error": "备份名称不合法"}
        src = self.exports_dir / name
        if not src.is_dir() or not (src / "pocketmind.db").exists():
            return {"ok": False, "error": "找不到备份或缺少 pocketmind.db"}
        safety = self.create(prefix="pre-import")
        db_path = self.settings.db_path
        try:
            tmp = db_path.with_suffix(".restore.tmp")
            shutil.copy2(src / "pocketmind.db", tmp)
            for suffix in ("-wal", "-shm"):
                p = Path(str(db_path) + suffix)
                if p.exists():
                    p.unlink()
            if db_path.exists():
                db_path.unlink()
            tmp.replace(db_path)
            for name_file in ("prompt_template.md", "prompt_template.meta.json", "settings.json"):
                f = src / name_file
                if f.exists():
                    shutil.copy2(f, self.settings.data_dir / name_file)
            for folder in ("diary", "logs", "media", "secrets"):
                f = src / folder
                if f.is_dir():
                    shutil.copytree(f, self.settings.data_dir / folder, dirs_exist_ok=True)
            # 简单完整性检查
            db = Database(db_path)
            db.init_schema()
            counts = db.fetchone("SELECT COUNT(*) AS n FROM memories") or {"n": 0}
            return {"ok": True, "name": name, "safety_backup": safety["name"], "memories": counts["n"]}
        except Exception as exc:
            return {"ok": False, "error": f"{exc}", "safety_backup": safety.get("name", "")}
