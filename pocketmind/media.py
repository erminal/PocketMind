"""图片/媒体：本地保存 + 生成 caption。

M1 原则：
- 图片只存本地 `data/media/`，数据库存路径/哈希/说明；
- 有真实 vision 能力时用模型生成一句话描述；无 Key/mock 时用占位 caption；
- caption 作为一条 MEDIA 记忆进入记忆库，但原图不被注入 prompt（只给路径/说明）；
- 不做人脸识别、不做身份绑定（符合单主人/隐私设计）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .config import Settings
from .db import Database, new_id, now_iso
from .llm import FallbackLLM

ALLOWED_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
MAX_BYTES = 8 * 1024 * 1024


class MediaService:
    def __init__(self, db: Database, settings: Settings, llm: FallbackLLM):
        self.db = db
        self.settings = settings
        self.llm = llm
        self.dir = settings.data_dir / "media"
        self.dir.mkdir(parents=True, exist_ok=True)

    def save_bytes(
        self,
        entity_id: str,
        conversation_id: str,
        turn_id: str,
        data: bytes,
        filename: str = "",
        mime: str = "",
    ) -> dict:
        if not data:
            raise ValueError("图片为空")
        if len(data) > MAX_BYTES:
            raise ValueError(f"图片超过 {MAX_BYTES // (1024 * 1024)}MB 限制")
        mime = (mime or "").lower().strip()
        if mime not in ALLOWED_MIME:
            # 有些浏览器给空 mime；按文件名兜底
            suffix = Path(filename or "").suffix.lower()
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                    ".webp": "image/webp", ".gif": "image/gif"}.get(suffix, "")
        if mime not in ALLOWED_MIME:
            raise ValueError("只支持 PNG/JPEG/WebP/GIF 图片")
        media_id = new_id("media")
        suffix = ALLOWED_MIME[mime]
        folder = self.dir / entity_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{media_id}{suffix}"
        path.write_bytes(data)
        caption = ""
        try:
            caption = self.llm.describe_image(str(path), mime)
        except Exception:
            caption = "（收到一张图片；视觉识别暂不可用）"
        caption = (caption or "").strip() or "（收到一张图片）"
        now = now_iso()
        self.db.execute(
            """INSERT INTO media(id,entity_id,conversation_id,turn_id,path,mime,size,caption,memory_id,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (media_id, entity_id, conversation_id, turn_id, str(path), mime, len(data), caption, "", now),
        )
        return {
            "id": media_id,
            "path": str(path),
            "mime": mime,
            "size": len(data),
            "caption": caption,
            "sha256": hashlib.sha256(data).hexdigest()[:16],
        }

    def attach_memory(self, media_id: str, memory_id: str) -> None:
        self.db.execute("UPDATE media SET memory_id=? WHERE id=?", (memory_id, media_id))

    def list_media(self, entity_id: str, limit: int = 20) -> list[dict]:
        return self.db.fetchall(
            "SELECT * FROM media WHERE entity_id=? ORDER BY created_at DESC LIMIT ?",
            (entity_id, limit),
        )
