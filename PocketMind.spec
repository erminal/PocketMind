# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec：onedir 版（比 onefile 启动快、排错容易）。

用法：
  .\.venv\Scripts\python.exe -m PyInstaller PocketMind.spec --noconfirm --clean
产物：
  dist\PocketMind\PocketMind.exe   （双击启动；data/ 会建在 exe 旁边）
"""

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("fastapi")
    + ["openai", "pocketmind.web", "pocketmind.admin", "pocketmind.launcher"]
)

datas = [
    ("pocketmind/templates", "pocketmind/templates"),
    ("config", "config"),
    (".env.example", "."),
    ("使用说明.md", "."),
]

a = Analysis(
    ["run_exe.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tests", "matplotlib", "pandas", "ipython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PocketMind",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PocketMind",
)
