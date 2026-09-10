from __future__ import annotations

import pytest

from backend.app.arbiter import Arbiter
from backend.app.rules import RULESET_ID, START_FEN

SHUFFLE = ["b0c2", "b9c7", "c2b0", "c7b9"] * 2
PERPETUAL_CHECK_FEN = "3k5/9/9/9/9/9/9/9/3R5/5K3 b - - 0 1"
PERPETUAL_CHECK = ["d9e9", "d1e1", "e9d9", "e1d1"] * 2
CHASE_FEN = "r3k4/9/9/9/4P4/9/9/1N7/9/4K4 b - - 0 1"
PERPETUAL_CHASE = ["a9b9", "b2a4", "b9a9", "a4b2"] * 2
MATE_FEN = "3k5/4R4/3R5/9/9/9/9/9/9/4K4 b - - 0 1"
STALEMATE_FEN = "3k5/R8/9/9/9/9/9/9/4R4/4K4 b - - 0 1"
NATURAL_LIMIT_FEN = "3k5/9/9/9/4P4/9/9/9/3R5/5K3 b - - 119 60"
MATE_IN_ONE_FEN = "3k5/R8/9/9/9/4P4/9/9/R3C4/5K3 w - - 0 1"


@pytest.fixture
def arbiter():
    return Arbiter()


def test_native_rules_are_the_authority(arbiter):
    assert arbiter.backend == RULESET_ID
    assert arbiter.describe()["dependency"] is None
    verdict = arbiter.inspect(START_FEN, [])
    assert not verdict.ended and len(verdict.legal_moves) == 44
    assert verdict.legal_moves == sorted(verdict.legal_moves)


def test_same_fen_different_history_is_adjudicated(arbiter):
    fresh = arbiter.inspect(START_FEN, [])
    repeated = arbiter.inspect(START_FEN, SHUFFLE)
    assert fresh.fen.split()[:4] == repeated.fen.split()[:4]
    assert not fresh.ended
    assert (repeated.ended, repeated.winner, repeated.reason) == (True, None, "repetition_draw")


def test_perpetual_check_loses_for_checker(arbiter):
    verdict = arbiter.inspect(PERPETUAL_CHECK_FEN, PERPETUAL_CHECK)
    assert (verdict.ended, verdict.winner, verdict.reason) == (True, "black", "perpetual_check")


def test_perpetual_chase_loses_for_chaser(arbiter):
    verdict = arbiter.inspect(CHASE_FEN, PERPETUAL_CHASE)
    assert (verdict.ended, verdict.winner, verdict.reason) == (True, "red", "perpetual_chase")


def test_checkmate_and_stalemate(arbiter):
    assert (arbiter.inspect(MATE_FEN, []).winner, arbiter.inspect(MATE_FEN, []).reason) == ("red", "checkmate")
    assert (arbiter.inspect(STALEMATE_FEN, []).winner, arbiter.inspect(STALEMATE_FEN, []).reason) == ("red", "stalemate")


def test_natural_limit_and_insufficient_material(arbiter):
    natural = arbiter.inspect(NATURAL_LIMIT_FEN, ["d9e9"])
    assert (natural.ended, natural.winner, natural.reason) == (True, None, "natural_limit_draw")
    material = arbiter.inspect("3k5/9/9/9/4B4/9/9/9/9/4K4 w - - 0 1", [])
    assert (material.ended, material.winner, material.reason) == (True, None, "insufficient_material")


def test_mate_in_one_and_bad_input(arbiter):
    verdict = arbiter.inspect(MATE_IN_ONE_FEN, ["a1d1"])
    assert (verdict.winner, verdict.reason) == ("red", "checkmate")
    with pytest.raises(ValueError):
        arbiter.inspect(START_FEN, ["a0a9"])
    with pytest.raises(ValueError):
        arbiter.inspect("not a fen", [])
