"""Generate release_manifest.json for a desktop build.

Usage:
  python desktop/make_release_manifest.py --root . --backend-exe dist/PocketMind/PocketMind.exe \
      --desktop-exe dist/PocketMindDesktop/PocketMind.exe --out dist/PocketMindDesktop/release_manifest.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pocketmind.release_security import build_manifest, write_manifest  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--backend-exe", default="")
    parser.add_argument("--desktop-exe", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    backend = (root / args.backend_exe).resolve() if args.backend_exe else None
    desktop = (root / args.desktop_exe).resolve() if args.desktop_exe else None
    out = Path(args.out).resolve() if args.out else root / "release_manifest.json"
    manifest = build_manifest(root, backend, desktop)
    write_manifest(out, manifest)
    print(f"manifest: {out}")
    print(f"source:   {manifest['source_hash']}")
    print(f"commit:   {manifest['git_commit']} dirty={manifest['git_dirty']}")
    for name, info in manifest.get("artifacts", {}).items():
        print(f"{name}: {info['sha256']}")


if __name__ == "__main__":
    main()
