from __future__ import annotations

import pytest

from pocketmind.config import Settings
from pocketmind.pipeline import ChatEngine


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data", mock=True)


@pytest.fixture()
def engine(settings: Settings) -> ChatEngine:
    return ChatEngine(settings)
