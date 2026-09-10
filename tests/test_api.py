from __future__ import annotations

import json
import time

from backend.app.rules import RULESET_ID, START_FEN
from backend.app.schemas import GameCreate
from tests.conftest import MATE_IN_ONE_FEN, set_model


def wait_for_game(client, game_id: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        game = client.get(f"/api/games/{game_id}").json()
        if game["status"] not in {"queued", "running"}:
            return game
        time.sleep(0.05)
    raise AssertionError(f"game {game_id} did not finish within {timeout}s")


def start_game(client, **overrides) -> str:
    payload = {"red_preset_id": "red", "black_preset_id": "black",
               "move_timeout_seconds": 30, "max_plies": 8}
    payload.update(overrides)
    response = client.post("/api/games", json=payload)
    assert response.status_code == 202, response.text
    return response.json()["id"]


def test_health_and_config_hide_keys(api):
    client, main = api
    health = client.get("/api/health").json()
    assert health["status"] == "ok"
    assert health["arbiter"]["backend"] == RULESET_ID
    assert health["arbiter"]["dependency"] is None
    config = client.get("/api/config").json()
    openai = next(c for c in config["connections"] if c["id"] == "openai")
    assert "api_key" not in openai
    assert openai["api_key_masked"].startswith("••••")
    assert "secret" not in str(config)
    assert openai["extra_headers"]["X-API-Token"].startswith("••••")
    assert openai["extra_headers"]["X-Region"] == "local"


def test_connection_test_endpoint(api):
    client, _ = api
    for connection_id in ("openai", "claude"):
        result = client.post(f"/api/connections/{connection_id}/test").json()
        assert result["ok"] is True, result


def test_provider_can_discover_and_sync_models(api):
    client, _ = api
    result = client.post("/api/connections/openai/sync-models")
    assert result.status_code == 200, result.text
    assert any(model["model"] == "fake-model" for model in result.json()["models"])
    presets = client.get("/api/config").json()["presets"]
    assert any(p["connection_id"] == "openai" and p["model"] == "fake-model" for p in presets)


def test_openai_chat_and_anthropic_games_reach_a_rule_result(api):
    client, _ = api
    set_model(client, "red", "prefer:a1d1")
    set_model(client, "black", "first")
    game = wait_for_game(client, start_game(client, initial_fen=MATE_IN_ONE_FEN))
    assert game["status"] == "finished"
    assert (game["winner"], game["reason"]) == ("red", "checkmate")
    assert game["ruleset_id"] == RULESET_ID
    assert game["history"] == ["a1d1"]
    move = game["moves"][0]
    assert move["attempt"] == 1 and move["error"] is None
    assert move["notation"] == "后车平六"
    assert move["input_tokens"] == 11 and move["output_tokens"] == 3
    assert move["total_input_tokens"] == 11
    assert move["cache_read_tokens"] == 0 and move["cache_miss_tokens"] == 11
    assert move["connect_ms"] is not None and move["connect_ms"] >= 0
    assert move["first_byte_ms"] is not None and move["first_byte_ms"] >= move["connect_ms"]
    assert move["provider_request_id"] == "fake-provider-request-123"
    assert move["fen_after"]


def test_game_reasoning_effort_is_frozen_and_sent(api):
    client, _ = api
    set_model(client, "red", "prefer:a1d1")
    game = wait_for_game(client, start_game(client, initial_fen=MATE_IN_ONE_FEN,
                         red_reasoning_effort="max"))
    frozen = json.loads(game["red_config_json"])
    assert frozen["thinking"] == "enabled" and frozen["reasoning_effort"] == "max"
    assert game["moves"][0]["raw"]["request_echo"] == {
        "thinking": {"type": "enabled"}, "reasoning_effort": "max"}


def test_game_records_usage_and_truncates_at_the_ply_limit(api):
    client, _ = api
    game = wait_for_game(client, start_game(client, max_plies=6))
    assert game["status"] == "truncated" and game["reason"] == "max_plies"
    assert len(game["history"]) == 6
    assert all(move["input_tokens"] == 11 for move in game["moves"])
    assert all(move["duration_ms"] is not None for move in game["moves"])


def test_illegal_answer_is_corrected_once(api):
    client, _ = api
    set_model(client, "red", "illegal_once")
    set_model(client, "black", "first")
    game = wait_for_game(client, start_game(client, max_plies=2))
    attempts = [(move["ply"], move["attempt"], bool(move["error"])) for move in game["moves"]]
    assert attempts == [(0, 1, True), (0, 2, False), (1, 1, False)]
    assert len(game["history"]) == 2


def test_persistent_illegal_answer_is_a_technical_loss(api):
    client, _ = api
    set_model(client, "red", "illegal")
    game = wait_for_game(client, start_game(client, max_plies=6))
    assert game["status"] == "finished"
    assert (game["winner"], game["reason"]) == ("black", "invalid_move")
    assert len(game["moves"]) == 2 and all(move["error"] for move in game["moves"])


def test_stop_marks_the_game_stopped(api):
    client, _ = api
    game_id = start_game(client, max_plies=400)
    assert client.post(f"/api/games/{game_id}/stop").status_code == 200
    game = wait_for_game(client, game_id)
    assert game["status"] == "stopped" and game["reason"] == "user_stopped"


def test_benchmark_swaps_colours_and_reports_usage(api):
    client, _ = api
    set_model(client, "red", "prefer:a1d1")
    set_model(client, "black", "prefer:a1d1")
    created = client.post("/api/benchmarks", json={
        "preset_a_id": "red", "preset_b_id": "black", "pairs": 1,
        "initial_fen": MATE_IN_ONE_FEN, "max_plies": 4, "move_timeout_seconds": 30,
        "preset_a_reasoning_effort": "max", "preset_b_reasoning_effort": "low"}).json()
    deadline = time.monotonic() + 90
    report = {}
    while time.monotonic() < deadline:
        report = client.get(f"/api/benchmarks/{created['id']}").json()
        if report["status"] not in {"queued", "running"}:
            break
        time.sleep(0.1)
    assert report["status"] == "finished"
    assert report["stats"]["games"] == 2 and report["stats"]["decided"] == 2
    assert report["stats"]["red_wins"] == 2 and report["stats"]["draws"] == 0
    assert report["model_results"]["red"]["score_rate"] == 0.5
    assert report["model_results"]["black"]["score_rate"] == 0.5
    assert {game["reason"] for game in report["games"]} == {"checkmate"}
    assert [game["red_preset"] for game in report["games"]] == ["red", "black"]
    for game in report["games"]:
        red_config=json.loads(game["red_config_json"]); black_config=json.loads(game["black_config_json"])
        assert red_config["reasoning_effort"] == ("max" if game["red_preset"]=="red" else "low")
        assert black_config["reasoning_effort"] == ("max" if game["black_preset"]=="red" else "low")
    assert report["settings"]["pairs"] == 1
    usage = {item["preset_id"]: item for item in report["usage"]}
    assert set(usage) == {"red", "black"}
    assert usage["red"]["attempts"] > 0 and usage["red"]["input_tokens"] > 0
    assert usage["red"]["cost_estimate"] is None
    csv_response=client.get(f"/api/benchmarks/{created['id']}/export.csv")
    assert csv_response.status_code == 200
    assert "model_summary" in csv_response.text and "score_rate" in csv_response.text
    exported=client.get(f"/api/games/{report['games'][0]['id']}/export.json").json()
    assert exported["prompt_version"] and isinstance(exported["moves"][0]["prompt"],dict)
    assert isinstance(exported["moves"][0]["raw"],dict)
    assert exported["moves"][0]["notation"] == "后车平六"


def test_truncated_answer_is_retried_and_explained(api, request_log):
    """推理模型把预算花在思考上时，正文为空；纠错重试应继续对局并留下原因。"""
    client, _ = api
    set_model(client, "red", "truncate_once")
    game = wait_for_game(client, start_game(client, max_plies=2))
    attempts = [(move["ply"], move["attempt"], bool(move["error"])) for move in game["moves"]]
    assert attempts == [(0, 1, True), (0, 2, False), (1, 1, False)]
    assert "max_tokens" in game["moves"][0]["error"]
    corrected = game["moves"][1]
    assert corrected["actual_request"]["max_tokens"] <= 512
    assert corrected["prompt"]["output_token_limit"] == corrected["actual_request"]["max_tokens"]
    assert corrected["actual_request"]["thinking"] == {"type": "disabled"}
    assert "max_tokens" in corrected["prompt"]["correction"]["error"]
    assert game["context_messages"]["red"]["count"] == 2

    assert game["history"][0] and len(game["history"]) == 2


def test_missing_api_key_is_an_interface_failure(api):
    """没有密钥属于接口故障，不能算某一方的技术负。"""
    client, _ = api
    presets = client.get("/api/config").json()["presets"]
    preset = next(p for p in presets if p["id"] == "red")
    preset["connection_id"] = "no-key"
    connection = {"id": "no-key", "name": "No key", "protocol": "openai_chat",
                  "base_url": "http://127.0.0.1:1/v1", "api_key_env": "XIANGQI_MISSING_KEY"}
    assert client.put("/api/connections/no-key", json=connection).status_code == 200
    assert client.put("/api/presets/red", json=preset).status_code == 200
    game = wait_for_game(client, start_game(client, max_plies=4, move_timeout_seconds=20))
    assert (game["status"], game["winner"], game["reason"]) == ("finished", None, "api_failure")
    assert "No API key" in game["moves"][0]["error"]


def test_unknown_preset_is_rejected(api):
    client, _ = api
    response = client.post("/api/games", json={"red_preset_id": "nope", "black_preset_id": "black"})
    assert response.status_code == 400


def test_slow_model_times_out_as_a_technical_loss(api):
    client, _ = api
    set_model(client, "red", "slow")
    game = wait_for_game(client, start_game(client, max_plies=4, move_timeout_seconds=5), timeout=30)
    assert (game["status"], game["winner"], game["reason"]) == ("finished", "black", "timeout")
    assert any(move["error"] == "timeout" for move in game["moves"])


def test_upstream_error_is_recorded_as_api_failure(api):
    client, _ = api
    set_model(client, "red", "error500")
    game = wait_for_game(client, start_game(client, max_plies=4, move_timeout_seconds=20), timeout=30)
    assert (game["status"], game["winner"], game["reason"]) == ("finished", None, "api_failure")
    assert all((move["error"] or "").startswith("api_error") for move in game["moves"])


def test_prompt_contract_is_documented_and_isolated(api, request_log):
    client, main = api
    game = wait_for_game(client, start_game(client, max_plies=3))
    rows = main.db.all("SELECT prompt_json FROM moves WHERE game_id=? ORDER BY ply,attempt", (game["id"],))
    prompts = [json.loads(row["prompt_json"]) for row in rows]
    assert len(prompts) == 3
    for prompt in prompts:
        assert set(prompt) <= {"game_id", "ply", "side_to_move", "pieces", "history",
                               "legal_moves", "in_check", "ruleset_id", "move_timeout_seconds",
                               "correction", "private_memory", "player_side", "output_token_limit", "last_move",
                               "history_start_ply", "remaining_timeout_seconds", "request_timeout_seconds"}
        assert "private_memory" in prompt
        assert prompt["move_timeout_seconds"] == 30
        assert "response_text" not in prompt and "raw" not in prompt
        assert len(prompt["pieces"]) == 32
    assert prompts[1]["side_to_move"] == "black"
    assert prompts[1]["history"] == game["history"][:1]
    assert prompts[2]["history"] == game["history"][:2]
    assert prompts[0]["private_memory"] == [] and prompts[1]["private_memory"] == []
    assert prompts[2]["private_memory"] == [{"ply": 0, "move": game["history"][0], "note": "private plan for first"}]
    system = next(body for path, body in request_log if path.endswith("/chat/completions"))["messages"][0]["content"]
    assert "move_timeout_seconds" in system and "不要进行穷尽搜索" in system


def test_model_context_is_current_position_and_side_isolated(api, request_log):
    client, main = api
    set_model(client, "red", "red-context")
    set_model(client, "black", "black-context")
    game = wait_for_game(client, start_game(client, max_plies=4))
    assert game["status"] == "truncated"
    chat_requests = [body for path, body in request_log if path.endswith("/chat/completions")]
    anthropic_requests = [body for path, body in request_log if path.endswith("/v1/messages")]
    assert len(chat_requests) >= 2 and len(anthropic_requests) >= 2
    assert len(chat_requests[1]["messages"]) == len(chat_requests[0]["messages"]) == 2
    assert len(anthropic_requests[1]["messages"]) == len(anthropic_requests[0]["messages"]) == 1
    assert chat_requests[0]["messages"][0] == chat_requests[1]["messages"][0]
    assert anthropic_requests[0]["system"] == anthropic_requests[1]["system"]
    red_context = main.db.all("SELECT role,content FROM context_messages WHERE game_id=? AND side='red' ORDER BY sequence", (game["id"],))
    assert chat_requests[0]["messages"][-1] == red_context[0]
    assert chat_requests[1]["messages"][-1] == red_context[2]
    assert game["moves"][0]["actual_request"]["model"] == "red-context"
    red_text = json.dumps(chat_requests[1], ensure_ascii=False)
    black_text = json.dumps(anthropic_requests[1], ensure_ascii=False)
    assert "private plan for red-context" in red_text
    assert "private plan for red-context" not in black_text


def test_model_context_is_compacted_at_a_bounded_size(api):
    _, main = api
    game_id = main.games.create(GameCreate(red_preset_id="red", black_preset_id="black", max_plies=4))
    game = main.db.game(game_id)
    for index in range(60):
        main.games._append_context(game_id, "red", "user", f"event-{index}", index, 1)
    messages = main.games._context_messages(game_id, "red", game)
    assert len(messages) == 25
    assert messages[0]["content"].startswith("<xiangqi_context_compaction>")
    assert messages[-1]["content"] == "event-59"


def test_game_freezes_preset_parameters_at_creation(api):
    client, _ = api
    created = client.post("/api/games", json={"red_preset_id":"human-red","black_preset_id":"black",
        "max_plies":2,"move_timeout_seconds":30}).json()
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        game=client.get(f"/api/games/{created['id']}").json()
        if game.get("awaiting"): break
        time.sleep(.05)
    set_model(client,"black","illegal")
    response=client.post(f"/api/games/{created['id']}/move",json={"move":"b0c2","expected_ply":0,"expected_side":"red"})
    assert response.status_code==200
    game=wait_for_game(client,created["id"])
    assert game["status"]=="truncated" and len(game["history"])==2
