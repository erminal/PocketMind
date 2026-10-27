from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from pocketmind.config import Settings
from pocketmind.web import create_app


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(Settings(data_dir=tmp_path / "data", mock=True)))


def test_main_nav_uses_buttons_and_chat_is_inner_scroll(tmp_path: Path) -> None:
    page = _client(tmp_path).get("/").text
    assert 'class="navbtn active"' in page
    assert "location.href='/space'" in page
    assert "location.href='/admin'" in page
    assert "location.href='/review'" in page
    # C1：手动写日记按钮已移除，只保留删除
    assert "gen-diary" not in page
    assert "del-diary" in page
    # 固定视口 + 聊天内窗滚动 + 输入框贴底的关键 CSS
    assert "height:100vh" in page
    assert "min-height:0" in page
    assert "overflow-y:auto" in page


def test_admin_nav_uses_buttons(tmp_path: Path) -> None:
    page = _client(tmp_path).get("/admin").text
    assert 'class="quick"' in page
    assert "location.href='/'" in page
    assert "location.href='/space'" in page
