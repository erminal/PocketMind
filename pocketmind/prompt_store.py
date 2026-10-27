"""Prompt 模板：外置到 data/prompt_template.md，UI 可编辑，无需改 .py/重打包。

模板使用 `{{placeholder}}` 占位符；程序每轮读取模板并渲染。
身份基线/名字是必需占位符（防止误删后人格失去披露）。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import PACKAGE_ROOT, Settings

TEMPLATE_NAME = "prompt_template.md"
META_NAME = "prompt_template.meta.json"
DEFAULT_TEMPLATE = PACKAGE_ROOT / "templates" / "default_prompt_template.md"
REQUIRED = ("name", "identity_note")
_PLACEHOLDER_RE = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")

PLACEHOLDERS: dict[str, str] = {
    "name": "角色名字（必需）",
    "age": "当前展示年龄（生日追踪开启时自动增长）",
    "age_base": "年龄基准值（生日追踪开启时的初始年龄）",
    "gender": "性别",
    "species": "物种/身份设定",
    "origin": "来源原始值（created 等；不建议直接当事实）",
    "origin_story": "来源故事原始文本",
    "identity_note": "身份基线：被创造的智能生命、被问如实说明（必需，建议保留）",
    "current_datetime": "当前日期/时间（自动注入，让她知道现在几点/星期几）",
    "origin_setting_section": "非 created 来源的安全说明段（自动生成）",
    "origin_story_section": "来源故事段（自动加『这是设定』提示）",
    "values": "价值观（最多 5 项）",
    "traits": "性格（最多 5 项）",
    "speech_section": "说话风格段（句长/语气词/禁词/表情）",
    "address": "对用户的称呼",
    "vocab_level": "用词水平",
    "boundaries": "边界（最多 6 项）",
    "known_section": "熟悉领域段",
    "unknown_section": "不懂领域/好奇方式段",
    "custom_section": "补充 Prompt 段",
    "relationship": "关系快照",
    "gate_section": "记忆门提示（闲聊/显式线索）",
    "interlocutor_section": "对方情绪推断段",
    "core_facts_section": "核心事实段",
    "memories_section": "相关记忆段",
}


def default_template() -> str:
    try:
        return DEFAULT_TEMPLATE.read_text(encoding="utf-8")
    except Exception:
        return "你是「{{name}}」，{{age}} 岁。\n{{identity_note}}\n{{custom_section}}\n"


def template_path(settings: Settings) -> Path:
    return settings.data_dir / TEMPLATE_NAME


def meta_path(settings: Settings) -> Path:
    return settings.data_dir / META_NAME


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _write_meta(settings: Settings, text: str, version: int, reason: str = "save") -> dict[str, Any]:
    meta = {
        "version": int(version),
        "sha256": _hash(text),
        "chars": len(text),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "reason": reason,
    }
    try:
        meta_path(settings).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return meta


def template_meta(settings: Settings) -> dict[str, Any]:
    """模板版本/哈希；手工改文件会导致 hash 不一致，但不会静默升版本。"""
    path = template_path(settings)
    if not path.exists():
        return {"version": 0, "sha256": "", "chars": 0, "updated_at": "", "reason": "missing", "modified": False}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        text = ""
    meta: dict[str, Any] = {}
    try:
        if meta_path(settings).exists():
            raw = json.loads(meta_path(settings).read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                meta = raw
    except Exception:
        meta = {}
    if not meta:
        meta = _write_meta(settings, text, 1, "bootstrap")
    current_hash = _hash(text)
    meta.setdefault("version", 1)
    meta["modified"] = bool(meta.get("sha256") and meta["sha256"] != current_hash)
    meta["current_hash"] = current_hash
    meta["chars"] = len(text)
    return meta


def _ensure_meta(settings: Settings, text: str) -> None:
    if not meta_path(settings).exists():
        _write_meta(settings, text, 1, "bootstrap")


def get_template(settings: Settings) -> str:
    """读取 data/prompt_template.md；不存在时把默认模板复制过去。"""
    path = template_path(settings)
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
            if text.strip():
                _ensure_meta(settings, text)
                return text
        except Exception:
            pass
    text = default_template()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        _write_meta(settings, text, 1, "bootstrap")
    except Exception:
        pass
    return text


def validate_template(text: str) -> dict[str, Any]:
    if not text or not text.strip():
        return {"ok": False, "error": "模板不能为空。", "missing": list(REQUIRED), "unknown": []}
    found = set(_PLACEHOLDER_RE.findall(text))
    missing = [key for key in REQUIRED if key not in found]
    unknown = sorted(found - set(PLACEHOLDERS))
    if missing or unknown:
        parts = []
        if missing:
            parts.append("缺少必需占位符：" + "、".join(missing))
        if unknown:
            parts.append("未知占位符：" + "、".join(unknown))
        return {"ok": False, "error": "；".join(parts), "missing": missing, "unknown": unknown}
    return {"ok": True, "error": "", "missing": [], "unknown": []}


def save_template(settings: Settings, text: str) -> dict[str, Any]:
    check = validate_template(text)
    if not check["ok"]:
        return check
    old = template_meta(settings)
    version = int(old.get("version", 0) or 0) + 1
    path = template_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    meta = _write_meta(settings, text, version, "save")
    return {"ok": True, "error": "", "path": str(path), "meta": meta}


def reset_template(settings: Settings) -> dict[str, Any]:
    text = default_template()
    result = save_template(settings, text)
    result["template"] = text
    return result


def render_template(text: str, context: dict[str, Any]) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        value = context.get(key, "")
        return "" if value is None else str(value)

    return _PLACEHOLDER_RE.sub(repl, text)
