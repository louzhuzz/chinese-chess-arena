"""评审 9 项的定点验证：全部本地 mock / 假提供方，不调用付费模型，不依赖 pytest。

这是一个独立脚本（pytest 只收集 test_*.py，不会收集它），编号对应评审报告：
 实现质量 1 推理续接字段 / 2 思考强度透传 / 3 批量评测模式 / 4 未知用量不当作 0
 实现质量 5 棋谱导出终止状态 / 6 排查日志完整性
 方案符合度 7 落子前检查截止时间 / 8 整手输出预算 / 9 正式提交只纠正一次

用法： python tests\verify_review_fixes.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml
import httpx

from backend.app.agent import AgentAction, AgentTurn
from backend.app.arbiter import Arbiter
from backend.app.config import ConfigStore
from backend.app.db import Database
from backend.app.models import ToolCall, ToolReply, describe_error
from backend.app.record import render_fen_record
from backend.app.rules import START_FEN
from backend.app.runner import BenchmarkRunner, EventBus, GameRunner
from backend.app.schemas import BenchmarkCreate, GameCreate, Preset
from backend.app.timeline import build_timeline, load_actions, render_timeline
from backend.app.tools import RuleTools

WORK = ROOT / "run" / "verify-tmp"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail else ""))


def make_runner(path: Path, red_policy: str, black_policy: str, *, strict_url: str | None = None,
                prices: bool = False) -> GameRunner:
    path.mkdir(parents=True, exist_ok=True)
    connections = [
        {"id": "red-agent", "name": "Red agent", "protocol": "mock", "base_url": red_policy},
        {"id": "black-agent", "name": "Black agent", "protocol": "mock", "base_url": black_policy},
    ]
    if strict_url:
        connections.append({"id": "strict", "name": "Strict wire", "protocol": "openai_chat",
                            "base_url": strict_url, "api_key": "sk-strict-secret"})
    red = {"id": "red", "name": "Red", "connection_id": "red-agent", "model": "mock"}
    if prices:
        red |= {"input_price_per_million": 1.0, "output_price_per_million": 2.0}
    presets = [red, {"id": "black", "name": "Black", "connection_id": "black-agent", "model": "mock"}]
    if strict_url:
        presets.append({"id": "strict", "name": "Strict", "connection_id": "strict",
                        "model": "strict-model", "reasoning_effort": "low", "temperature": None})
    (path / "models.yaml").write_text(yaml.safe_dump({"connections": connections, "presets": presets},
                                                     allow_unicode=True), encoding="utf-8")
    return GameRunner(Database(path / "verify.db"), ConfigStore(path / "models.yaml"), EventBus())


async def wait_game(runner: GameRunner, game_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        game = runner.db.game(game_id)
        if game["status"] not in {"queued", "running"}:
            return game
        await asyncio.sleep(0.02)
    raise AssertionError(f"game {game_id} did not finish")


def actions_of(runner: GameRunner, game_id: str) -> list[dict]:
    return runner.db.all("SELECT * FROM agent_actions WHERE game_id=? ORDER BY ply, sequence", (game_id,))


def start(runner: GameRunner, **overrides) -> str:
    spec = dict(red_preset_id="red", black_preset_id="black", move_timeout_seconds=30,
                max_plies=2, mode="agent")
    spec.update(overrides)
    game_id = runner.create(GameCreate(**spec))
    runner.start(game_id)
    return game_id


# ---------------------------------------------------------------- 1 + 2：严格协议
class StrictWire(BaseHTTPRequestHandler):
    """最小号「严格提供方」：第 2 轮起，助手消息缺 reasoning_content 就按 400 拒绝。

    真实 DeepSeek 开启思考并调用工具时就是这个要求：工具轮次的助手消息要把
    reasoning_content 原样带回去，否则第二轮直接失败。
    """

    protocol_version = "HTTP/1.1"
    bodies: list[dict] = []
    rejects = 0

    def log_message(self, *args) -> None:
        pass

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        StrictWire.bodies.append(body)
        messages = body.get("messages") or []
        tool_turns = [m for m in messages if m.get("role") == "assistant" and m.get("tool_calls")]
        if tool_turns:
            # 严格到底：DeepSeek 要 reasoning_content，Command Code 这类兼容网关要
            # reasoning / reasoning_details，缺任何一个都按 400 拒绝。
            missing = [m for m in tool_turns
                       if not m.get("reasoning_content") or not m.get("reasoning")
                       or not m.get("reasoning_details")]
            if missing:
                StrictWire.rejects += 1
                self._send({"error": {"message": "reasoning fields are required in tool-call turns",
                                      "type": "invalid_request_error"}}, 400)
                return
            legal: list[str] = []
            for message in messages:
                if message.get("role") != "tool":
                    continue
                try:
                    data = json.loads(message.get("content") or "{}")
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict) and isinstance(data.get("moves"), list) and data["moves"]:
                    legal = data["moves"]
            move = legal[0] if legal else "h2e2"
            self._send({"choices": [{"message": {"content": "", "tool_calls": [
                {"id": "call-2", "type": "function",
                 "function": {"name": "submit_move", "arguments": json.dumps({"move": move})}}]},
                "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 4,
                          "prompt_tokens_details": {"cached_tokens": 12}}})
            return
        self._send({"choices": [{"message": {
            "content": "",
            "reasoning_content": "先查合法着法，再决定走哪一步。",
            "reasoning": "先查合法着法，再决定走哪一步。",
            "reasoning_details": [{"type": "reasoning.text", "index": 0,
                                   "text": "先查合法着法，再决定走哪一步。"}],
            "tool_calls": [{"id": "call-1", "type": "function",
                            "function": {"name": "get_legal_moves", "arguments": "{}"}}]},
            "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5}})


def verify_reasoning_and_effort():
    server = ThreadingHTTPServer(("127.0.0.1", 0), StrictWire)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    try:
        runner = make_runner(WORK / "strict", "agent-check", "agent-check", strict_url=url)

        async def scenario():
            game_id = start(runner, red_preset_id="strict", black_preset_id="black", max_plies=1)
            return game_id, await wait_game(runner, game_id)

        game_id, game = asyncio.run(scenario())
        check("1 严格提供方下工具轮次仍能完成落子（reasoning_content 已回传）",
              len(game["history"]) == 1 and game["reason"] == "max_plies",
              f"status={game['status']} reason={game['reason']} history={game['history']}")
        check("1 严格提供方没有拒绝过任何一轮请求", StrictWire.rejects == 0, f"400 次数={StrictWire.rejects}")
        second = StrictWire.bodies[1] if len(StrictWire.bodies) > 1 else {}
        assistant = [m for m in (second.get("messages") or [])
                     if m.get("role") == "assistant" and m.get("tool_calls")]
        check("1 第二轮请求的助手消息带 reasoning_content",
              bool(assistant) and bool(assistant[0].get("reasoning_content")),
              f"reasoning_content={assistant[0].get('reasoning_content') if assistant else None!r}")
        check("1 兼容网关的 reasoning / reasoning_details 也原样回传",
              bool(assistant) and bool(assistant[0].get("reasoning"))
              and isinstance(assistant[0].get("reasoning_details"), list),
              f"reasoning_details={json.dumps(assistant[0].get('reasoning_details'), ensure_ascii=False) if assistant else None}")
        rows = [row for row in actions_of(runner, game_id) if row["kind"] == "request"]
        check("1 OpenAI 风格的缓存用量被识别（prompt_tokens_details.cached_tokens）",
              len(rows) >= 2 and rows[1]["cache_read_tokens"] == 12,
              f"第二轮 cache_read_tokens={rows[1]['cache_read_tokens'] if len(rows) > 1 else None}")
        check("2 智能体请求带上推理强度（openai_chat / Command Code 同一路径）",
              second.get("reasoning_effort") == "low", f"reasoning_effort={second.get('reasoning_effort')!r}")
        check("2 密钥没有进入请求体", "sk-strict-secret" not in json.dumps(second),
              "Authorization 只在头部")
        return runner, game_id
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- 6：接口故障的可读性
def verify_error_detail() -> None:
    """httpx 的连接类异常 str() 常是空串：只写异常名排查不了，必须带出底层原因。"""
    request = httpx.Request("POST", "https://api.example.com/provider/v1/chat/completions?api_key=sk-should-not-leak")
    exc = httpx.ConnectError("", request=request)
    exc.__cause__ = OSError("[Errno 11001] getaddrinfo failed")
    detail = describe_error(exc)
    check("6 空消息的连接异常也给出底层原因", "11001" in detail, detail)
    check("6 错误信息标明失败的接口地址", "POST https://api.example.com/provider/v1/chat/completions" in detail, detail)
    check("6 错误信息不带查询串（不泄漏密钥）", "sk-should-not-leak" not in detail and "?" not in detail, detail)

    class BrokenClient:
        async def choose_with_tools(self, preset, messages, tools, timeout, **kwargs):
            raise exc

    tools = RuleTools(arbiter=Arbiter(), game_id="g-err", side="red", initial_fen=START_FEN, history=[])
    turn = AgentTurn(client=BrokenClient(), preset=Preset(id="p", name="p", connection_id="p", model="mock"),
                     tools=tools, side="red", ply=0, deadline=time.monotonic() + 30,
                     position_message="{}", max_rounds=1)
    outcome = asyncio.run(turn.run())
    check("6 整手失败时错误里保留同一条可读信息",
          outcome.error and "11001" in outcome.error, f"error={outcome.error}")


# ---------------------------------------------------------------- 6：日志完整性
def verify_diagnostics(runner: GameRunner, game_id: str) -> None:
    long_text = "很长的原始回复" * 700

    async def scenario():
        await runner._record_action(game_id, 99, "red", AgentAction(
            sequence=0, kind="request", name="model", text=long_text,
            args={"round": 9, "tools": ["submit_move"], "max_tokens": 123, "timeout_seconds": 45.0},
            request={"model": "mock", "max_tokens": 123, "timeout_seconds": 45.0},
            raw={"choices": [{"finish_reason": "length"}]}, finish_reason="length"))

    asyncio.run(scenario())
    rows = [row for row in actions_of(runner, game_id) if row["kind"] == "request"]
    check("6 每轮请求都记下 finish_reason", bool(rows) and all(row["finish_reason"] for row in rows),
          f"finish_reasons={[row['finish_reason'] for row in rows]}")
    sent = [json.loads(row["request_json"]) for row in rows if row["request_json"]]
    check("6 每轮请求都记下实际发出的参数",
          bool(sent) and all("max_tokens" in body for body in sent),
          f"发出参数键={sorted(sent[0]) if sent else None}")
    raw = [json.loads(row["raw_json"]) for row in rows if row["raw_json"]]
    check("6 每轮请求都记下提供方原始回复", bool(raw) and all(isinstance(body, dict) for body in raw),
          f"原始回复键={sorted(raw[0]) if raw else None}")
    long_row = runner.db.one("SELECT text FROM agent_actions WHERE game_id=? AND ply=99", (game_id,))
    check("6 原始正文不再截断到 2000 字", bool(long_row) and len(long_row["text"]) == len(long_text),
          f"落盘长度={len(long_row['text']) if long_row else None} / 原文 {len(long_text)}")
    move = runner.db.one("SELECT actual_request_json FROM moves WHERE game_id=? AND ply=0", (game_id,))
    summary = json.loads(move["actual_request_json"]) if move and move["actual_request_json"] else {}
    rounds = summary.get("rounds") or []
    check("6 整手汇总含每轮参数与结束原因",
          bool(rounds) and rounds[0].get("finish_reason") and rounds[0].get("sent")
          and summary.get("output_token_limit"),
          f"rounds[0]={json.dumps(rounds[0], ensure_ascii=False)[:140] if rounds else '缺少 rounds'}")


# ---------------------------------------------------------------- 4：未知用量
def verify_unknown_usage() -> None:
    runner = make_runner(WORK / "usage", "agent-check", "agent-check", prices=True)

    async def scenario():
        game_id = start(runner, max_plies=1)
        await wait_game(runner, game_id)
        return game_id

    game_id = asyncio.run(scenario())
    game = runner.db.game(game_id)
    known = build_timeline(runner.db, game, lambda side: runner._preset_for(game, side))
    check("4 用量已知时才给费用", known["moves"][0]["cost_estimate"] is not None,
          f"cost={known['moves'][0]['cost_estimate']}")
    runner.db.execute("UPDATE moves SET input_tokens=NULL, output_tokens=NULL, total_input_tokens=NULL WHERE game_id=?",
                      (game_id,))
    game = runner.db.game(game_id)
    unknown = build_timeline(runner.db, game, lambda side: runner._preset_for(game, side))
    move = unknown["moves"][0]
    check("4 网关不给用量时 token 与费用都是未知",
          move["input_tokens"] is None and move["output_tokens"] is None
          and move["cost_estimate"] is None and unknown["total"]["cost_estimate"] is None,
          f"input={move['input_tokens']} cost={move['cost_estimate']} total={unknown['total']['cost_estimate']}")
    check("4 文本回放写「未知」而不是 0", "估算费用 未知" in render_timeline(game, unknown),
          next((line for line in render_timeline(game, unknown).splitlines() if "估算费用" in line), ""))


# ---------------------------------------------------------------- 5：棋谱导出
def verify_record_labels() -> None:
    base = {"id": "g" * 32, "initial_fen": START_FEN, "red_preset": "red", "black_preset": "black",
            "ruleset_id": "xiangqi-bench-rules-1.0", "move_timeout": 600, "moves": []}
    stopped = render_fen_record(base | {"status": "stopped", "winner": None, "reason": "user_stopped"})
    truncated = render_fen_record(base | {"status": "truncated", "winner": None, "reason": "max_plies"})
    draw = render_fen_record(base | {"status": "finished", "winner": None, "reason": "repetition_draw"})
    mate = render_fen_record(base | {"status": "finished", "winner": "red", "reason": "checkmate"})
    check("5 手动停止不写成和棋", "结果     : 未计胜负" in stopped and "手动停止" in stopped,
          [line for line in stopped.splitlines() if line.startswith("结果")][0])
    check("5 截断不写成和棋", "未计胜负" in truncated and "步数上限截断" in truncated)
    check("5 停止/截断不写「终局局面已由裁判确认」",
          "记录终止状态" in stopped and "记录终止状态" in truncated
          and "终局局面已由裁判确认" not in stopped and "终局局面已由裁判确认" not in truncated)
    check("5 真正的和棋与将死仍照旧",
          "结果     : 和棋" in draw and "终局局面已由裁判确认" in draw
          and "结果     : 红方胜" in mate and "终局局面已由裁判确认" in mate)

    # 同一套说法也要用在行动回放抬头：截断/停止的回放不能写成「和」。
    cases = (({"status": "stopped", "winner": None, "reason": "user_stopped"}, "未计胜负 · 手动停止"),
             ({"status": "truncated", "winner": None, "reason": "max_plies"}, "未计胜负 · 步数上限截断"),
             ({"status": "finished", "winner": None, "reason": "repetition_draw"}, "和棋 · 三次重复判和"),
             ({"status": "finished", "winner": "red", "reason": "checkmate"}, "红方胜 · 将死"))
    for row, expected in cases:
        header = render_timeline(base | row, {"mode": "agent", "max_rounds": 6, "moves": [], "total": {}}).splitlines()[1]
        check(f"5 行动回放抬头与棋谱同口径（{expected}）", header == f"结果: {expected} · 半回合 0", header)


# ---------------------------------------------------------------- 7 + 8 + 9：预算与期限
def verify_budget_and_deadline() -> None:
    # 9：正式提交非法只纠正一次；普通校验不占额度
    class IllegalClient:
        async def choose_with_tools(self, preset, messages, tools, timeout, **kwargs):
            step = sum(1 for m in messages if m.get("role") == "assistant" and m.get("calls"))
            if step == 0:
                return ToolReply(calls=[ToolCall("c0", "check_move", {"move": "a0a9"})],
                                 finish_reason="tool_calls", input_tokens=10, output_tokens=5)
            return ToolReply(calls=[ToolCall(f"c{step}", "submit_move", {"move": "a0a9"})],
                             finish_reason="tool_calls", input_tokens=10, output_tokens=5)

    tools = RuleTools(arbiter=Arbiter(), game_id="g9", side="red", initial_fen=START_FEN, history=[])
    turn = AgentTurn(client=IllegalClient(), preset=Preset(id="p", name="p", connection_id="p", model="mock"),
                     tools=tools, side="red", ply=0, deadline=time.monotonic() + 30,
                     position_message="{}", max_rounds=8)
    outcome = asyncio.run(turn.run())
    illegal_actions = [a for a in outcome.actions if a.name == "submit_move"]
    probe = [a for a in outcome.actions if a.kind == "tool" and a.name == "check_move"]
    check("9 连续非法提交在第二次后终止，不给第四次机会",
          outcome.error == "invalid_move" and tools.submit_state.attempts == 2 and outcome.move is None,
          f"error={outcome.error} attempts={tools.submit_state.attempts} 提交动作={len(illegal_actions)}")
    check("9 普通校验（试走/校验）不占用纠错次数", len(probe) == 1, f"check_move 次数={len(probe)}")

    # 8：整手输出额度按剩余量下发
    class BudgetClient:
        def __init__(self):
            self.limits: list[int] = []

        async def choose_with_tools(self, preset, messages, tools, timeout, **kwargs):
            self.limits.append(preset.max_tokens)
            if len(self.limits) == 1:
                return ToolReply(calls=[ToolCall("b1", "check_move", {"move": "h2e2"})],
                                 finish_reason="tool_calls", input_tokens=5, output_tokens=60)
            return ToolReply(calls=[ToolCall("b2", "submit_move", {"move": "h2e2"})],
                             finish_reason="tool_calls", input_tokens=5, output_tokens=40)

    budget_client = BudgetClient()
    budget_tools = RuleTools(arbiter=Arbiter(), game_id="g8", side="red", initial_fen=START_FEN, history=[])
    budget_turn = AgentTurn(client=budget_client,
                            preset=Preset(id="p", name="p", connection_id="p", model="mock", max_tokens=100),
                            tools=budget_tools, side="red", ply=0, deadline=time.monotonic() + 30,
                            position_message="{}", max_rounds=4)
    budget_outcome = asyncio.run(budget_turn.run())
    check("8 第二轮只拿到剩余输出额度", budget_client.limits == [100, 40],
          f"逐轮 max_tokens={budget_client.limits}")
    check("8 额度用尽后不再继续请求而是明确失败", budget_outcome.move == "h2e2",
          f"move={budget_outcome.move} tokens={budget_outcome.output_tokens}")

    class ExhaustClient:
        async def choose_with_tools(self, preset, messages, tools, timeout, **kwargs):
            return ToolReply(calls=[ToolCall("e1", "get_position", {})], finish_reason="tool_calls",
                             input_tokens=5, output_tokens=100)

    exhaust_tools = RuleTools(arbiter=Arbiter(), game_id="g8b", side="red", initial_fen=START_FEN, history=[])
    exhaust_turn = AgentTurn(client=ExhaustClient(),
                             preset=Preset(id="p", name="p", connection_id="p", model="mock", max_tokens=100),
                             tools=exhaust_tools, side="red", ply=0, deadline=time.monotonic() + 30,
                             position_message="{}", max_rounds=6)
    exhaust_outcome = asyncio.run(exhaust_turn.run())
    requests = [a for a in exhaust_outcome.actions if a.kind == "request"]
    check("8 输出额度耗尽时以 agent_output_limit 收手，不刷满 6 轮",
          exhaust_outcome.error == "agent_output_limit" and len(requests) == 1,
          f"error={exhaust_outcome.error} 请求轮数={len(requests)}")

    # 7：工具层的期限守卫 + 正文落子的期限守卫
    expired_tools = RuleTools(arbiter=Arbiter(), game_id="g7", side="red", initial_fen=START_FEN,
                              history=[], expired=lambda: True)
    rejected = expired_tools.submit_move("h2e2")
    check("7 期限已过的落子在工具层直接拒绝，且不算一次非法提交",
          not rejected.ok and rejected.payload.get("expired") and expired_tools.submit_state.attempts == 0,
          f"payload={json.dumps(rejected.payload, ensure_ascii=False)}")

    class TextClient:
        async def choose_with_tools(self, preset, messages, tools, timeout, **kwargs):
            return ToolReply(text=json.dumps({"move": "h2e2"}), finish_reason="stop",
                             input_tokens=5, output_tokens=5)

    async def late_text():
        tools = RuleTools(arbiter=Arbiter(), game_id="g7b", side="red", initial_fen=START_FEN, history=[])

        async def slow_record(action):
            if action.kind == "request":
                await asyncio.sleep(0.05)

        turn = AgentTurn(client=TextClient(), preset=Preset(id="p", name="p", connection_id="p", model="mock"),
                         tools=tools, side="red", ply=0, deadline=time.monotonic() + 0.01,
                         position_message="{}", max_rounds=2, on_action=slow_record)
        return await turn.run(), tools

    text_outcome, text_tools = asyncio.run(late_text())
    check("7 正文落子同样受整手截止时间约束",
          text_outcome.error == "timeout" and text_outcome.move is None and text_tools.submit_state.submitted is None,
          f"error={text_outcome.error} move={text_outcome.move}")


# ---------------------------------------------------------------- 3：批量评测模式
def verify_benchmark_mode() -> None:
    runner = make_runner(WORK / "bench", "agent-check", "agent-check", prices=True)
    benchmarks = BenchmarkRunner(runner.db, runner, runner.bus, runner.config)

    async def scenario():
        bench_id = benchmarks.create(BenchmarkCreate(preset_a_id="red", preset_b_id="black", pairs=1,
                                                     max_plies=2, move_timeout_seconds=30,
                                                     mode="agent", agent_max_rounds=4))
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            row = runner.db.one("SELECT status FROM benchmarks WHERE id=?", (bench_id,))
            if row and row["status"] not in {"queued", "running"}:
                break
            await asyncio.sleep(0.05)
        return bench_id

    bench_id = asyncio.run(scenario())
    report = benchmarks.report(bench_id)
    games = report["games"] if report else []
    agent_games = [g for g in games if g.get("mode") == "agent"]
    actions = sum(len(actions_of(runner, g["id"])) for g in games)
    check("3 批量评测按所选模式运行（界面/接口的 mode 贯通到每一局）",
          report and report["status"] == "finished" and len(games) == 2 and len(agent_games) == 2,
          f"status={report['status'] if report else None} 局数={len(games)} agent 局数={len(agent_games)}")
    check("3 评测局的每手轮数也随配置冻结", all(g.get("agent_max_rounds") == 4 for g in games),
          f"agent_max_rounds={[g.get('agent_max_rounds') for g in games]}")
    check("3 评测报告保留模式设置", (report or {}).get("settings", {}).get("mode") == "agent",
          f"settings.mode={(report or {}).get('settings', {}).get('mode')}")
    check("3 评测局确实产出了智能体行动记录", actions > 0, f"行动记录条数={actions}")


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    strict_runner, strict_game = verify_reasoning_and_effort()
    verify_error_detail()
    verify_diagnostics(strict_runner, strict_game)
    verify_unknown_usage()
    verify_record_labels()
    verify_budget_and_deadline()
    verify_benchmark_mode()
    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} 项通过")
    if failed:
        print("未通过：" + "；".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
