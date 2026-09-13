from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import httpx

from .arbiter import Arbiter, Verdict
from .agent import AgentTurn
from .config import ConfigStore
from .db import Database
from .models import (AGENT_PROMPT_VERSION, PROMPT_VERSION, ModelClient, ModelConfigError, RequestMetrics,
                     parse_move, parse_note, serialize_position)
from .rules import START_FEN, apply_history, apply_move, chinese_notation, pieces, to_fen
from .schemas import BenchmarkCreate, GameCreate, Preset
from .tools import RuleTools, NoteStore

# A finished game is only scored when the arbiter produced a rule result.
UNSCORED_REASONS = {"api_failure", "arbiter_failure", "process_restart", "user_stopped"}
FORFEIT_REASONS = {"invalid_move", "timeout"}
HUMAN_PROTOCOL = "human"
CONTEXT_MAX_MESSAGES = 48
CONTEXT_KEEP_MESSAGES = 24
# 首次请求给纠错重试留的余量：min(整步剩余 × SHARE, CAP) 秒。
CORRECTION_RESERVE_SHARE = 0.2
CORRECTION_RESERVE_CAP = 30.0


def first_attempt_budget(remaining: float) -> float:
    """首次请求可用的秒数。

    小预算仍按 80% 分配（120 秒 → 首答 96 秒，与旧行为一致）；
    预算放宽后只留固定 30 秒给纠错重试，慢速推理模型才能真正用满整步时限。
    """
    return max(0.0, remaining - min(remaining * CORRECTION_RESERVE_SHARE, CORRECTION_RESERVE_CAP))


def now() -> str: return datetime.now(timezone.utc).isoformat()


def _with_effort(preset: Preset, effort: str) -> Preset:
    if effort == "default":
        return preset
    if effort == "none":
        return preset.model_copy(update={"thinking":"disabled","reasoning_effort":None})
    return preset.model_copy(update={"thinking":"enabled","reasoning_effort":effort})


class EventBus:
    def __init__(self): self.queues: dict[str, set[asyncio.Queue]] = defaultdict(set)
    async def publish(self, channel: str, payload: dict):
        for queue in list(self.queues[channel]): await queue.put(payload)
    async def stream(self, channel: str):
        queue: asyncio.Queue = asyncio.Queue(); self.queues[channel].add(queue)
        try:
            while True:
                try: yield await asyncio.wait_for(queue.get(), 15)
                except asyncio.TimeoutError: yield {"type":"ping"}
        finally: self.queues[channel].discard(queue)


class GameRunner:
    def __init__(self, db: Database, config: ConfigStore, bus: EventBus, arbiter: Arbiter | None = None,
                 human_timeout: float | None = None):
        self.db=db; self.config=config; self.bus=bus; self.client=ModelClient(config)
        self.arbiter=arbiter or Arbiter(); self.tasks: dict[str, asyncio.Task] = {}
        self.human_timeout=human_timeout if human_timeout is not None else float(os.getenv("XIANGQI_HUMAN_TIMEOUT","1800"))
        self.pending: dict[str, asyncio.Future] = {}
        self.pending_side: dict[str, str] = {}

    def create(self, spec: GameCreate, benchmark_id: str | None = None) -> str:
        red_preset=self.config.preset(spec.red_preset_id); black_preset=self.config.preset(spec.black_preset_id)
        red_preset=_with_effort(red_preset,spec.red_reasoning_effort)
        black_preset=_with_effort(black_preset,spec.black_reasoning_effort)
        game_id = uuid.uuid4().hex; stamp=now(); fen=spec.initial_fen or START_FEN
        apply_history(fen, [])
        self.db.execute("""INSERT INTO games
            (id,status,red_preset,black_preset,initial_fen,current_fen,history_json,winner,reason,
             move_timeout,max_plies,ruleset_id,created_at,updated_at,benchmark_id,
             red_config_json,black_config_json,prompt_version,mode,agent_max_rounds)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (game_id,"queued",spec.red_preset_id,spec.black_preset_id,fen,fen,"[]",None,None,
             spec.move_timeout_seconds,spec.max_plies,self.arbiter.backend,stamp,stamp,benchmark_id,
             json.dumps(red_preset.model_dump(),ensure_ascii=False),
             json.dumps(black_preset.model_dump(),ensure_ascii=False),
             AGENT_PROMPT_VERSION if spec.mode=="agent" else PROMPT_VERSION,
             spec.mode,spec.agent_max_rounds))
        return game_id

    def start(self, game_id: str):
        self.tasks[game_id] = asyncio.create_task(self.run(game_id))

    def stop(self, game_id: str):
        self.db.execute("UPDATE games SET status='stopped', reason='user_stopped', updated_at=? WHERE id=? AND status IN ('queued','running')", (now(),game_id))
        task=self.tasks.get(game_id)
        if task: task.cancel()

    def is_human(self, preset_id: str) -> bool:
        try: return self.config.connection(self.config.preset(preset_id).connection_id).protocol == HUMAN_PROTOCOL
        except StopIteration: return False

    def awaiting(self, game_id: str) -> str | None:
        """Which side the runner is waiting on a human move for, if any."""
        future = self.pending.get(game_id)
        return self.pending_side.get(game_id) if future is not None and not future.done() else None

    def inspect(self, game_id: str) -> Verdict:
        game = self.db.game(game_id)
        if not game: raise ValueError("Game not found")
        return self.arbiter.inspect(game["initial_fen"], game["history"])

    def _preset_for(self, game: dict, side: str) -> Preset:
        snapshot = game.get("red_config_json" if side == "red" else "black_config_json")
        if snapshot:
            return Preset.model_validate(json.loads(snapshot))
        return self.config.preset(game["red_preset"] if side == "red" else game["black_preset"])

    def _context_header(self, game: dict) -> str:
        """Stable per-game prefix placed before the first dynamic position."""
        return ("<xiangqi_game_context>\n"
                "这是本局固定背景；后续请求会以追加方式保留本方上下文。\n"
                f"ruleset_id: {self.arbiter.backend}\n"
                f"initial_fen: {game['initial_fen']}\n"
                "公共棋谱以每次请求中的 history 为准；当前完整局面以最后的 JSON 为准。\n"
                "</xiangqi_game_context>")

    def _context_messages(self, game_id: str, side: str, game: dict) -> list[dict[str, str]]:
        rows = self.db.all("""SELECT role,content,ply,attempt,created_at FROM context_messages
            WHERE game_id=? AND side=? ORDER BY sequence""", (game_id, side))
        if len(rows) > CONTEXT_MAX_MESSAGES:
            rows = self._compact_context(game_id, side, game, rows)
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    def _compact_context(self, game_id: str, side: str, game: dict,
                         rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Bound growth while retaining a deterministic local memory summary."""
        keep = rows[-CONTEXT_KEEP_MESSAGES:]
        notes = self._private_memory(game_id, side)
        recent_history = game["history"][-32:]
        summary = ("<xiangqi_context_compaction>\n"
                   f"已压缩本方较早上下文；当前公共着数 {len(game['history'])}。\n"
                   f"最近公共坐标着法: {json.dumps(recent_history, ensure_ascii=False)}\n"
                   f"本方保留笔记: {json.dumps(notes, ensure_ascii=False)}\n"
                   "旧回复仍在棋谱记录中；当前请求的完整 pieces、history 和 legal_moves 优先。\n"
                   "</xiangqi_context_compaction>")
        with self.db.connect() as conn:
            conn.execute("DELETE FROM context_messages WHERE game_id=? AND side=?", (game_id, side))
            conn.execute("""INSERT INTO context_messages
                (game_id,side,sequence,role,content,ply,attempt,created_at)
                VALUES(?,?,?,?,?,?,?,?)""", (game_id, side, 0, "user", summary,
                                             len(game["history"]), 0, now()))
            for sequence, row in enumerate(keep, 1):
                conn.execute("""INSERT INTO context_messages
                    (game_id,side,sequence,role,content,ply,attempt,created_at)
                    VALUES(?,?,?,?,?,?,?,?)""", (game_id, side, sequence, row["role"], row["content"],
                                                 row.get("ply", len(game["history"])), row.get("attempt", 0),
                                                 row.get("created_at") or now()))
        return [{"role": "user", "content": summary}, *keep]

    def _append_context(self, game_id: str, side: str, role: str, content: str,
                        ply: int, attempt: int) -> None:
        if not content:
            return
        next_sequence = self.db.one("""SELECT COALESCE(MAX(sequence), -1) + 1 AS sequence
            FROM context_messages WHERE game_id=? AND side=?""", (game_id, side))["sequence"]
        self.db.execute("""INSERT INTO context_messages
            (game_id,side,sequence,role,content,ply,attempt,created_at)
            VALUES(?,?,?,?,?,?,?,?)""", (game_id, side, next_sequence, role, content, ply, attempt, now()))

    def _private_memory(self, game_id: str, side: str, include_notation: bool = False) -> list[dict[str, Any]]:
        rows = self.db.all("""SELECT ply,move,note,fen_before FROM moves
            WHERE game_id=? AND side=? AND error IS NULL AND move IS NOT NULL
            ORDER BY ply DESC LIMIT 1""", (game_id, side))
        memory = [{"ply": row["ply"], "move": row["move"], "note": row["note"][:80]}
                  for row in reversed(rows) if row.get("note")]
        if include_notation:
            by_ply = {row["ply"]: row for row in rows}
            for entry in memory:
                row = by_ply[entry["ply"]]
                entry["notation"] = chinese_notation(row["fen_before"], row["move"])
        return memory

    async def submit_move(self, game_id: str, move: str, expected_ply: int, expected_side: str) -> None:
        game = self.db.game(game_id)
        if not game: raise ValueError("Game not found")
        if game["status"] != "running": raise ValueError("Game is not running")
        state = apply_history(game["initial_fen"], game["history"], validate=False)
        side = state.turn
        if len(game["history"]) != expected_ply or side != expected_side:
            raise ValueError("Stale move: ply or side no longer matches")
        if not self.is_human(game["red_preset"] if side == "red" else game["black_preset"]):
            raise ValueError(f"It is {side}'s model turn, not a human turn")
        verdict = await asyncio.to_thread(self.arbiter.inspect, game["initial_fen"], game["history"])
        if move not in verdict.legal_moves:
            raise ValueError(f"Illegal move: {move}")
        future = self.pending.get(game_id)
        if future is None or future.done() or self.pending_side.get(game_id) != expected_side:
            raise ValueError("The runner is not waiting for this move")
        current = self.db.game(game_id)
        if not current or len(current["history"]) != expected_ply:
            raise ValueError("Stale move: position changed before submission")
        future.set_result(move)

    async def run(self, game_id: str):
        game=self.db.game(game_id)
        if not game: return
        self.db.execute("UPDATE games SET status='running',updated_at=? WHERE id=?",(now(),game_id))
        await self.bus.publish(game_id,{"type":"game","game_id":game_id,"status":"running"})
        try:
            while True:
                game=self.db.game(game_id)
                if not game or game["status"] != "running": return
                state=apply_history(game["initial_fen"],game["history"],validate=False)
                verdict=await asyncio.to_thread(self.arbiter.inspect,game["initial_fen"],game["history"])
                if verdict.ended:
                    await self._finish(game_id,"finished",verdict.winner,verdict.reason); return
                if len(game["history"]) >= game["max_plies"]:
                    await self._finish(game_id,"truncated",None,"max_plies"); return
                side=state.turn; preset=self._preset_for(game,side)
                legal=verdict.legal_moves
                view={"game_id":game_id,"ply":len(game["history"]),"side_to_move":side,"pieces":[p.model_dump() for p in pieces(state)],"history":game["history"],"legal_moves":legal,"in_check":verdict.in_check,"ruleset_id":verdict.backend,"fen":verdict.fen,"move_timeout_seconds":game["move_timeout"],"private_memory":self._private_memory(game_id,side)}
                if self.is_human(game["red_preset"] if side=="red" else game["black_preset"]):
                    selected, error, record = await self._human_move(game_id, side, legal, view, state, game)
                    attempt = 1
                elif game.get("mode") == "agent":
                    selected, error, attempt, record = await self._agent_move(game_id, side, preset, view, state, game)
                else:
                    selected, error, attempt, record = await self._model_move(game_id, side, preset, legal, view, state, game)
                if error:
                    if error == "cancelled": return
                    if error in {"timeout", "agent_no_submit", "agent_rounds_exhausted", "agent_output_limit"}:
                        await self._finish(game_id,"finished","black" if side=="red" else "red","timeout")
                    elif error.startswith("api_error"): await self._finish(game_id,"finished",None,"api_failure")
                    else: await self._finish(game_id,"finished","black" if side=="red" else "red","invalid_move")
                    return
                before=to_fen(state); apply_move(state,selected); after=to_fen(state); history=game["history"]+[selected]
                with self.db.connect() as conn:
                    cursor=conn.execute("UPDATE games SET current_fen=?,history_json=?,updated_at=? WHERE id=? AND status='running' AND history_json=?",(after,json.dumps(history),now(),game_id,json.dumps(game["history"])))
                    if cursor.rowcount != 1: return
                    self._record_attempt(**record, fen_after=after, conn=conn)
                await self.bus.publish(game_id,{"type":"move","game_id":game_id,"ply":len(history),"move":selected,"fen":after})
        except asyncio.CancelledError: return
        except Exception as exc:
            await self._finish(game_id,"aborted",None,f"arbiter_failure:{type(exc).__name__}")
        finally: self.tasks.pop(game_id,None)

    async def _agent_move(self, game_id, side, preset, view, state, game):
        """智能体模式的一手：模型可在同一手里连续调用规则工具，直到正式提交。

        预算按整手管理（时间、轮数、输出用量都在这一手累计）；只有 submit_move 真正落子，
        重复提交、过期 position_id、停止后的迟到提交都会被拒绝。平台不替模型选招。
        """
        ply = len(game["history"])
        try:
            self.client.resolve(preset)
        except ModelConfigError as exc:
            error = f"api_error: config: {exc}"
            record = self._attempt_record(game_id, ply, side, None, to_fen(state), view, None, 0, 1, error)
            self._record_attempt(**record)
            return None, error, 1, record
        started = time.perf_counter()
        deadline = time.monotonic() + game["move_timeout"]
        tools = self._rule_tools(game_id, side, game, expired=lambda: time.monotonic() >= deadline)
        position_message = self._agent_position_message(game, side, view, state, tools, preset)
        turn = AgentTurn(client=self.client, preset=preset, tools=tools, side=side, ply=ply,
                         deadline=deadline, position_message=position_message,
                         max_rounds=int(game.get("agent_max_rounds") or 6),
                         cancelled=lambda: self._stale(game_id, ply),
                         on_action=lambda action: self._record_action(game_id, ply, side, action))
        try:
            outcome = await turn.run()
        except asyncio.CancelledError:
            raise
        duration = int((time.perf_counter() - started) * 1000)
        self._record_actions(game_id, ply, side, outcome.actions)   # 兜底：重复行由 INSERT OR IGNORE 忽略
        error = self._agent_error(outcome)
        selected = outcome.move if not error else None
        metrics = RequestMetrics()
        rounds = [{"round": (action.args or {}).get("round"), "tools": (action.args or {}).get("tools"),
                   "max_tokens": (action.args or {}).get("max_tokens"),
                   "timeout_seconds": (action.args or {}).get("timeout_seconds"),
                   "reasoning_effort": (action.args or {}).get("reasoning_effort"),
                   "finish_reason": action.finish_reason, "duration_ms": action.duration_ms,
                   "input_tokens": action.total_input_tokens if action.total_input_tokens is not None else action.input_tokens,
                   "output_tokens": action.output_tokens, "error": action.error,
                   "sent": action.request} for action in outcome.actions if action.kind == "request"]
        metrics.actual_request = {"mode": "agent", "requests": outcome.requests,
                                  "max_rounds": turn.max_rounds,
                                  "output_token_limit": preset.max_tokens,
                                  "output_tokens_used": outcome.output_tokens,
                                  "elapsed_ms": duration, "error": error,
                                  "rounds": rounds,
                                  "actions": [{"kind": a.kind, "name": a.name} for a in outcome.actions]}
        record = self._attempt_record(game_id, ply, side, selected, to_fen(state),
                                      json.loads(position_message), None, duration, 1, error, metrics,
                                      token_override=(outcome.input_tokens, outcome.output_tokens,
                                                      outcome.cache_read_tokens, outcome.cache_write_tokens))
        if error:
            self._record_attempt(**record)     # 失败的整手自己落一行；成功的那行由 run 循环与落子同事务写入
        return selected, error, 1, record

    def _rule_tools(self, game_id: str, side: str, game: dict, expired=None) -> RuleTools:
        return RuleTools(arbiter=self.arbiter, game_id=game_id, side=side, initial_fen=game["initial_fen"],
                         history=game["history"], note_store=NoteStore(self.db, game_id, side), expired=expired)

    def _agent_position_message(self, game: dict, side: str, view: dict, state, tools: RuleTools,
                                preset: Preset) -> str:
        """每手开头自动给出的紧凑局面：完整棋子表、上一手、短笔记与预算，不需要模型先调工具读棋盘。"""
        last_move = None
        if game["history"]:
            last = game["history"][-1]
            before = apply_history(game["initial_fen"], game["history"][:-1], validate=False)
            captured = next((p.model_dump() for p in pieces(before) if p.square == last[2:]), None)
            last_move = {"move": last, "side": "black" if side == "red" else "red",
                         "notation": chinese_notation(to_fen(before), last), "captured": captured}
        payload = {
            "game_id": game["id"],
            "ply": len(game["history"]),
            "player_side": side,
            "side_to_move": view["side_to_move"],
            "position_id": tools.position_id,
            "pieces": view["pieces"],
            "in_check": view["in_check"],
            "history_length": len(game["history"]),
            "history_tail": game["history"][-8:],
            "last_move": last_move,
            "private_note": tools.note_store.read() if tools.note_store else None,
            "ruleset_id": view["ruleset_id"],
            "move_timeout_seconds": game["move_timeout"],
            "max_rounds": int(game.get("agent_max_rounds") or 6),
            "output_token_limit": preset.max_tokens,
        }
        return serialize_position(payload)

    @staticmethod
    def _agent_error(outcome) -> str | None:
        """智能体一手的失败映射：没有成功提交就记录精确原因，平台不替它挑一步棋。"""
        if outcome.move:
            return None
        if not outcome.error:
            return "agent_no_submit"
        if outcome.error.startswith("api_error"):
            return outcome.error
        return outcome.error

    def _stale(self, game_id: str, ply: int) -> bool:
        """停止、被替换或手数已经前进时，迟到的工具/提交都不再生效。"""
        current = self.db.game(game_id)
        return not current or current["status"] != "running" or len(current["history"]) != ply

    def _record_actions(self, game_id: str, ply: int, side: str, actions) -> None:
        if not actions:
            return
        with self.db.connect() as conn:
            for action in actions:
                self._insert_action(conn, game_id, ply, side, action)

    async def _record_action(self, game_id: str, ply: int, side: str, action) -> None:
        """一条行动刚落定就落盘并推事件：网页能实时看到本手的行动链条。"""
        with self.db.connect() as conn:
            self._insert_action(conn, game_id, ply, side, action)
        await self.bus.publish(game_id, {"type": "action", "game_id": game_id, "ply": ply, "side": side,
                                         "kind": action.kind, "name": action.name})

    def _insert_action(self, conn, game_id: str, ply: int, side: str, action) -> None:
        row = action.to_row()
        conn.execute("""INSERT OR IGNORE INTO agent_actions
            (game_id,ply,side,attempt,sequence,kind,name,args_json,result_json,text,duration_ms,
             input_tokens,output_tokens,total_input_tokens,cache_read_tokens,cache_write_tokens,
             connect_ms,first_byte_ms,provider_request_id,finish_reason,error,created_at,
             request_json,raw_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (game_id, ply, side, 1, action.sequence, row["kind"], row["name"], row["args_json"],
             row["result_json"], row["text"], row["duration_ms"], row["input_tokens"],
             row["output_tokens"], row["total_input_tokens"], row["cache_read_tokens"],
             row["cache_write_tokens"], row["connect_ms"], row["first_byte_ms"],
             row["provider_request_id"], row["finish_reason"], row["error"], now(),
             row["request_json"], row["raw_json"]))

    async def _human_move(self, game_id, side, legal, view, state, game):
        """Wait for a move submitted over the API; a human has a much longer budget."""
        started=time.perf_counter(); selected=None; error=None
        self.pending_side[game_id]=side
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[game_id]=future
        await self.bus.publish(game_id,{"type":"await_human","game_id":game_id,"ply":len(game["history"]),"side":side})
        try:
            selected=await asyncio.wait_for(future,self.human_timeout)
            if selected not in legal: raise ValueError(self._illegal_move_error(selected,state))
        except asyncio.TimeoutError:
            error="timeout"
        except ValueError as exc:
            error=str(exc)
        finally:
            self.pending.pop(game_id,None); self.pending_side.pop(game_id,None)
        record=self._attempt_record(game_id,len(game["history"]),side,selected if not error else None,to_fen(state),view,None,int((time.perf_counter()-started)*1000),1,error)
        if error: self._record_attempt(**record)
        return selected, error, record

    async def _model_move(self, game_id, side, preset, legal, view, state, game):
        result=None; selected=None; error=None; attempt=1
        try:
            self.client.resolve(preset)
        except ModelConfigError as exc:
            error=f"api_error: config: {exc}"
            record=self._attempt_record(game_id,len(game["history"]),side,None,to_fen(state),view,None,0,1,error)
            self._record_attempt(**record)
            return None, error, 1, record
        deadline=time.monotonic()+game["move_timeout"]
        for attempt in (1,2):
            attempt_view={key:value for key,value in view.items() if key not in {"fen", "board_rows"}}
            attempt_view["history"] = game["history"][-8:]
            attempt_view["history_start_ply"] = max(0, len(game["history"])-8)
            attempt_view["player_side"] = side
            attempt_view["last_move"] = None
            if game["history"]:
                last = game["history"][-1]
                before = apply_history(game["initial_fen"],game["history"][:-1])
                captured = next((p.model_dump() for p in pieces(before) if p.square == last[2:]), None)
                attempt_view["last_move"] = {"move":last,"side":"black" if side=="red" else "red","captured":captured}
            context=[]
            context_header=None
            current=preset
            if attempt==2:
                attempt_view["correction"]={"error":error,"instruction":"修正上述错误，只从当前 legal_moves 原样选一项，立即返回 JSON。"}
                if preset.thinking != "disabled":
                    # 推理模型可能把预算全花在思考上、正文为空；纠错时改为直接作答。
                    current=preset.model_copy(update={"thinking":"disabled","reasoning_effort":None})
                current=current.model_copy(update={"max_tokens":min(current.max_tokens,512)})
            attempt_view["output_token_limit"] = current.max_tokens
            started=time.perf_counter()
            selected=None; error=None; result=None; metrics=RequestMetrics()
            try:
                remaining=deadline-time.monotonic()
                if remaining <= 0: raise asyncio.TimeoutError
                request_deadline = time.monotonic()+first_attempt_budget(remaining) if attempt==1 else deadline
                for api_try in (1,2):
                    remaining = deadline-time.monotonic()
                    request_remaining = request_deadline-time.monotonic()
                    if request_remaining <= 0: raise asyncio.TimeoutError
                    attempt_view["remaining_timeout_seconds"] = round(max(0,remaining),3)
                    attempt_view["request_timeout_seconds"] = round(max(0,request_remaining),3)
                    try:
                        result=await asyncio.wait_for(self.client.choose(current,attempt_view,request_remaining,metrics,
                                                                         context=context,
                                                                         context_header=context_header),request_remaining); break
                    except (httpx.HTTPError, OSError) as exc:
                        if api_try==2: raise
                        remaining=deadline-time.monotonic()
                        if remaining <= 0: raise asyncio.TimeoutError from exc
                if not result.text.strip() and result.finish_reason in {"length","max_tokens","incomplete"}:
                    raise ValueError(f"Model output was cut off by max_tokens before any answer (finish_reason={result.finish_reason})")
                selected=parse_move(result.text)
                if selected not in legal: raise ValueError(self._illegal_move_error(selected,state))
            except asyncio.TimeoutError:
                error="request_budget_exhausted" if attempt==1 and deadline > time.monotonic() else "timeout"
            except (ValueError, json.JSONDecodeError) as exc:
                error=str(exc)
            except Exception as exc:
                error=f"api_error: {type(exc).__name__}: {exc}"
            if result is not None and not error:
                prompt_text = (f"{context_header}\n{serialize_position(attempt_view)}"
                               if context_header else serialize_position(attempt_view))
                self._append_context(game_id, side, "user",
                                     prompt_text,
                                     len(game["history"]), attempt)
                self._append_context(game_id, side, "assistant", self._context_response(result.text),
                                     len(game["history"]), attempt)
            duration=int((time.perf_counter()-started)*1000)
            record=self._attempt_record(game_id,len(game["history"]),side,selected if not error else None,to_fen(state),attempt_view,result,duration,attempt,error,metrics)
            if error: self._record_attempt(**record)
            if not error: break
            if error=="timeout" or error.startswith("api_error"): break
        return selected, error, attempt, record

    @staticmethod
    def _illegal_move_error(move, state):
        message = f"着法 {move} 不合法：不在当前合法着法（legal_moves）中。"
        source, target = move[:2], move[2:]
        piece = state.board.get(source)
        if not piece:
            return message+f" 起点 {source} 没有棋子。"
        if source != target and (source[0] == target[0] or source[1] == target[1]):
            dx = (ord(target[0]) > ord(source[0]))-(ord(target[0]) < ord(source[0]))
            dy = (int(target[1]) > int(source[1]))-(int(target[1]) < int(source[1]))
            x,y = ord(source[0])+dx,int(source[1])+dy
            blockers=[]
            while (x,y) != (ord(target[0]),int(target[1])):
                square=f"{chr(x)}{y}"
                if square in state.board: blockers.append(square)
                x+=dx; y+=dy
            if blockers: message += " 路径上有棋子："+", ".join(blockers)+"；车不可越子，炮移到空格不可越子，吃子须隔一个炮架。"
        return message

    @staticmethod
    def _context_response(text: str) -> str:
        """Keep the model's reusable memory compact; raw output remains in moves."""
        if not text:
            return ""
        try:
            move = parse_move(text)
        except ValueError:
            move = None
        note = parse_note(text)
        if move:
            return json.dumps({"move": move, "note": note}, ensure_ascii=False, separators=(",", ":"))
        return text[:1000]

    @staticmethod
    def _attempt_record(game_id,ply,side,move,fen,prompt,result,duration,attempt,error,metrics=None,
                        token_override=None):
        return dict(game_id=game_id,ply=ply,side=side,move=move,fen=fen,prompt=prompt,result=result,
                    duration=duration,attempt=attempt,error=error,metrics=metrics,token_override=token_override)

    def _record_attempt(self,game_id,ply,side,move,fen,prompt,result,duration,attempt,error,metrics=None,
                        fen_after=None,conn=None,token_override=None):
        """token_override 是智能体整手的累计用量 (输入, 输出, 缓存读, 缓存写)：一手多次请求只落一行。"""
        tokens = token_override or (None, None, None, None)
        total_input = result.total_input_tokens if result else tokens[0]
        output_tokens = result.output_tokens if result else tokens[1]
        cache_read = result.cache_read_tokens if result else tokens[2]
        cache_write = result.cache_write_tokens if result else tokens[3]
        # 智能体模式没有单次回复，input_tokens 记为整手累计输入，便于与直接模式同表比较。
        input_tokens = result.input_tokens if result else total_input
        values=(game_id,ply,side,move,fen,fen_after,json.dumps(prompt,ensure_ascii=False),
                result.text if result else None,json.dumps(result.raw,ensure_ascii=False) if result else None,
                duration,input_tokens,output_tokens,
                attempt,error,now(),parse_note(result.text) if result else None,
                metrics.connect_ms if metrics else None,metrics.first_byte_ms if metrics else None,
                metrics.provider_request_id if metrics else None,
                total_input,
                cache_read,
                cache_write,
                result.cache_miss_tokens if result else None,
                json.dumps(metrics.actual_request,ensure_ascii=False) if metrics and metrics.actual_request else None)
        sql="""INSERT INTO moves(game_id,ply,side,move,fen_before,fen_after,prompt_json,response_text,
               raw_json,duration_ms,input_tokens,output_tokens,attempt,error,created_at,note,
               connect_ms,first_byte_ms,provider_request_id,total_input_tokens,cache_read_tokens,
               cache_write_tokens,cache_miss_tokens,actual_request_json)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        if conn is None: self.db.execute(sql,values)
        else: conn.execute(sql,values)

    async def _finish(self,game_id,status,winner,reason):
        self.db.execute("UPDATE games SET status=?,winner=?,reason=?,updated_at=? WHERE id=?",(status,winner,reason,now(),game_id))
        await self.bus.publish(game_id,{"type":"game","game_id":game_id,"status":status,"winner":winner,"reason":reason})


class BenchmarkRunner:
    def __init__(self,db:Database,games:GameRunner,bus:EventBus,config:ConfigStore): self.db=db; self.games=games; self.bus=bus; self.config=config; self.tasks={}
    def create(self,spec:BenchmarkCreate)->str:
        for preset_id in (spec.preset_a_id,spec.preset_b_id):
            if self.games.is_human(preset_id):
                raise ValueError("Benchmarks require model presets; a human side cannot be scored")
        bench_id=uuid.uuid4().hex; stamp=now()
        self.db.execute("INSERT INTO benchmarks VALUES(?,?,?,?,?,?,?,?)",(bench_id,"queued",spec.preset_a_id,spec.preset_b_id,spec.pairs,json.dumps(spec.model_dump()),stamp,stamp))
        self.tasks[bench_id]=asyncio.create_task(self.run(bench_id,spec)); return bench_id
    async def run(self,bench_id,spec):
        self.db.execute("UPDATE benchmarks SET status='running',updated_at=? WHERE id=?",(now(),bench_id))
        try:
            for _ in range(spec.pairs):
                for red,black in ((spec.preset_a_id,spec.preset_b_id),(spec.preset_b_id,spec.preset_a_id)):
                    red_effort = spec.preset_a_reasoning_effort if red == spec.preset_a_id else spec.preset_b_reasoning_effort
                    black_effort = spec.preset_a_reasoning_effort if black == spec.preset_a_id else spec.preset_b_reasoning_effort
                    game_id=self.games.create(GameCreate(red_preset_id=red,black_preset_id=black,
                        initial_fen=spec.initial_fen,move_timeout_seconds=spec.move_timeout_seconds,
                        max_plies=spec.max_plies,red_reasoning_effort=red_effort,
                        black_reasoning_effort=black_effort,mode=spec.mode,
                        agent_max_rounds=spec.agent_max_rounds),bench_id)
                    self.games.start(game_id)
                    while (game:=self.db.game(game_id)) and game["status"] in {"queued","running"}: await asyncio.sleep(.1)
                    await self.bus.publish(bench_id,{"type":"benchmark_progress","game_id":game_id})
            self.db.execute("UPDATE benchmarks SET status='finished',updated_at=? WHERE id=?",(now(),bench_id))
        except asyncio.CancelledError: return
        except Exception:
            self.db.execute("UPDATE benchmarks SET status='aborted',updated_at=? WHERE id=?",(now(),bench_id))

    def report(self,bench_id):
        bench=self.db.one("SELECT * FROM benchmarks WHERE id=?",(bench_id,))
        if not bench: return None
        games=self.db.all("SELECT * FROM games WHERE benchmark_id=? ORDER BY created_at",(bench_id,))
        moves=self.db.all("SELECT m.* FROM moves m JOIN games g ON g.id=m.game_id WHERE g.benchmark_id=? ORDER BY m.game_id,m.ply,m.attempt",(bench_id,))
        scored=[g for g in games if g["status"]=="finished" and g["reason"] not in UNSCORED_REASONS]
        first=[m for m in moves if m["attempt"]==1]; second=[m for m in moves if m["attempt"]==2]
        model_results={preset_id:_model_result(preset_id,scored)
                       for preset_id in (bench["preset_a"],bench["preset_b"])}
        durations=[_game_duration_ms(g) for g in games]
        stats={
            "games":len(games),"decided":len(scored),
            "red_wins":sum(g["winner"]=="red" for g in scored),
            "black_wins":sum(g["winner"]=="black" for g in scored),
            "draws":sum(g["winner"] is None for g in scored),
            "forfeit_wins":sum(g["reason"] in FORFEIT_REASONS for g in games),
            "api_failures":sum(g["reason"]=="api_failure" for g in games),
            "api_failure_rate":_ratio(sum(g["reason"]=="api_failure" for g in games),len(games)),
            "truncated":sum(g["status"]=="truncated" for g in games),
            "stopped":sum(g["status"]=="stopped" for g in games),
            "interrupted":sum(g["status"] in {"interrupted","aborted"} for g in games),
            "first_answer_legal_rate":_ratio(sum(not m["error"] for m in first),len(first)),
            "correction_success_rate":_ratio(sum(not m["error"] for m in second),len(second)),
            "average_move_ms":round(sum(m["duration_ms"] or 0 for m in moves)/len(moves),1) if moves else None,
            "average_game_ms":round(sum(durations)/len(durations),1) if durations else None,
            "input_tokens":_sum_known(moves, "input_tokens"),
            "output_tokens":_sum_known(moves, "output_tokens"),
            "total_input_tokens":_sum_known(moves, "total_input_tokens"),
            "cache_read_tokens":_sum_known(moves, "cache_read_tokens"),
            "cache_write_tokens":_sum_known(moves, "cache_write_tokens"),
            "cache_miss_tokens":_sum_known(moves, "cache_miss_tokens"),
        }
        stats["cache_hit_rate"] = _ratio(stats["cache_read_tokens"], stats["total_input_tokens"]) \
            if stats["cache_read_tokens"] is not None and stats["total_input_tokens"] is not None else None
        settings=json.loads(bench.pop("settings_json"))
        return {**bench,"settings":settings,"stats":stats,"model_results":model_results,
                "usage":_usage(games,moves,self.config),"games":games}

def _usage(games:list[dict],moves:list[dict],config:ConfigStore)->list[dict]:
    by_game={g["id"]:g for g in games}; totals:dict[str,dict[str,Any]]={}
    for move in moves:
        game=by_game.get(move["game_id"])
        if not game: continue
        preset_id=game["red_preset"] if move["side"]=="red" else game["black_preset"]
        entry=totals.setdefault(preset_id,{"preset_id":preset_id,"moves":0,"attempts":0,
            "input_tokens":0,"output_tokens":0,"total_input_tokens":0,"cache_read_tokens":0,
            "cache_write_tokens":0,"cache_miss_tokens":0,"duration_ms":0,
            "input_known":True,"output_known":True,"total_input_known":True,
            "cache_read_known":True,"cache_write_known":True,"cache_miss_known":True})
        entry["attempts"]+=1
        if move["move"] and not move["error"]: entry["moves"]+=1
        if move["input_tokens"] is None: entry["input_known"]=False
        else: entry["input_tokens"]+=move["input_tokens"]
        if move["output_tokens"] is None: entry["output_known"]=False
        else: entry["output_tokens"]+=move["output_tokens"]
        for key in ("total_input_tokens", "cache_read_tokens", "cache_write_tokens", "cache_miss_tokens"):
            if move.get(key) is None: entry[f"{key[:-6]}known"]=False
            else: entry[key]+=move[key]
        entry["duration_ms"]+=move["duration_ms"] or 0
    for preset_id,entry in totals.items():
        preset=_snapshot_preset(games,preset_id)
        if not entry.pop("input_known"): entry["input_tokens"]=None
        if not entry.pop("output_known"): entry["output_tokens"]=None
        for key in ("total_input_tokens", "cache_read_tokens", "cache_write_tokens", "cache_miss_tokens"):
            if not entry.pop(f"{key[:-6]}known"): entry[key]=None
        entry["cache_hit_rate"]=_ratio(entry["cache_read_tokens"],entry["total_input_tokens"])
        if preset and entry["input_tokens"] is not None and entry["output_tokens"] is not None and preset.input_price_per_million is not None and preset.output_price_per_million is not None:
            entry["cost_estimate"]=round(entry["input_tokens"]/1_000_000*preset.input_price_per_million+entry["output_tokens"]/1_000_000*preset.output_price_per_million,6)
        else:
            entry["cost_estimate"]=None
    return sorted(totals.values(),key=lambda item:item["preset_id"])


def _ratio(a,b): return round(a/b,4) if a is not None and b else None


def _sum_known(items: list[dict], key: str) -> int | None:
    values=[item.get(key) for item in items]
    return sum(values) if values and all(value is not None for value in values) else None


def _snapshot_preset(games:list[dict],preset_id:str)->Preset|None:
    for game in games:
        for side in ("red","black"):
            if game.get(f"{side}_preset") == preset_id and game.get(f"{side}_config_json"):
                return Preset.model_validate(json.loads(game[f"{side}_config_json"]))
    return None


def _model_result(preset_id:str,games:list[dict])->dict[str,Any]:
    wins=losses=draws=0
    for game in games:
        for side in ("red","black"):
            if game[f"{side}_preset"]!=preset_id: continue
            if game["winner"] is None: draws+=1
            elif game["winner"]==side: wins+=1
            else: losses+=1
    total=wins+losses+draws
    return {"wins":wins,"losses":losses,"draws":draws,"games":total,
            "score_rate":round((wins+draws*.5)/total,4) if total else None}


def _game_duration_ms(game:dict)->int:
    start=datetime.fromisoformat(game["created_at"].replace("Z","+00:00"))
    end=datetime.fromisoformat(game["updated_at"].replace("Z","+00:00"))
    return max(0,int((end-start).total_seconds()*1000))
