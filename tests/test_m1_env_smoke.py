"""M1 环境烟雾测试。

用途：后续子代理接手时，可先跑 `pytest -q` 快速确认项目内 .venv 完好。
覆盖：Python 版本、核心依赖可导入、SQLite FTS5 可用、pytest-asyncio 自动模式。
"""

import importlib
import sqlite3
import sys

import pytest

# M1 核心依赖的「导入名」（注意：python-multipart 新导入名是 python_multipart，
# 旧名 multipart 仍兼容但会告警；python-dotenv 是 dotenv，Pillow 是 PIL，pyyaml 是 yaml）。
CORE_MODULES = [
    "openai",
    "httpx",
    "tenacity",
    "pydantic",
    "pydantic_settings",
    "dotenv",
    "orjson",
    "jsonschema",
    "yaml",
    "numpy",
    "typer",
    "rich",
    "fastapi",
    "uvicorn",
    "jinja2",
    "python_multipart",
    "websockets",
    "jieba",
    "PIL",
    "tiktoken",
    "pytest",
    "pytest_asyncio",
]


def test_python_is_312():
    assert sys.version_info[:2] == (3, 12), sys.version


@pytest.mark.parametrize("module_name", CORE_MODULES)
def test_core_module_importable(module_name):
    assert importlib.import_module(module_name) is not None


def test_sqlite_fts5_available():
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        con.execute("INSERT INTO t(x) VALUES ('pocketmind fts5 ok')")
        hits = con.execute("SELECT x FROM t WHERE t MATCH 'pocketmind'").fetchall()
    finally:
        con.close()
    assert hits == [("pocketmind fts5 ok",)]


async def test_asyncio_mode_auto():
    # 没有 @pytest.mark.asyncio 也能收集，说明 asyncio_mode=auto 生效。
    assert True
