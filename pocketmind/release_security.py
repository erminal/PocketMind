"""发布清单 / 源码哈希 / 完整性校验。

注意：哈希不是签名。没有代码签名证书时，它只能证明“文件与清单一致”，
不能证明“清单由官方发布”。manifest 保留 code_signing 字段，等证书到位后签。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SOURCE_DIRS = ("pocketmind", "tests", "desktop", "config")
SOURCE_FILES = (
    "README.md",
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "build_exe.ps1",
    "PocketMind.spec",
    "run_exe.py",
    "启动PocketMind.cmd",
)
EXCLUDE_DIR_NAMES = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".ruff_cache", "dist", "build", "data", "backups", "media", "logs",
    ".uv-cache", ".gradle", ".kotlin", "exports",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _source_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for dirname in SOURCE_DIRS:
        base = root / dirname
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if any(part in EXCLUDE_DIR_NAMES for part in path.relative_to(root).parts):
                continue
            if path.suffix.lower() in {".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3"}:
                continue
            found.append(path)
    for name in SOURCE_FILES:
        path = root / name
        if path.is_file():
            found.append(path)
    return sorted(set(found), key=lambda p: p.relative_to(root).as_posix())


def compute_source_hash(root: Path) -> str:
    root = Path(root)
    h = hashlib.sha256()
    for path in _source_files(root):
        rel = path.relative_to(root).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(sha256_file(path).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *args],
            text=True, encoding="utf-8", errors="replace", stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return ""


def build_manifest(root: Path, backend_exe: Path | None = None, desktop_exe: Path | None = None) -> dict[str, Any]:
    root = Path(root)
    artifacts: dict[str, Any] = {}
    for name, path in (("backend_exe", backend_exe), ("desktop_exe", desktop_exe)):
        if path and Path(path).is_file():
            artifacts[name] = {
                "name": Path(path).name,
                "sha256": sha256_file(Path(path)),
                "size": Path(path).stat().st_size,
            }
    for name, rel in (("desktop_main", "desktop/main.js"), ("desktop_loading", "desktop/loading.html")):
        path = root / rel
        if path.is_file():
            artifacts[name] = {"name": path.name, "sha256": sha256_file(path), "size": path.stat().st_size}
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "git_commit": _git(root, "rev-parse", "HEAD"),
        "git_dirty": bool(_git(root, "status", "--porcelain")),
        "source_hash": "sha256:" + compute_source_hash(root),
        "artifacts": artifacts,
        "code_signing": {
            "status": "unsigned",
            "detail": "当前没有代码签名证书；这是完整性清单，不是可信签名。",
        },
    }
    return manifest


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _candidate_manifests(settings: Any) -> list[Path]:
    paths: list[Path] = []
    exe_dir = Path(sys.executable).resolve().parent
    paths.append(exe_dir / "release_manifest.json")
    try:
        paths.append(Path(settings.root) / "release_manifest.json")
        paths.append(Path(settings.data_dir) / "release_manifest.json")
    except Exception:
        pass
    return paths


def release_payload(settings: Any) -> dict[str, Any]:
    manifest_path = next((p for p in _candidate_manifests(settings) if p.is_file()), None)
    if not manifest_path:
        return {
            "available": False,
            "manifest_path": "",
            "source_hash": "",
            "git_commit": "",
            "code_signing": "unsigned",
            "verified": "unknown",
            "detail": "未找到 release_manifest.json（开发模式或旧构建）。",
        }
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "available": False,
            "manifest_path": str(manifest_path),
            "source_hash": "",
            "git_commit": "",
            "code_signing": "unknown",
            "verified": "unknown",
            "detail": f"清单读取失败：{exc}",
        }
    backend = (manifest.get("artifacts") or {}).get("backend_exe") or {}
    expected = str(backend.get("sha256") or "")
    actual = ""
    verified = "unknown"
    try:
        current = Path(sys.executable) if getattr(sys, "frozen", False) else None
        if current and current.is_file():
            actual = sha256_file(current)
            if expected:
                verified = "ok" if actual == expected else "mismatch"
    except Exception as exc:
        verified = f"error: {exc}"
    return {
        "available": True,
        "manifest_path": str(manifest_path),
        "generated_at": manifest.get("generated_at", ""),
        "source_hash": manifest.get("source_hash", ""),
        "git_commit": manifest.get("git_commit", ""),
        "git_dirty": bool(manifest.get("git_dirty")),
        "code_signing": (manifest.get("code_signing") or {}).get("status", "unsigned"),
        "verified": verified,
        "backend_sha256": actual,
        "detail": "哈希一致" if verified == "ok" else (
            "后端 exe 与清单不一致；可能被替换或清单不是本次构建。" if verified == "mismatch"
            else "未安装/开发模式，无法比较当前 exe。"
        ),
    }
