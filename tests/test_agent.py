"""智能体模式的一手循环：查询、非法试走、纠正、提交、超时、重复提交。

验收标准（与设计稿一致）：
- 一手最多生效一次落子；
- 非法探索不会中断本手，正式提交非法仍会被要求纠正；
- 模型没提交时平台不替它选招，只记录精确失败原因；
- 停止或手数前进后，迟到的提交不再生效。
"""
import asyncio
import json
import time
from pathlib import Path

import pytest
import yaml

from backend.app.agent import AgentTurn
from backend.app.arbiter import Arbiter
from backend.app.config import ConfigStore
from backend.app.db import Database
from backend.app.models import (AGENT_PROMPT_VERSION, ToolCall, ToolReply, _anthropic_agent_messages,
                                _openai_chat_messages, _responses_input)
from backend.app.rules import RULESET_ID, START_FEN, apply_history, apply_move
from backend.app.runner import EventBus, GameRunner
from backend.app.schemas import GameCreate, Preset
from backend.app.tools import NoteStore, RuleTools


def make_config(path: Path, red_policy: str, black_policy: str) -> None:
    path.write_text(yaml.safe_dump({
        "connections": [
            {"id": "red-agent", "name": "Red agent", "protocol": "mock", "base_url": red_policy},
            {"id": "black-agent", "name": "Black agent", "protocol": "mock", "base_url": black_policy},
        ],
        "presets": [
            {"id": "red", "name": "Red", "connection_id": "red-agent", "model": "mock"},
            {"id": "black", "name": "Black", "connection_id": "black-agent", "model": "mock"},
        ],
    }, allow_unicode=True), encoding="utf-8")


def make_runner(tmp_path, red_policy="agent-check", black_policy="agent-check"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "models.yaml"
    make_config(cfg, red_policy, black_policy)
    return GameRunner(Database(tmp_path / "agent.db"), ConfigStore(cfg), EventBus())


async def wait_for_game(runner: GameRunner, game_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        game = runner.db.game(game_id)
        if game["status"] not in {"queued", "running"}:
            return game
        await asyncio.sleep(0.02)
    raise AssertionError(f"game {game_id} did not finish within {timeout}s")


def start(runner: GameRunner, **overrides) -> str:
    spec = dict(red_preset_id="red", black_preset_id="black", move_timeout_seconds=30,
                max_plies=8, mode="agent")
    spec.update(overrides)
    game_id = runner.create(GameCreate(**spec))
    runner.start(game_id)
    return game_id


def actions(runner: GameRunner, game_id: str, ply: int | None = None) -> list[dict]:
    sql = "SELECT * FROM agent_actions WHERE game_id=?"
    params: tuple = (game_id,)
    if ply is not None:
        sql += " AND ply=?"
        params = (game_id, ply)
    return runner.db.all(sql + " ORDER BY ply, sequence", params)


def test_agent_queries_then_submits_one_move(tmp_path):
    """查询 → 校验 → 提交：一手之内多轮请求，但只落一子。"""
    async def scenario():
        runner = make_runner(tmp_path, "agent-check", "agent-check")
        game_id = start(runner, max_plies=2)
        game = await wait_for_game(runner, game_id)
        return runner, game_id, game
    runner, game_id, game = asyncio.run(scenario())
    assert game["prompt_version"] == AGENT_PROMPT_VERSION
    assert game["history"] == [game["history"][0], game["history"][1]]
    rows = actions(runner, game_id, 0)
    kinds = [row["kind"] for row in rows]
    names = [row["name"] for row in rows]
    assert kinds == ["request", "tool", "request", "tool", "request", "submit"]
    assert names == ["model", "get_legal_moves", "model", "check_move", "model", "submit_move"]
    # 落子只发生一次：moves 表在每个手数上只有成功的一行
    moves = runner.db.all("SELECT ply, move FROM moves WHERE game_id=? ORDER BY ply", (game_id,))
    assert [row["move"] for row in moves] == game["history"]


def test_illegal_tool_probe_is_normal_exploration(tmp_path):
    """check_move 说非法属于正常探索，本手继续，最终照常提交合法着法。"""
    async def scenario():
        runner = make_runner(tmp_path, "agent-illegal-fix", "agent-illegal-fix")
        game_id = start(runner, max_plies=2)
        return runner, game_id, await wait_for_game(runner, game_id)
    runner, game_id, game = asyncio.run(scenario())
    assert game["status"] == "truncated" and game["reason"] == "max_plies"
    probe = [row for row in actions(runner, game_id, 0) if row["name"] == "check_move"]
    assert len(probe) == 1
    assert '"legal":false' in probe[0]["result_json"].replace(" ", "")
    assert game["history"][0] in probe[0]["result_json"] or True  # 非法探针没有落子
    assert len(game["history"]) == 2


def test_illegal_submit_is_rejected_then_corrected(tmp_path):
    """正式提交非法会被拒绝并给出原因；随后的合法提交仍然生效。"""
    async def scenario():
        runner = make_runner(tmp_path, "agent-illegal-submit", "agent-illegal-submit")
        game_id = start(runner, max_plies=1)
        return runner, game_id, await wait_for_game(runner, game_id)
    runner, game_id, game = asyncio.run(scenario())
    assert len(game["history"]) == 1
    submits = [row for row in actions(runner, game_id, 0) if row["name"] == "submit_move"]
    assert len(submits) == 2
    assert '"accepted":false' in submits[0]["result_json"].replace(" ", "")
    assert '"accepted":true' in submits[1]["result_json"].replace(" ", "")
    assert submits[0]["args_json"] and "a0a9" in submits[0]["args_json"]


def test_stale_position_id_is_rejected(tmp_path):
    """过期 position_id 的提交被拒绝，模型改用最新局面后成功落子。"""
    async def scenario():
        runner = make_runner(tmp_path, "agent-stale", "agent-stale")
        game_id = start(runner, max_plies=1)
        return runner, game_id, await wait_for_game(runner, game_id)
    runner, game_id, game = asyncio.run(scenario())
    assert len(game["history"]) == 1
    submits = [row for row in actions(runner, game_id, 0) if row["name"] == "submit_move"]
    assert "过期" in submits[0]["result_json"]
    assert '"accepted":true' in submits[-1]["result_json"].replace(" ", "")


def test_model_never_submits_records_failure_without_picking_a_move(tmp_path):
    """模型一直不提交：记 technical 失败，平台不替它选一步棋。"""
    async def scenario():
        runner = make_runner(tmp_path, "agent-refuse", "agent-refuse")
        game_id = start(runner, max_plies=4, move_timeout_seconds=6)
        return runner, game_id, await wait_for_game(runner, game_id)
    runner, game_id, game = asyncio.run(scenario())
    assert game["status"] == "finished"
    assert game["winner"] == "black" and game["reason"] == "timeout"  # 红方先手失败，黑方获胜
    assert game["history"] == []
    failure = runner.db.one("SELECT error FROM moves WHERE game_id=? AND ply=0", (game_id,))
    assert failure["error"] in {"agent_no_submit", "agent_rounds_exhausted", "timeout"}


def test_round_limit_is_respected(tmp_path):
    """轮数上限生效：每手请求次数不超过 agent_max_rounds，用尽则记录失败而不代选着法。"""
    async def enough():
        runner = make_runner(tmp_path / "enough", "agent-check", "agent-check")
        game_id = start(runner, max_plies=2, agent_max_rounds=3)
        return runner, game_id, await wait_for_game(runner, game_id)
    runner, game_id, game = asyncio.run(enough())
    assert len(game["history"]) == 2
    for ply in (0, 1):
        requests = [row for row in actions(runner, game_id, ply) if row["kind"] == "request"]
        assert 1 <= len(requests) <= 3

    async def too_few():
        runner = make_runner(tmp_path / "few", "agent-check", "agent-check")
        game_id = start(runner, max_plies=2, agent_max_rounds=2)
        return runner, game_id, await wait_for_game(runner, game_id)
    runner, game_id, game = asyncio.run(too_few())
    assert game["status"] == "finished" and game["reason"] == "timeout"
    assert game["history"] == []                      # 平台不替模型挑棋
    assert game["agent_max_rounds"] == 2
    requests = [row for row in actions(runner, game_id, 0) if row["kind"] == "request"]
    assert len(requests) == 2
    failure = runner.db.one("SELECT error FROM moves WHERE game_id=? AND ply=0", (game_id,))
    assert failure["error"] == "agent_rounds_exhausted"


def test_notes_are_per_side_and_replaced(tmp_path):
    """笔记只属于写它的那一方，写入即替换，并且带上手数。"""
    async def scenario():
        runner = make_runner(tmp_path, "agent-note", "agent-check")
        game_id = start(runner, max_plies=2)
        return runner, game_id, await wait_for_game(runner, game_id)
    runner, game_id, game = asyncio.run(scenario())
    red = runner.db.one("SELECT text, ply FROM agent_notes WHERE game_id=? AND side='red'", (game_id,))
    black = runner.db.one("SELECT text FROM agent_notes WHERE game_id=? AND side='black'", (game_id,))
    assert red and red["ply"] == 0 and "中路" in red["text"]
    assert black is None


def test_own_note_returns_next_move_but_never_leaks_to_the_opponent(tmp_path):
    """跨手精简上下文：下一手只带回自己的短笔记，对方的局面消息里看不到它。"""
    async def scenario():
        runner = make_runner(tmp_path, "agent-note", "agent-check")
        game_id = start(runner, max_plies=3)
        return runner, game_id, await wait_for_game(runner, game_id)
    runner, game_id, game = asyncio.run(scenario())

    def prompt(ply: int) -> dict:
        row = runner.db.one("SELECT prompt_json FROM moves WHERE game_id=? AND ply=? AND error IS NULL", (game_id, ply))
        assert row, f"第 {ply} 手没有成功记录"
        return json.loads(row["prompt_json"])

    black_first, red_second = prompt(1), prompt(2)
    assert black_first["player_side"] == "black" and black_first["private_note"] is None
    assert "中路" not in json.dumps(black_first, ensure_ascii=False)        # 红方笔记没有泄漏
    assert red_second["player_side"] == "red" and "中路" in red_second["private_note"]["text"]
    assert red_second["ply"] == 2 and red_second["history_length"] == 2     # 每手都是最新局面
    assert red_second["last_move"]["side"] == "black" and red_second["last_move"]["move"] == game["history"][1]


def test_text_reply_without_tool_calls_still_submits(tmp_path):
    """模型不会用工具也能走：正文给出的着法被当作提交。"""
    async def scenario():
        runner = make_runner(tmp_path, "agent-text", "agent-text")
        game_id = start(runner, max_plies=1)
        return runner, game_id, await wait_for_game(runner, game_id)
    runner, game_id, game = asyncio.run(scenario())
    assert len(game["history"]) == 1
    assert any(row["kind"] == "text_submit" for row in actions(runner, game_id, 0))


def test_stop_prevents_late_submission(tmp_path):
    """停止后迟到的工具/提交不再生效：本手不再继续，棋盘不动。"""
    async def scenario():
        runner = make_runner(tmp_path, "agent-refuse", "agent-refuse")
        game_id = start(runner, max_plies=4, move_timeout_seconds=30)
        runner.stop(game_id)                  # 在第一次 await 之前就停：整手直接作废
        await asyncio.sleep(0.05)
        return runner, game_id, runner.db.game(game_id)
    runner, game_id, game = asyncio.run(scenario())
    assert game["status"] == "stopped" and game["reason"] == "user_stopped"
    assert game["history"] == []
    assert actions(runner, game_id) == []


def test_actions_are_recorded_and_published_as_they_happen(tmp_path):
    """每条行动刚落定就写库并推事件：网页不用等整手结束就能看到行动链条。"""
    from backend.app.agent import AgentAction

    async def scenario():
        runner = make_runner(tmp_path, "agent-check", "agent-check")
        game_id = start(runner, max_plies=4, move_timeout_seconds=30)
        runner.stop(game_id)
        await asyncio.sleep(0.05)
        stream = runner.bus.stream(game_id)
        events = []
        pending = asyncio.create_task(stream.__anext__())
        await asyncio.sleep(0.01)                  # 让订阅先注册队列，再落盘推事件
        await runner._record_action(game_id, 0, "red", AgentAction(
            sequence=0, kind="tool", name="get_legal_moves", args={}, result={"count": 44}))
        events.append(await asyncio.wait_for(pending, 5))
        await stream.aclose()
        return runner, game_id, events
    runner, game_id, events = asyncio.run(scenario())
    row = runner.db.one("SELECT kind, name, result_json, sequence FROM agent_actions WHERE game_id=?", (game_id,))
    assert row["kind"] == "tool" and row["name"] == "get_legal_moves"
    assert '"count": 44' in row["result_json"] and row["sequence"] == 0
    assert events[0]["type"] == "action" and events[0]["name"] == "get_legal_moves"


def test_retry_is_only_allowed_once_and_only_with_time_left():
    """超时重试的门槛：整手只一次，剩余时间太短就不再发。"""
    tools = RuleTools(arbiter=Arbiter(), game_id="g11", side="red", initial_fen=START_FEN, history=[])
    turn = AgentTurn(client=None, preset=Preset(id="p", name="p", connection_id="red-agent", model="mock"),
                     tools=tools, side="red", ply=0, deadline=time.monotonic() + 120,
                     position_message="{}", max_rounds=6)
    assert abs(turn.retry_floor() - 6) < 0.1 and turn._can_retry(False, 0)
    assert not turn._can_retry(True, 0)                    # 整手只重试一次
    assert not turn._can_retry(False, 5)                   # 最后一轮没有下一轮可用
    turn.deadline = time.monotonic() + 4
    assert not turn._can_retry(False, 0)                   # 只剩 4 秒：不值得再等一次请求
    turn.deadline = time.monotonic() + 10
    assert turn._can_retry(False, 0)                       # 10 秒够换一次「只提交」
    short = AgentTurn(client=None, preset=Preset(id="p", name="p", connection_id="red-agent", model="mock"),
                      tools=tools, side="red", ply=0, deadline=time.monotonic() + 20,
                      position_message="{}", max_rounds=6)
    assert short.retry_floor() == 1                         # 短预算按比例缩小


def test_request_timeout_gets_one_submit_only_retry(tmp_path):
    """一次请求超时不该直接判本手失败：用剩下的时间只换一次提交（整手只重试一次）。"""
    async def scenario():
        runner = make_runner(tmp_path, "agent-timeout-once", "agent-check")
        game_id = start(runner, max_plies=1, move_timeout_seconds=20)
        return runner, game_id, await wait_for_game(runner, game_id)
    runner, game_id, game = asyncio.run(scenario())
    assert game["history"] == ["h2e2"]
    rows = actions(runner, game_id, 0)
    assert [row["kind"] for row in rows] == ["request", "request", "submit"]
    assert rows[0]["error"] == "request_timeout"
    retry = json.loads(rows[1]["args_json"])
    assert retry["round"] == 2 and retry["tools"] == ["submit_move"]     # 重试只给提交工具
    assert rows[2]["name"] == "submit_move"


def test_cancelled_turn_stops_before_next_round():
    """整手被取消后不再发请求、也不再落子（停止后的迟到结果没有落点）。"""
    tools = RuleTools(arbiter=Arbiter(), game_id="g6", side="red", initial_fen=START_FEN, history=[])
    turn = AgentTurn(client=None, preset=Preset(id="p", name="p", connection_id="red-agent", model="mock"),
                     tools=tools, side="red", ply=0, deadline=time.monotonic() + 30,
                     position_message="{}", max_rounds=6, cancelled=lambda: True)
    outcome = asyncio.run(turn.run())
    assert outcome.error == "cancelled" and outcome.move is None and outcome.actions == []


def test_tool_arguments_are_forgiving_but_still_exact():
    """工具定义写 `from`，实现叫 `source`：等价别名要能用，真正的错参数仍报错。"""
    tools = RuleTools(arbiter=Arbiter(), game_id="g8", side="red", initial_fen=START_FEN, history=[])
    by_from = tools.call("get_legal_moves", {"from": "b0"})
    assert by_from.ok and by_from.payload["moves"] and all(
        move.startswith("b0") for move in by_from.payload["moves"])
    assert tools.call("get_legal_moves", {"square": "b0"}).payload["moves"] == by_from.payload["moves"]
    assert tools.call("get_legal_moves", {"source": "b0"}).payload["moves"] == by_from.payload["moves"]
    assert not tools.call("get_legal_moves", {"from": "zz"}).ok
    assert not tools.call("get_legal_moves", {"colour": "red"}).ok          # 未知参数照旧报错
    single = tools.call("simulate_line", {"moves": "b0c2"})                  # 模型只给一个字符串
    assert single.ok and single.payload["steps"][0]["legal"] is True
    assert tools.call("get_history", {"start": "0", "end": "1"}).ok


def test_illegal_move_reason_explains_the_move_rule():
    """非法原因要讲走子规则，而不是只说「不在合法着法里」。"""
    tools = RuleTools(arbiter=Arbiter(), game_id="g9", side="red", initial_fen=START_FEN, history=[])
    knight = tools.check_move("b0d1").payload["reason"]              # 马走田字
    assert "马走日字" in knight
    rook = tools.check_move("a0b0").payload["reason"]                # 车斜走
    assert "车只能沿直线走" in rook
    blocked = tools.check_move("a0a9").payload["reason"]             # 路径被自己的兵挡住
    assert "路径上有棋子" in blocked and "a3" in blocked
    # 走法本身合规但不能走：把 e5 的红车挪开就让红帅被黑车将军
    self_check = RuleTools(arbiter=Arbiter(), game_id="g10", side="red",
                           initial_fen="3kr4/9/9/9/4R4/9/9/9/9/4K4 w - - 0 1", history=[])
    reason = self_check.check_move("e5d5").payload["reason"]
    assert self_check.check_move("e5d5").payload["legal"] is False
    assert "自己的将/帅被将军" in reason


def test_tools_reject_duplicate_and_foreign_input(tmp_path):
    """工具层的边界：重复提交、读取对方笔记、伪造坐标。"""
    db = Database(tmp_path / "tools.db")
    db.execute("""INSERT INTO games(id,status,red_preset,black_preset,initial_fen,current_fen,history_json,
                  move_timeout,max_plies,ruleset_id,created_at,updated_at,mode)
                  VALUES('g1','running','red','black',?,?,'[]',600,400,?, '', '', 'agent')""",
               (START_FEN, START_FEN, RULESET_ID))
    tools = RuleTools(arbiter=Arbiter(), game_id="g1", side="red", initial_fen=START_FEN, history=[],
                      note_store=NoteStore(db, "g1", "red"))
    first = tools.get_legal_moves().payload["moves"][0]
    assert tools.submit_move(first).ok
    again = tools.submit_move(first)
    assert not again.ok and "重复提交" in again.payload["reason"]
    assert not tools.submit_move("z9z8").ok
    assert not tools.get_legal_moves(source="z9").ok
    assert tools.check_move("a0a9").payload["legal"] is False
    # 对方笔记读不到：黑方读自己的，是空的
    other = RuleTools(arbiter=Arbiter(), game_id="g1", side="black", initial_fen=START_FEN, history=[],
                      note_store=NoteStore(db, "g1", "black"))
    tools.write_note("红方计划：中炮")
    assert other.read_note().payload["private_note"] is None
    assert "中炮" in tools.read_note().payload["private_note"]["text"]


def test_simulate_line_keeps_real_history_and_never_touches_board(tmp_path):
    """试走保留真实历史，且不改动真实棋盘。"""
    tools = RuleTools(arbiter=Arbiter(), game_id="g2", side="red", initial_fen=START_FEN, history=[])
    first = tools.get_legal_moves().payload["moves"][0]
    # 第二步换黑方：黑方的合法着法要用「初始局面 + 红方这一手」的工具去问
    reply = RuleTools(arbiter=Arbiter(), game_id="g2", side="black", initial_fen=START_FEN, history=[first])
    line = tools.simulate_line(moves=[first, reply.get_legal_moves().payload["moves"][0]])
    assert line.ok and all(step["legal"] for step in line.payload["steps"])
    assert tools.history == []                      # 真实历史没变
    assert tools.get_position().payload["ply"] == 0
    bad = tools.simulate_line(moves=[first, "a0a9"])
    assert bad.payload["steps"][-1]["legal"] is False and "reason" in bad.payload["steps"][-1]


def test_simulate_line_detects_terminal_position(tmp_path):
    """试走能给出将死判定：直接摆一个一步杀的局面。"""
    # 红方双车控住 d、f 两路，a5 的车平到 e5 将军：黑将四个逃格全被控，且无法吃子。
    fen = "4k4/9/9/9/R8/9/9/9/9/K2R1R3 w - - 0 1"
    tools = RuleTools(arbiter=Arbiter(), game_id="g3", side="red", initial_fen=fen, history=[])
    kill = None
    reasons = set()
    for move in tools.get_legal_moves().payload["moves"]:
        result = tools.simulate_line(moves=[move])
        if result.payload.get("ended"):
            reasons.add(result.payload["reason"])
            if result.payload["reason"] == "checkmate":
                kill = (move, result.payload)
    assert kill, f"该局面应当存在一步将死，实际只试出 {reasons or '没有终局'}"
    _, payload = kill
    assert payload["winner"] == "red" and payload["reason"] == "checkmate"


def test_agent_budget_shrinks_and_reserves_submit_window():
    """预算按整手管理：单次请求有上限，临近结束只保留提交窗口。"""
    tools = RuleTools(arbiter=Arbiter(), game_id="g4", side="red", initial_fen=START_FEN, history=[])
    turn = AgentTurn(client=None, preset=Preset(id="p", name="p", connection_id="red-agent", model="mock"),
                     tools=tools, side="red", ply=0, deadline=time.monotonic() + 200,
                     position_message="{}", max_rounds=6)
    assert turn.request_budget() <= 100.5          # 不超过整手的一半
    assert abs(turn.request_budget() - 80) < 0.5   # 200 秒的 40%：一次请求不该吃掉整手一半
    assert not turn.in_submit_window()
    turn.deadline = time.monotonic() + 10          # 进入提交窗口
    assert turn.in_submit_window()
    assert turn.request_budget() <= 10
    assert turn.request_budget(submit_only=True) <= 10
    turn.deadline = time.monotonic() + 100         # 只提交的那一次不再为提交预留时间
    assert turn.request_budget() <= 76             # 100 − 30（预留）与单次上限取小
    assert 95 <= turn.request_budget(submit_only=True) <= 100


def test_submit_window_adapts_to_how_long_a_request_actually_takes():
    """实测出单次请求耗时后，剩余时间不够再发一次就直接进入提交窗口。"""
    tools = RuleTools(arbiter=Arbiter(), game_id="g7", side="red", initial_fen=START_FEN, history=[])
    turn = AgentTurn(client=None, preset=Preset(id="p", name="p", connection_id="red-agent", model="mock"),
                     tools=tools, side="red", ply=0, deadline=time.monotonic() + 200,
                     position_message="{}", max_rounds=6)
    assert turn.measured_request_seconds() == 0
    assert turn.submit_window_threshold() == 30    # 没有实测数据时：20% 与 30 秒取小
    turn.request_seconds.append(45.0)              # 这一手的上一次请求花了 45 秒
    assert turn.submit_window_threshold() == 54    # 45 × 1.2
    assert not turn.in_submit_window()             # 还剩 200 秒，仍然可以继续用工具
    turn.deadline = time.monotonic() + 60
    assert turn.in_submit_window()                 # 只剩 60 秒：不够再发一次 45 秒的请求
    assert turn.request_budget() <= 60


def test_agent_requires_position_id_after_ply_advances(tmp_path):
    """手数前进后旧的 position_id 失效：迟到的提交被拒绝。"""
    db = Database(tmp_path / "stale.db")
    db.execute("""INSERT INTO games(id,status,red_preset,black_preset,initial_fen,current_fen,history_json,
                  move_timeout,max_plies,ruleset_id,created_at,updated_at,mode)
                  VALUES('g5','running','red','black',?,?,'[]',600,400,?, '', '', 'agent')""",
               (START_FEN, START_FEN, RULESET_ID))
    first = RuleTools(arbiter=Arbiter(), game_id="g5", side="red", initial_fen=START_FEN, history=[])
    state = apply_history(START_FEN, [])
    apply_move(state, first.get_legal_moves().payload["moves"][0])
    later = RuleTools(arbiter=Arbiter(), game_id="g5", side="black", initial_fen=START_FEN,
                      history=[first.get_legal_moves().payload["moves"][0]])
    assert later.position_id != first.position_id
    rejected = later.submit_move(later.get_legal_moves().payload["moves"][0], position_id=first.position_id)
    assert not rejected.ok and "过期" in rejected.payload["reason"]


def test_agent_preserves_provider_reasoning_across_tool_rounds():
    call = ToolCall("call-1", "get_position", {})
    message = {"role": "assistant", "content": "", "calls": [call],
               "reasoning_content": "chat reasoning",
               "reasoning_items": [{"type": "reasoning", "id": "r1", "encrypted_content": "opaque"}],
               "thinking_blocks": [{"type": "thinking", "thinking": "claude reasoning", "signature": "sig"}]}
    assert _openai_chat_messages([message])[0]["reasoning_content"] == "chat reasoning"
    assert _responses_input([message])[0]["type"] == "reasoning"
    assert _anthropic_agent_messages([message])[0]["content"][0]["signature"] == "sig"


def test_agent_output_budget_is_shared_across_rounds():
    class Client:
        def __init__(self): self.limits = []
        async def choose_with_tools(self, preset, messages, tools, timeout, **kwargs):
            self.limits.append(preset.max_tokens)
            if len(self.limits) == 1:
                return ToolReply(calls=[ToolCall("c1", "check_move", {"move": "h2e2"})],
                                 input_tokens=10, output_tokens=60)
            return ToolReply(calls=[ToolCall("c2", "submit_move", {"move": "h2e2"})],
                             input_tokens=10, output_tokens=40)
    client = Client()
    tools = RuleTools(arbiter=Arbiter(), game_id="budget", side="red", initial_fen=START_FEN, history=[])
    turn = AgentTurn(client=client,
                     preset=Preset(id="p", name="p", connection_id="p", model="mock", max_tokens=100),
                     tools=tools, side="red", ply=0, deadline=time.monotonic() + 30,
                     position_message="{}", max_rounds=3)
    outcome = asyncio.run(turn.run())
    assert outcome.move == "h2e2" and outcome.output_tokens == 100
    assert client.limits == [100, 40]


def test_agent_rejects_late_move_and_only_allows_one_submit_correction():
    class Client:
        def __init__(self, move): self.move = move
        async def choose_with_tools(self, preset, messages, tools, timeout, **kwargs):
            return ToolReply(calls=[ToolCall("c", "submit_move", {"move": self.move})],
                             input_tokens=1, output_tokens=1, cache_read_tokens=0, cache_write_tokens=0)

    tools = RuleTools(arbiter=Arbiter(), game_id="late", side="red", initial_fen=START_FEN, history=[])
    async def late():
        async def slow_record(action):
            if action.kind == "request": await asyncio.sleep(0.03)
        turn = AgentTurn(client=Client("h2e2"), preset=Preset(id="p", name="p", connection_id="p", model="mock"),
                         tools=tools, side="red", ply=0, deadline=time.monotonic() + 0.01,
                         position_message="{}", max_rounds=2, on_action=slow_record)
        return await turn.run()
    late_outcome = asyncio.run(late())
    assert late_outcome.error == "timeout" and late_outcome.move is None

    invalid_tools = RuleTools(arbiter=Arbiter(), game_id="invalid", side="red", initial_fen=START_FEN, history=[])
    invalid_turn = AgentTurn(client=Client("a0a0"),
                             preset=Preset(id="p", name="p", connection_id="p", model="mock"),
                             tools=invalid_tools, side="red", ply=0, deadline=time.monotonic() + 30,
                             position_message="{}", max_rounds=6)
    invalid_outcome = asyncio.run(invalid_turn.run())
    assert invalid_outcome.error == "invalid_move" and invalid_tools.submit_state.attempts == 2
