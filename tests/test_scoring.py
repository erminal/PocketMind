from __future__ import annotations

import pytest

from pocketmind.config import ScoringConfig
from pocketmind.models import RelationshipState
from pocketmind.scoring import (
    affect_match,
    base_score,
    combine_scores,
    cosine,
    half_life_for,
    recall_reward,
    retention,
    update_relationship,
)


def test_cosine_basic() -> None:
    score = cosine([1.0, 0.0, 1.0], [0.8, 0.2, 0.9])
    assert score == pytest.approx(0.984, abs=0.002)
    assert cosine([], [1.0]) == 0.0
    assert cosine([0.0], [1.0]) == 0.0


def test_retention_and_half_life() -> None:
    assert retention(30, 30) == pytest.approx(0.5)
    assert retention(7, 14) == pytest.approx(0.707, abs=0.002)
    assert retention(0, 14) == 1.0


def test_base_score_and_affect() -> None:
    cfg = ScoringConfig()
    assert base_score(0.8, 2, cfg) == pytest.approx(4.8)
    assert base_score(-0.9, 3, cfg) == pytest.approx(-9.0)
    assert affect_match(0.8, 0.2) == pytest.approx(0.7)
    assert affect_match(-1.0, 1.0) == 0.0


def test_combine_scores_weights() -> None:
    cfg = ScoringConfig()
    # 全部为 1 时，综合分应为 1
    assert combine_scores(1, 1, 1, 1, 1, 1, cfg) == pytest.approx(1.0)
    # 语义为 0、其它为 0 → 0
    assert combine_scores(0, 0, 0, 0, 0, 0, cfg) == 0.0


def test_half_life_by_category() -> None:
    cfg = ScoringConfig()
    core = half_life_for("FACT", 0, 1, 0.8, True, cfg)
    routine = half_life_for("OTHER", 0, 1, 0.05, False, cfg)
    assert core > routine
    assert core >= cfg.half_life_anchor


def test_relationship_update() -> None:
    cfg = ScoringConfig()
    state = RelationshipState(entity_id="e")
    new_state, changes = update_relationship(state, base=4.8, importance=0.7, cfg=cfg)
    assert new_state.favorability > state.favorability
    assert any(c["field"] == "favorability" for c in changes)
    assert -100 <= new_state.favorability <= 100


def test_recall_reward_decays() -> None:
    cfg = ScoringConfig()
    assert recall_reward(10, 0, cfg) > recall_reward(10, 5, cfg)
