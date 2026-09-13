"""行动回放与费用统计：一手内的请求、工具、落子要能完整对应，费用含重试。

设计目标（与设计稿第 3 步一致）：
- 每手的行动链条可以直接读出来，例如「查询合法走法 → 校验一步 → 正式落子 a0a1」；
- 每手的 token、耗时与请求次数包含同一手里的重试与失败请求；
- 单价未知时费用留空，不用 0 冒充。
"""
import asyncio
import json

from backend.app.agent import AgentAction
from backend.app.schemas import Preset
from backend.app.timeline import build_timeline, load_actions, move_headline, render_timeline
from tests.test_agent import make_runner, start, wait_for_game


def run_game(tmp_path, red, black, **overrides):
    async def scenario():
        runner = make_runner(tmp_path, red, black)
        game_id = start(runner, **overrides)
        return runner, game_id, await wait_for_game(runner, game_id)
    return asyncio.run(scenario())


def timeline_of(runner, game):
    return build_timeline(runner.db, game, lambda side: runner._preset_for(game, side))


def test_timeline_matches_every_action_in_a_move(tmp_path):
    """一手内的每次请求、每次工具调用、唯一一次落子都在回放里。"""
    runner, game_id, game = run_game(tmp_path, "agent-check", "agent-check", max_plies=2)
    timeline = timeline_of(runner, game)
    assert timeline["mode"] == "agent" and timeline["max_rounds"] == 6

    first = timeline["moves"][0]
    assert (first["requests"], first["tools"], first["submits"]) == (3, 2, 1)
    assert first["input_tokens"] > 0 and first["output_tokens"] > 0
    kinds = [(action["kind"], action["name"]) for action in first["actions"]]
    assert kinds == [("request", "model"), ("tool", "get_legal_moves"), ("request", "model"),
                     ("tool", "check_move"), ("request", "model"), ("submit", "submit_move")]
    assert move_headline(first) == "查询合法走法 → 校验一步 → 正式落子 " + game["history"][0]
    assert first["actions"][0]["args"]["tools"] and len(first["actions"][0]["args"]["tools"]) == 8

    assert timeline["total"]["requests"] == 6 and timeline["total"]["tools"] == 4
    assert timeline["total"]["input_tokens"] == sum(move["input_tokens"] for move in timeline["moves"])


def test_timeline_keeps_failed_requests_and_retries_in_cost(tmp_path):
    """一手没走成时也要留下可回放的原因与开销，而不是静默丢掉。"""
    runner, game_id, game = run_game(tmp_path, "agent-refuse", "agent-refuse",
                                    max_plies=4, move_timeout_seconds=6)
    timeline = timeline_of(runner, game)
    first = timeline["moves"][0]
    assert first["failed"] is True and first["submits"] == 0
    assert first["requests"] == 6 and first["input_tokens"] > 0
    failure = runner.db.one("SELECT error FROM moves WHERE game_id=? AND ply=0", (game_id,))
    assert failure["error"] == "agent_rounds_exhausted"
    assert first["ply"] == 0 and first["side"] == "red"
    assert move_headline(first) == ""


def test_timeline_counts_tool_errors_and_text_submit(tmp_path):
    """非法提交被拒绝算工具错误；正文落子也记进回放。"""
    runner, game_id, game = run_game(tmp_path, "agent-illegal-submit", "agent-check", max_plies=1)
    timeline = timeline_of(runner, game)
    first = timeline["moves"][0]
    assert first["tool_errors"] == 1                      # a0a9 被拒
    assert first["submits"] == 1 and first["requests"] == 3
    rejected = [action for action in first["actions"] if action["name"] == "submit_move"]
    assert rejected and rejected[0]["error"] == "error"
    assert "不合法" in json.dumps(rejected[0]["result"], ensure_ascii=False)


def test_timeline_cost_estimate_uses_preset_prices(tmp_path):
    """给了单价才算费用：费用 = 整手累计输入与输出 token × 单价。"""
    runner, game_id, game = run_game(tmp_path, "agent-check", "agent-check", max_plies=1)
    preset = Preset(id="p", name="p", connection_id="red-agent", model="mock",
                    input_price_per_million=1.0, output_price_per_million=2.0)
    timeline = build_timeline(runner.db, game, lambda side: preset)
    move = timeline["moves"][0]
    expected = move["input_tokens"] / 1_000_000 + move["output_tokens"] * 2 / 1_000_000
    assert move["cost_estimate"] is not None and abs(move["cost_estimate"] - expected) < 1e-9
    assert timeline["total"]["cost_estimate"] is not None

    unknown = timeline_of(runner, game)                   # mock 预设没有单价
    assert unknown["moves"][0]["cost_estimate"] is None and unknown["total"]["cost_estimate"] is None


def test_missing_usage_keeps_cost_and_totals_unknown(tmp_path):
    runner, game_id, game = run_game(tmp_path, "agent-check", "agent-check", max_plies=1)
    runner.db.execute("UPDATE moves SET output_tokens=NULL WHERE game_id=?", (game_id,))
    preset = Preset(id="p", name="p", connection_id="red-agent", model="mock",
                    input_price_per_million=1.0, output_price_per_million=2.0)
    timeline = build_timeline(runner.db, game, lambda side: preset)
    assert timeline["moves"][0]["output_tokens"] is None
    assert timeline["moves"][0]["cost_estimate"] is None
    assert timeline["total"]["output_tokens"] is None and timeline["total"]["cost_estimate"] is None


def test_render_timeline_reads_like_a_replay(tmp_path):
    """文本回放：每手一行汇总，下面是这一手的每个动作，末尾给合计。"""
    runner, game_id, game = run_game(tmp_path, "agent-simulate", "agent-check", max_plies=1)
    timeline = timeline_of(runner, game)
    text = render_timeline(game, timeline)
    assert "模式 agent · 每手最多 6 轮请求" in text
    assert "[第 1 手 红方] 请求 3 次" in text and "估算费用 未知" in text
    assert "试走变化" in text and "正式落子 → 接受" in text
    assert text.rstrip().endswith("估算费用 未知")


def test_timeline_shows_an_in_flight_move_before_it_is_played(tmp_path):
    """整手还没落子时回放已经能看到这一手：token 用已记录的请求用量，不假装是 0。"""
    async def scenario():
        runner = make_runner(tmp_path, "agent-check", "agent-check")
        game_id = start(runner)
        runner.stop(game_id)
        await asyncio.sleep(0.02)
        await runner._record_action(game_id, 0, "red", AgentAction(
            sequence=0, kind="request", name="model", args={"round": 1, "tools": ["get_legal_moves"]},
            duration_ms=1200, input_tokens=900, total_input_tokens=1200, output_tokens=40))
        await runner._record_action(game_id, 0, "red", AgentAction(
            sequence=1, kind="tool", name="get_legal_moves", args={}, result={"count": 44}))
        game = runner.db.game(game_id)
        return build_timeline(runner.db, game, lambda side: runner._preset_for(game, side))

    timeline = asyncio.run(scenario())
    move = timeline["moves"][0]
    assert move["submits"] == 0 and move["requests"] == 1 and move["tools"] == 1
    assert (move["input_tokens"], move["output_tokens"], move["duration_ms"]) == (1200, 40, 1200)
    assert move_headline(move) == "查询合法走法"
    assert timeline["total"]["input_tokens"] == 1200


def test_actions_are_scoped_per_side(tmp_path):
    """行动记录按手数+行棋方分开：红黑两手的回放不會互相串。"""
    runner, game_id, game = run_game(tmp_path, "agent-check", "agent-check", max_plies=2)
    actions = load_actions(runner.db, game_id)
    assert {action["side"] for action in actions} == {"red", "black"}
    assert [action["ply"] for action in actions] == sorted(action["ply"] for action in actions)
    assert all(action["sequence"] >= 0 for action in actions)
    red_ply0 = [action for action in actions if action["ply"] == 0 and action["side"] == "red"]
    black_ply1 = [action for action in actions if action["ply"] == 1 and action["side"] == "black"]
    assert len(red_ply0) == len(black_ply1) == 6
