"""PocketMind 一键启动器（给不使用命令行的日常使用）。

双击 EXE 或 `启动PocketMind.cmd`：
- 自动使用项目内环境（EXE 自带；cmd 用项目 venv）；
- 自动找 8000 起可用端口；
- **等服务器真正能访问后**再打开浏览器（避免“网页无法连接”的竞态）；
- 写日志和地址文件，浏览器打不开时可手动复制；
- 关闭窗口即停止服务。

Edge 提示「现有实例以提升权限运行」不是本项目错误，是 Edge 单实例权限冲突：
点“是(Y)”让 Edge 以普通权限重启即可；或复制窗口里的地址到普通浏览器。

可选：
  python -m pocketmind.launcher --no-browser
  环境变量 POCKETMIND_NO_BROWSER=1 也可跳过自动开浏览器。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

import uvicorn

from .config import Settings
from .web import app


def find_free_port(preferred: int = 8000, attempts: int = 30) -> int:
    for port in range(preferred, preferred + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"从 {preferred} 开始找不到空闲端口")


def _log(path: Path, message: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception:
        pass


def _write_shortcut(data_dir: Path, url: str) -> None:
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "workbench_url.txt").write_text(url + "\n", encoding="utf-8")
        (data_dir / "工作台地址.txt").write_text(url + "\n", encoding="utf-8")
        (data_dir / "工作台.url").write_text(
            "[InternetShortcut]\nURL=" + url + "\n", encoding="utf-8"
        )
    except Exception:
        pass


def _wait_and_open(url: str, log_path: Path, no_browser: bool) -> None:
    health = url + "/health"
    ready = False
    for _ in range(120):  # 最多等 60 秒
        try:
            with urllib.request.urlopen(health, timeout=1) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.5)
    if not ready:
        _log(log_path, f"服务器未在 60 秒内就绪：{health}")
        return
    _log(log_path, f"服务器已就绪：{url}")
    if no_browser:
        _log(log_path, "已按参数跳过自动打开浏览器")
        print(f"\n[提示] 请手动打开：{url}\n")
        return
    try:
        opened = webbrowser.open(url)
        if not opened:
            print(f"\n[提示] 浏览器没有自动打开，请手动访问：{url}\n")
            _log(log_path, "webbrowser.open 返回 False")
    except Exception as exc:
        print(f"\n[提示] 浏览器没有自动打开，请手动访问：{url}\n错误：{exc}\n")
        _log(log_path, f"打开浏览器失败：{exc}")
    print(
        "如果 Edge 弹「现有实例正在以提升的权限运行」，点“是(Y)”重启 Edge；"
        "或复制上面的地址到普通浏览器。\n"
    )


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    settings = Settings()
    log_path = settings.log_dir / "launcher.log"
    port = find_free_port(8000)
    url = f"http://127.0.0.1:{port}"
    desktop = (
        "--desktop" in sys.argv
        or os.getenv("POCKETMIND_DESKTOP", "").lower() in {"1", "true", "yes"}
    )
    no_browser = (
        desktop
        or "--no-browser" in sys.argv
        or os.getenv("POCKETMIND_NO_BROWSER", "").lower() in {"1", "true", "yes"}
    )
    runtime_file = settings.data_dir / "desktop_runtime.json"
    mode = "Mock 模式（不调用真实 API，可离线体验）" if settings.mock else "真实 API 模式"
    _write_shortcut(settings.data_dir, url)
    _log(log_path, f"启动：mode={mode} url={url} data={settings.data_dir} desktop={desktop}")
    print("=" * 62)
    print("  PocketMind · M1 本地实验台")
    print("=" * 62)
    print(f"  模式：{mode}")
    print(f"  地址：{url}")
    print(f"  管理台：{url}/admin")
    print(f"  标注台：{url}/review")
    print(f"  数据：{settings.data_dir}")
    print(f"  地址文件：{settings.data_dir / '工作台地址.txt'}")
    print(f"  日志：{log_path}")
    print("  关闭本窗口即停止服务；Ctrl+C 也可以停止。")
    print("=" * 62)
    if desktop:
        # 桌面壳用 stdout 拿动态端口，避免依赖可能过期的地址文件。
        runtime_file.write_text(
            json.dumps(
                {
                    "url": url,
                    "port": port,
                    "pid": os.getpid(),
                    "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"POCKETMIND_DESKTOP_URL={url}", flush=True)
    if not desktop:
        threading.Thread(
            target=_wait_and_open, args=(url, log_path, no_browser), daemon=True
        ).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
    except KeyboardInterrupt:
        print("\n已停止。")
        _log(log_path, "用户停止（KeyboardInterrupt）")
    except Exception as exc:
        _log(log_path, "启动失败：\n" + traceback.format_exc())
        print(f"\n[错误] 服务启动失败：{exc}")
        print(f"详细日志：{log_path}")
        try:
            input("按回车退出…")
        except Exception:
            pass
    finally:
        if desktop:
            try:
                runtime_file.unlink(missing_ok=True)
            except Exception:
                pass


if __name__ == "__main__":
    main()
