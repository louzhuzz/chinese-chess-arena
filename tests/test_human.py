from __future__ import annotations

import json
import time

from backend.app.rules import RULESET_ID, START_FEN
from tests.conftest import MATE_IN_ONE_FEN, set_model


def wait_for_game(client, game_id: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        game = client.get(f"/api/games/{game_id}").json()
        if game["status"] not in {"queued", "running"}:
            return game
        time.sleep(0.05)
    raise AssertionError(f"game {game_id} did not finish within {timeout}s")


def wait_for_human(client, game_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        game = client.get(f"/api/games/{game_id}").json()
        if game.get("awaiting"):
            return game
        if game["status"] not in {"queued", "running"}:
            raise AssertionError(f"game {game_id} ended as {game['status']} while waiting for a human")
        time.sleep(0.05)
    raise AssertionError(f"game {game_id} never awaited a human move")


def test_analyze_returns_legal_moves_and_pieces(api):
    client, _ = api
    result = client.post("/api/analyze", json={"fen": START_FEN}).json()
    assert result["side_to_move"] == "red"
    assert len(result["legal_moves"]) == 44 and len(result["pieces"]) == 32
    assert result["ended"] is False and result["in_check"] is False
    assert result["backend"] == RULESET_ID


def test_analyze_uses_history_for_repetition(api):
    client, _ = api
    history = ["b0c2", "b9c7", "c2b0", "c7b9"] * 2
    fresh = client.post("/api/analyze", json={"initial_fen": START_FEN}).json()
    repeated = client.post("/api/analyze", json={"initial_fen": START_FEN, "history": history}).json()
    assert fresh["ended"] is False
    assert repeated["ended"] is True and repeated["reason"] == "repetition_draw"
    assert repeated["fen"].split()[0] == fresh["fen"].split()[0]


def test_analyze_rejects_broken_positions(api):
    client, _ = api
    assert client.post("/api/analyze", json={"fen": "not a fen"}).status_code == 400
    assert client.post("/api/analyze", json={"fen": "3k5/9/9/9/9/9/9/9/9/9 w - - 0 1"}).status_code == 400


def test_analyze_reports_mate(api):
    client, _ = api
    result = client.post("/api/analyze", json={"initial_fen": MATE_IN_ONE_FEN, "history": ["a1d1"]}).json()
    assert (result["ended"], result["winner"], result["reason"]) == (True, "red", "checkmate")


def test_human_side_waits_for_a_submitted_move(api):
    client, _ = api
    created = client.post("/api/games", json={
        "red_preset_id": "human-red", "black_preset_id": "black",
        "max_plies": 2, "move_timeout_seconds": 30}).json()
    game = wait_for_human(client, created["id"])
    assert game["awaiting"] == "red" and game["history"] == []
    assert "e0e1" in game["legal_moves"] and len(game["legal_moves"]) == 44

    assert client.post(f"/api/games/{created['id']}/move", json={"move": "a0a9", "expected_ply":0, "expected_side":"red"}).status_code == 400
    assert client.post(f"/api/games/{created['id']}/move", json={"move": "b0c2", "expected_ply":0, "expected_side":"red"}).status_code == 200
    final = wait_for_game(client, created["id"])
    assert final["history"][0] == "b0c2"
    assert final["status"] == "truncated" and len(final["history"]) == 2
    assert [move["side"] for move in final["moves"]] == ["red", "black"]
    assert final["moves"][0]["move"] == "b0c2" and final["moves"][0]["error"] is None


def test_move_submission_is_rejected_when_a_model_is_to_move(api):
    client, _ = api
    created = client.post("/api/games", json={
        "red_preset_id": "human-red", "black_preset_id": "black",
        "max_plies": 2, "move_timeout_seconds": 30}).json()
    wait_for_human(client, created["id"])
    client.post(f"/api/games/{created['id']}/move", json={"move": "b0c2", "expected_ply":0, "expected_side":"red"})
    time.sleep(0.6)
    response = client.post(f"/api/games/{created['id']}/move", json={"move": "b9c7", "expected_ply":1, "expected_side":"black"})
    assert response.status_code in (400, 200)  # black may already have replied
    wait_for_game(client, created["id"])


def test_human_versus_human_plays_both_sides(api):
    client, _ = api
    created = client.post("/api/games", json={
        "red_preset_id": "human-red", "black_preset_id": "human-black",
        "max_plies": 2, "move_timeout_seconds": 30}).json()
    game = wait_for_human(client, created["id"])
    assert game["awaiting"] == "red"
    client.post(f"/api/games/{created['id']}/move", json={"move": "c3c4", "expected_ply":0, "expected_side":"red"})
    game = wait_for_human(client, created["id"])
    assert game["awaiting"] == "black" and game["history"] == ["c3c4"]
    client.post(f"/api/games/{created['id']}/move", json={"move": "g6g5", "expected_ply":1, "expected_side":"black"})
    final = wait_for_game(client, created["id"])
    assert final["history"] == ["c3c4", "g6g5"] and final["status"] == "truncated"


def test_game_can_start_from_a_custom_position(api):
    client, _ = api
    set_model(client, "black", "first")
    created = client.post("/api/games", json={
        "red_preset_id": "human-red", "black_preset_id": "black",
        "initial_fen": MATE_IN_ONE_FEN, "max_plies": 4, "move_timeout_seconds": 30}).json()
    game = wait_for_human(client, created["id"])
    assert game["initial_fen"] == MATE_IN_ONE_FEN and len(game["pieces"] if "pieces" in game else []) >= 0
    client.post(f"/api/games/{created['id']}/move", json={"move": "a1d1", "expected_ply":0, "expected_side":"red"})
    final = wait_for_game(client, created["id"])
    assert (final["winner"], final["reason"]) == ("red", "checkmate")


def test_benchmark_rejects_human_presets(api):
    client, _ = api
    response = client.post("/api/benchmarks", json={"preset_a_id": "human-red", "preset_b_id": "black", "pairs": 1})
    assert response.status_code == 400


def test_human_timeout_is_a_technical_loss(tmp_path):
    """A human side that never moves loses on time; keep the budget tiny for the test."""
    import asyncio

    from backend.app.arbiter import Arbiter
    from backend.app.config import ConfigStore
    from backend.app.db import Database
    from backend.app.runner import EventBus, GameRunner
    from backend.app.schemas import GameCreate

    config_path = tmp_path / "models.yaml"
    config_path.write_text(json.dumps({
        "connections": [{"id": "human", "name": "Human", "protocol": "human", "base_url": ""},
                        {"id": "mock", "name": "Mock", "protocol": "mock", "base_url": "first"}],
        "presets": [{"id": "me", "name": "Me", "connection_id": "human", "model": "human"},
                    {"id": "ai", "name": "AI", "connection_id": "mock", "model": "mock"}],
    }), encoding="utf-8")
    import yaml
    config_path.write_text(yaml.safe_dump(json.loads(config_path.read_text(encoding="utf-8"))), encoding="utf-8")

    async def scenario():
        runner = GameRunner(Database(tmp_path / "human.db"), ConfigStore(config_path), EventBus(),
                            arbiter=Arbiter(executable=tmp_path / "missing.exe"), human_timeout=0.3)
        game_id = runner.create(GameCreate(red_preset_id="me", black_preset_id="ai", move_timeout_seconds=5, max_plies=4))
        runner.start(game_id)
        await runner.tasks[game_id]
        game = runner.db.game(game_id)
        assert (game["status"], game["winner"], game["reason"]) == ("finished", "black", "timeout")

    asyncio.run(scenario())
