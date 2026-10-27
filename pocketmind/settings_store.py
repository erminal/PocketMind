"""管理台配置持久化：data/settings.json。

为什么单独一个文件：
- 用户不想手写 .env；UI 保存到这里即可；
- 文件在 data/ 下，已被 .gitignore 排除；
- 优先级 settings.json > 环境变量 > 默认值（由 config.Settings 读取）。

注意：API Key 以明文存在本机 data/settings.json；只在本机使用，不要提交/外传。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_raw(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def save_raw(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def merge_section(path: Path, section: str, values: dict[str, Any]) -> dict[str, Any]:
    data = load_raw(path)
    current = data.get(section)
    if not isinstance(current, dict):
        current = {}
    current.update(values)
    data[section] = current
    save_raw(path, data)
    return data


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "••••"
    return f"••••{key[-4:]}"
