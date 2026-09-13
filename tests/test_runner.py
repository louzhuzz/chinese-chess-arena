import asyncio
from pathlib import Path

import yaml

from backend.app.arbiter import Arbiter
from backend.app.rules import RULESET_ID
from backend.app.config import ConfigStore
from backend.app.db import Database
from backend.app.runner import EventBus, GameRunner, first_attempt_budget
from backend.app.schemas import DEFAULT_MOVE_TIMEOUT, GameCreate


def make_config(path: Path):
    path.write_text(yaml.safe_dump({"connections":[
        {"id":"first","name":"First","protocol":"mock","base_url":"first"},
        {"id":"last","name":"Last","protocol":"mock","base_url":"last"}],
        "presets":[
        {"id":"a","name":"A","connection_id":"first","model":"mock"},
        {"id":"b","name":"B","connection_id":"last","model":"mock"}]}),encoding="utf-8")


def make_runner(tmp_path, arbiter=None):
    cfg_path=tmp_path/"models.yaml"; make_config(cfg_path)
    return GameRunner(Database(tmp_path/"test.db"),ConfigStore(cfg_path),EventBus(),arbiter=arbiter)


def test_move_timeout_defaults_and_first_attempt_budget():
    """默认每步时限放宽到 10 分钟；首答只给纠错留固定 30 秒余量。"""
    assert DEFAULT_MOVE_TIMEOUT == 600
    assert GameCreate(red_preset_id="a",black_preset_id="b").move_timeout_seconds == 600
    # 小预算保持旧行为：120 秒仍按 80% 给首答 96 秒。
    assert first_attempt_budget(120) == 96
    assert first_attempt_budget(5) == 4
    # 大预算不再按比例砍：600 秒首答可用 570 秒，比旧的 96 秒宽 6 倍。
    assert first_attempt_budget(600) == 570
    assert first_attempt_budget(1800) == 1770


def test_mock_models_complete_truncated_game(tmp_path):
    async def scenario():
        runner=make_runner(tmp_path)
        try:
            game_id=runner.create(GameCreate(red_preset_id="a",black_preset_id="b",max_plies=4,move_timeout_seconds=5))
            runner.start(game_id); await runner.tasks[game_id]
            game=runner.db.game(game_id)
            assert game["status"]=="truncated" and len(game["history"])==4
            assert game["ruleset_id"]==runner.arbiter.backend
            attempts=runner.db.all("SELECT * FROM moves WHERE game_id=?",(game_id,))
            assert len(attempts)==4 and all(not row["error"] for row in attempts)
        finally: runner.arbiter.close()
    asyncio.run(scenario())


def test_restart_marks_running_game_interrupted(tmp_path):
    db_path=tmp_path/"test.db"; db=Database(db_path)
    db.execute("""INSERT INTO games(id,status,red_preset,black_preset,initial_fen,current_fen,
        history_json,winner,reason,move_timeout,max_plies,ruleset_id,created_at,updated_at,benchmark_id)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",("g","running","a","b","fen","fen","[]",None,None,120,400,"rules","x","x",None))
    Database(db_path)
    assert db.game("g")["status"]=="interrupted"


def test_invalid_first_reply_is_corrected(tmp_path):
    async def scenario():
        cfg_path=tmp_path/"models.yaml"
        cfg_path.write_text(yaml.safe_dump({"connections":[{"id":"bad","name":"Bad","protocol":"mock","base_url":"invalid_once"}],"presets":[{"id":"p","name":"P","connection_id":"bad","model":"mock"}]}),encoding="utf-8")
        runner=GameRunner(Database(tmp_path/"test.db"),ConfigStore(cfg_path),EventBus(),arbiter=Arbiter(executable=tmp_path/"missing.exe"))
        game_id=runner.create(GameCreate(red_preset_id="p",black_preset_id="p",max_plies=1,move_timeout_seconds=5))
        runner.start(game_id); await runner.tasks[game_id]
        attempts=runner.db.all("SELECT attempt,error,move FROM moves WHERE game_id=? ORDER BY attempt",(game_id,))
        assert attempts[0]["error"] and attempts[1]["move"]
        assert runner.db.game(game_id)["status"]=="truncated"
    asyncio.run(scenario())


def test_game_uses_native_ruleset(tmp_path):
    async def scenario():
        runner=make_runner(tmp_path)
        try:
            game_id=runner.create(GameCreate(red_preset_id="a",black_preset_id="b",max_plies=1,move_timeout_seconds=5))
            assert runner.db.game(game_id)["ruleset_id"]==RULESET_ID
        finally: runner.arbiter.close()
    asyncio.run(scenario())


def test_event_bus_stream_delivers_and_cleans_up():
    async def scenario():
        bus=EventBus(); received=[]
        async def consume():
            async for event in bus.stream("g"):
                received.append(event)
                if len(received)==2: return
        task=asyncio.create_task(consume())
        await asyncio.sleep(0)
        await bus.publish("g",{"type":"a"}); await bus.publish("g",{"type":"b"})
        await asyncio.wait_for(task,5)
        assert received==[{"type":"a"},{"type":"b"}]
    asyncio.run(scenario())


def test_runtime_arbiter_failure_aborts_instead_of_changing_rules(tmp_path):
    async def scenario():
        arbiter=Arbiter(executable=tmp_path/"missing.exe")
        original=arbiter.inspect; calls=0
        def fail_after_first(fen,history):
            nonlocal calls
            calls+=1
            if calls>1: raise RuntimeError("simulated rules crash")
            return original(fen,history)
        arbiter.inspect=fail_after_first
        runner=make_runner(tmp_path,arbiter=arbiter)
        game_id=runner.create(GameCreate(red_preset_id="a",black_preset_id="b",max_plies=4,move_timeout_seconds=5))
        runner.start(game_id); await runner.tasks[game_id]
        game=runner.db.game(game_id)
        assert game["status"]=="aborted" and game["reason"].startswith("arbiter_failure")
        assert len(game["history"])==1
    asyncio.run(scenario())


def test_latest_game_rook_blockage_is_explained():
    from backend.app.rules import parse_fen, legal_moves
    state = parse_fen("1rb1kabr1/4a4/7c1/p7p/4P4/P5P2/8P/9/3NC2R1/R1BAKAB2 b - - 0 22")
    assert "h9h1" not in legal_moves(state)
    assert "h7" in GameRunner._illegal_move_error("h9h1", state)


def test_long_game_requests_remain_bounded(tmp_path):
    import json
    async def scenario():
        runner = make_runner(tmp_path)
        from backend.app.schemas import ModelReply
        import random
        rng = random.Random(42)
        async def choose(preset, position, timeout, metrics, **kwargs):
            assert kwargs["context"] == []
            return ModelReply(text=json.dumps({"move": rng.choice(position["legal_moves"]), "note": "plan" * 40}), raw={})
        runner.client.choose = choose
        game_id = runner.create(GameCreate(red_preset_id="a", black_preset_id="b", max_plies=12, move_timeout_seconds=5))
        try:
            runner.start(game_id)
            await runner.tasks[game_id]
            rows = runner.db.all("SELECT prompt_json FROM moves WHERE game_id=? ORDER BY ply,attempt", (game_id,))
            assert len(rows) == 12
            for index, row in enumerate(rows):
                prompt = json.loads(row["prompt_json"])
                assert len(prompt["history"]) == min(index, 8)
                assert prompt["history_start_ply"] == max(0, index-8)
                assert len(prompt["private_memory"]) <= 1
                assert "board_rows" not in prompt and "fen" not in prompt
                assert prompt["player_side"] == prompt["side_to_move"]
                assert 0 < prompt["request_timeout_seconds"] < prompt["remaining_timeout_seconds"] <= 5
                if index:
                    assert prompt["last_move"]["move"] == prompt["history"][-1]
        finally:
            runner.arbiter.close()
    asyncio.run(scenario())
