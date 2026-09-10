import asyncio
import json
from pathlib import Path

import pytest

from backend.app.rules import apply_history, to_fen
from backend.app.schemas import GameCreate, ModelReply
from test_runner import make_runner


CASES = json.loads((Path(__file__).parent / "fixtures/flash_budget_positions.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=lambda case: f"step-{case['step']}")
def test_failed_positions_keep_all_facts_without_duplicate_boards(tmp_path, case):
    async def scenario():
        runner = make_runner(tmp_path)
        preset = runner.config.preset("a").model_copy(update={
            "thinking": "enabled", "reasoning_effort": "low", "max_tokens": 16384})
        runner.config.upsert_preset(preset)
        game_id = runner.create(GameCreate(red_preset_id="a", black_preset_id="a",
            max_plies=case["step"], move_timeout_seconds=120))
        state = apply_history(case["initial_fen"], case["history"])
        runner.db.execute("UPDATE games SET history_json=?,current_fen=? WHERE id=?",
            (json.dumps(case["history"]), to_fen(state), game_id))
        captured = []

        async def choose(preset, position, timeout, metrics, **kwargs):
            captured.append(position)
            assert (preset.thinking, preset.reasoning_effort, preset.max_tokens) == ("enabled", "low", 16384)
            return ModelReply(text=json.dumps({"move": case["legal_moves"][0]}), raw={})

        runner.client.choose = choose
        try:
            runner.start(game_id)
            await runner.tasks[game_id]
            assert len(captured) == 1
            prompt = captured[0]
            assert "fen" not in prompt and "board_rows" not in prompt
            assert prompt["pieces"] == case["pieces"]
            assert prompt["legal_moves"] == case["legal_moves"]
            assert prompt["history"] == case["history"][-8:]
            assert prompt["output_token_limit"] == 16384
            row = runner.db.all("SELECT fen_before,fen_after,error FROM moves WHERE game_id=?", (game_id,))[0]
            assert row["fen_before"] == to_fen(state)
            assert row["fen_after"] and row["error"] is None
        finally:
            runner.arbiter.close()

    asyncio.run(scenario())
