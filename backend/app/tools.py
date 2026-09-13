"""本手可用的规则工具。

设计约束（与项目既有约定一致）：
- 工具只回答规则事实：不给评分、不给推荐、不替模型展开搜索；试走只执行模型自己提出的变化。
- 每个工具都绑定本局、本方与当前手数：读不到对方笔记，也改不了真实棋盘。
- 试走保留真实对局历史，因为三次重复、长将长捉等判定与历史有关，只复制当前 FEN 不够。
- 只有 submit_move 会真正落子；它再次校验，并拒绝重复提交、过期提交和超时提交。

坐标固定红方视角 a0—i9，与提示词一致。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .rules import State, apply_history, apply_move, chinese_notation, in_check, piece_targets, pieces, to_fen

SQUARE = re.compile(r"^[a-i][0-9]$")
MOVE = re.compile(r"^[a-i][0-9][a-i][0-9]$")
NOTE_LIMIT = 400
HISTORY_WINDOW_LIMIT = 40

# 走法不符合棋子规则时的说法，直接对着走子规则讲，不给评分或建议。
PIECE_HINTS: dict[str, str] = {
    "R": "车只能沿直线走，路径上不能有棋子。",
    "N": "马走日字，且马腿（相邻直线格）不能被占住。",
    "C": "炮沿直线移动；吃子必须正好隔一个棋子（炮架）。",
    "B": "相/象走田字，不能过河，象眼（田字中心）不能被占住。",
    "A": "仕/士只能在九宫内斜走一格。",
    "K": "帅/将只能在九宫内直走一格，且不能与对方将帅照面。",
    "P": "兵/卒只能向前一格；过河后可以向前或横向一格，不能后退。",
}


def piece_hint(piece: str) -> str:
    return PIECE_HINTS.get(piece.upper(), "这一步不符合该棋子的走法。")


def illegal_move_reason(state, move: str) -> str:
    """把一步棋为什么非法说清楚：起点无子、路径阻挡、炮架、马腿、自将等。"""
    message = f"着法 {move} 不合法：不在当前合法着法（legal_moves）中。"
    if not MOVE.match(move or ""):
        return message + " 着法格式应为起点+终点，例如 b2e2。"
    source, target = move[:2], move[2:]
    piece = state.board.get(source)
    if not piece:
        return message + f" 起点 {source} 没有棋子。"
    if source == target:
        return message + " 起点与终点相同。"
    if ("red" if piece.isupper() else "black") != state.turn:
        return message + f" 起点 {source} 不是行棋方的棋子。"
    probe = State(board=dict(state.board), turn=state.turn, halfmove=state.halfmove, fullmove=state.fullmove)
    try:
        apply_move(probe, move)
    except ValueError as exc:
        return message + f" {exc}"
    if source[0] == target[0] or source[1] == target[1]:
        dx = (ord(target[0]) > ord(source[0])) - (ord(target[0]) < ord(source[0]))
        dy = (int(target[1]) > int(source[1])) - (int(target[1]) < int(source[1]))
        x, y = ord(source[0]) + dx, int(source[1]) + dy
        blockers = []
        while (x, y) != (ord(target[0]), int(target[1])):
            square = f"{chr(x)}{y}"
            if square in state.board:
                blockers.append(square)
            x += dx
            y += dy
        if blockers:
            return message + " 路径上有棋子：" + ", ".join(blockers) + "；车不可越子，炮移到空格不可越子，吃子须隔一个炮架。"
    if move not in piece_targets(state, source):
        return message + f" 这不符合走子规则：{piece_hint(piece)}"
    # 走法本身符合规则，但仍不在 legal_moves 里：只能是自将或将帅照面。
    if in_check(probe, state.turn):
        return message + " 这一步会让自己的将/帅被将军（含将帅照面），所以不能走。"
    return message


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


@dataclass
class ToolResult:
    """一个工具调用的返回值；submitted 只在 submit_move 被接受时出现。"""

    ok: bool
    payload: dict[str, Any]
    submitted: str | None = None
    text: str = ""

    def render(self) -> str:
        return self.text or json.dumps(self.payload, ensure_ascii=False, separators=(",", ":"))


@dataclass
class SubmitState:
    """整手共享的提交状态：保证一手最多生效一次落子。"""

    submitted: str | None = None
    attempts: int = 0
    rejected: list[str] = field(default_factory=list)


class RuleTools:
    """绑定一局、一方、一手。同步实现，调用方用 to_thread 执行以免堵住事件循环。"""

    def __init__(self, *, arbiter, game_id: str, side: str, initial_fen: str, history: list[str],
                 note_store: "NoteStore | None" = None, position_id: str | None = None,
                 submit_state: SubmitState | None = None, expired: Callable[[], bool] | None = None):
        self.arbiter = arbiter
        self.game_id = game_id
        self.side = side
        self.initial_fen = initial_fen
        self.history = list(history)
        self.ply = len(self.history)
        self.note_store = note_store
        self.position_id = position_id or f"{game_id[:8]}-{self.ply}-{_short_hash(initial_fen + '|' + ','.join(self.history))}"
        self.submit_state = submit_state or SubmitState()
        # 整手截止时间：落子本身也要看期限，不能只在循环里检查。
        self.expired = expired or (lambda: False)

    # ---- 内部 ----

    def _state(self):
        return apply_history(self.initial_fen, self.history, validate=False)

    def _legal(self) -> list[str]:
        return self.arbiter.inspect(self.initial_fen, self.history).legal_moves

    def _view(self) -> dict[str, Any]:
        state = self._state()
        verdict = self.arbiter.inspect(self.initial_fen, self.history)
        return {"state": state, "verdict": verdict, "legal": verdict.legal_moves}

    @staticmethod
    def _notation(fen_before: str, move: str) -> str | None:
        try:
            return chinese_notation(fen_before, move)
        except Exception:  # noqa: BLE001 - 记法失败不影响规则事实
            return None

    # ---- 工具实现 ----

    def get_position(self) -> ToolResult:
        view = self._view()
        state = view["state"]
        note = self.note_store.read() if self.note_store else None
        return ToolResult(True, {
            "position_id": self.position_id,
            "game_id": self.game_id,
            "ply": self.ply,
            "side_to_move": state.turn,
            "in_check": view["verdict"].in_check,
            "pieces": [p.model_dump() for p in pieces(state)],
            "history_length": self.ply,
            "legal_move_count": len(view["legal"]),
            "ruleset_id": view["verdict"].backend,
            "private_note": note,
            "note": "棋盘事实以本回复为准；private_note 是你自己写的判断，可能过时。",
        })

    def get_legal_moves(self, source: str | None = None) -> ToolResult:
        """全部合法着法，或某一枚棋子的合法着法（工具定义里这个参数叫 from）。"""
        view = self._view()
        legal = view["legal"]
        if source:
            if not SQUARE.match(source):
                return ToolResult(False, {"error": f"from={source!r} 不是合法坐标，应为 a0—i9。"})
            legal = [move for move in legal if move.startswith(source)]
        return ToolResult(True, {
            "position_id": self.position_id,
            "from": source,
            "count": len(legal),
            "moves": legal,
            "note": "按坐标排序，不含评分或推荐。",
        })

    def check_move(self, move: str) -> ToolResult:
        view = self._view()
        if move in view["legal"]:
            return ToolResult(True, {"position_id": self.position_id, "move": move, "legal": True})
        return ToolResult(True, {
            "position_id": self.position_id,
            "move": move,
            "legal": False,
            "reason": illegal_move_reason(view["state"], move),
            "note": "非法只说明这一步不成立；可以改试其它候选。",
        })

    def simulate_line(self, moves: list[str]) -> ToolResult:
        if not isinstance(moves, list) or not moves:
            return ToolResult(False, {"error": "moves 需要是非空的着法数组，例如 [\"b2e2\",\"b9c7\"]。"})
        if len(moves) > 12:
            return ToolResult(False, {"error": "一次最多试走 12 步。"})
        line_history = list(self.history)
        steps: list[dict[str, Any]] = []
        previous_fen = to_fen(self._state())
        for index, move in enumerate(moves):
            if not isinstance(move, str) or not MOVE.match(move):
                steps.append({"index": index, "move": move, "legal": False, "reason": "着法格式应为 a0i9 这样的四字符坐标。"})
                break
            verdict = self.arbiter.inspect(self.initial_fen, line_history)
            if move not in verdict.legal_moves:
                state = apply_history(self.initial_fen, line_history, validate=False)
                steps.append({"index": index, "move": move, "legal": False,
                              "reason": illegal_move_reason(state, move)})
                break
            captured = next((p.model_dump() for p in pieces(apply_history(self.initial_fen, line_history, validate=False))
                             if p.square == move[2:]), None)
            line_history.append(move)
            state = apply_history(self.initial_fen, line_history, validate=False)
            after = self.arbiter.inspect(self.initial_fen, line_history)
            steps.append({
                "index": index,
                "move": move,
                "notation": self._notation(previous_fen, move),
                "legal": True,
                "side": "red" if (self.ply + index) % 2 == 0 else "black",
                "captured": captured,
                "in_check": after.in_check,
            })
            previous_fen = to_fen(state)
            if after.ended:
                return ToolResult(True, {
                    "position_id": self.position_id,
                    "steps": steps,
                    "ended": True,
                    "winner": after.winner,
                    "reason": after.reason,
                    "note": "这是试走，没有改动真实棋局；判定按真实历史加你提出的变化计算。",
                })
        final_state = apply_history(self.initial_fen, line_history, validate=False)
        return ToolResult(True, {
            "position_id": self.position_id,
            "steps": steps,
            "ended": False,
            "fen_after": to_fen(final_state),
            "in_check": self.arbiter.inspect(self.initial_fen, line_history).in_check,
            "note": "这是试走，没有改动真实棋局；判定按真实历史加你提出的变化计算。",
        })

    def get_history(self, start: int = 0, end: int | None = None) -> ToolResult:
        total = self.ply
        first = max(0, min(int(start or 0), total))
        last = total if end is None else max(first, min(int(end), total))
        if last - first > HISTORY_WINDOW_LIMIT:
            first = last - HISTORY_WINDOW_LIMIT
        state = apply_history(self.initial_fen, self.history[:first], validate=False)
        rows = []
        for index in range(first, last):
            move = self.history[index]
            before = to_fen(state)
            apply_move(state, move)
            rows.append({"ply": index + 1, "side": "red" if index % 2 == 0 else "black",
                         "move": move, "notation": self._notation(before, move)})
        return ToolResult(True, {
            "start_ply": first + 1 if last > first else 0,
            "end_ply": last,
            "total_plies": total,
            "moves": rows,
            "note": "这是真实棋谱；完整局面仍以 get_position 为准。",
        })

    def read_note(self) -> ToolResult:
        if not self.note_store:
            return ToolResult(False, {"error": "本局没有启用笔记。"})
        return ToolResult(True, {"private_note": self.note_store.read(), "limit": NOTE_LIMIT})

    def write_note(self, text: str) -> ToolResult:
        if not self.note_store:
            return ToolResult(False, {"error": "本局没有启用笔记。"})
        if not isinstance(text, str):
            return ToolResult(False, {"error": "text 需要是字符串。"})
        cleaned = text.strip()
        if len(cleaned) > NOTE_LIMIT:
            return ToolResult(False, {"error": f"笔记最多 {NOTE_LIMIT} 字，当前 {len(cleaned)} 字。"}, text="")
        stored = self.note_store.write(cleaned, self.ply)
        return ToolResult(True, {"private_note": stored, "note": "已替换旧笔记；它只是你自己的判断，棋盘事实仍以 get_position 为准。"})

    def submit_move(self, move: str, position_id: str | None = None) -> ToolResult:
        """正式落子：再次校验，并拒绝重复提交、过期提交与超时提交。"""
        if self.expired():
            # 期限已过：不算一次「非法提交」，也不占用纠错机会，整手按超时处理。
            return ToolResult(False, {"accepted": False, "move": move, "expired": True,
                                      "reason": "整手时限已过，这一步不再受理。"})
        self.submit_state.attempts += 1
        if self.submit_state.submitted:
            reason = f"本手已经落子 {self.submit_state.submitted}，重复提交被忽略。"
            self.submit_state.rejected.append(reason)
            return ToolResult(False, {"accepted": False, "move": move, "reason": reason})
        if position_id and position_id != self.position_id:
            reason = (f"position_id 过期：你提交的是 {position_id}，当前是 {self.position_id}。"
                      "请用最新的 get_position 结果重新确认局面。")
            self.submit_state.rejected.append(reason)
            return ToolResult(False, {"accepted": False, "move": move, "reason": reason})
        if not isinstance(move, str) or not MOVE.match(move):
            reason = "着法格式应为起点+终点，例如 b2e2。"
            self.submit_state.rejected.append(reason)
            return ToolResult(False, {"accepted": False, "move": move, "reason": reason})
        view = self._view()
        if move not in view["legal"]:
            reason = illegal_move_reason(view["state"], move)
            self.submit_state.rejected.append(reason)
            return ToolResult(False, {"accepted": False, "move": move,
                                      "reason": reason + " 这是正式提交，请核对后再提交一次合法着法。"})
        self.submit_state.submitted = move
        return ToolResult(True, {"accepted": True, "move": move, "notation": self._notation(to_fen(view["state"]), move),
                                 "note": "已正式落子，本手结束。"},
                          submitted=move, text=json.dumps({"accepted": True, "move": move}, ensure_ascii=False))

    def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """按工具名分发；未知工具名返回可读错误，不抛异常打断整手。"""
        handlers: dict[str, Callable[..., ToolResult]] = {
            "get_position": self.get_position,
            "get_legal_moves": self.get_legal_moves,
            "check_move": self.check_move,
            "simulate_line": self.simulate_line,
            "get_history": self.get_history,
            "read_note": self.read_note,
            "write_note": self.write_note,
            "submit_move": self.submit_move,
        }
        handler = handlers.get(name)
        if handler is None:
            return ToolResult(False, {"error": f"未知工具 {name}；可用工具：{', '.join(handlers)}。"})
        try:
            return handler(**_normalise_arguments(name, arguments))
        except TypeError as exc:
            return ToolResult(False, {"error": f"{name} 参数不匹配：{exc}。请按工具定义传参。"})


def _normalise_arguments(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """工具参数容错：定义里写 `from`，实现里叫 `source`；模型也常把数组传成单个字符串。

    只做等价改写的容错，不改语义：真正的非法参数仍然会以 TypeError 报回去。
    """
    if not isinstance(arguments, dict):
        return {}
    normalised = dict(arguments)
    if name == "get_legal_moves":
        for alias in ("from", "square", "piece", "origin"):
            if alias in normalised and "source" not in normalised:
                normalised["source"] = normalised.pop(alias)
    if name == "simulate_line" and isinstance(normalised.get("moves"), str):
        normalised["moves"] = [normalised["moves"]]
    if name == "get_history":
        for key in ("start", "end"):
            if isinstance(normalised.get(key), str) and normalised[key].lstrip("-").isdigit():
                normalised[key] = int(normalised[key])
    if name == "write_note" and isinstance(normalised.get("text"), (int, float)):
        normalised["text"] = str(normalised["text"])
    return normalised


class NoteStore:
    """本方短笔记：一手最多一条，写入即替换；只属于写它的那一方。"""

    def __init__(self, db, game_id: str, side: str):
        self.db = db
        self.game_id = game_id
        self.side = side

    def read(self) -> dict[str, Any] | None:
        row = self.db.one("SELECT text,ply,updated_at FROM agent_notes WHERE game_id=? AND side=?",
                          (self.game_id, self.side))
        return dict(row) if row else None

    def write(self, text: str, ply: int) -> dict[str, Any]:
        from datetime import datetime, timezone
        stamp = datetime.now(timezone.utc).isoformat()
        self.db.execute("""INSERT INTO agent_notes(game_id,side,text,ply,updated_at) VALUES(?,?,?,?,?)
            ON CONFLICT(game_id,side) DO UPDATE SET text=excluded.text,ply=excluded.ply,updated_at=excluded.updated_at""",
                        (self.game_id, self.side, text, ply, stamp))
        return {"text": text, "ply": ply, "updated_at": stamp}


# 工具定义：三种协议共用同一份 JSON Schema，转换在各适配层完成。
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "get_position",
        "description": "查询完整当前局面：棋子位置表、行棋方、将军状态、position_id 和本方短笔记。棋盘事实以它的返回为准。",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "get_legal_moves",
        "description": "查询当前全部合法走法，或某一枚棋子（from）的合法走法。按坐标排序，不含评分或推荐。",
        "parameters": {"type": "object", "properties": {
            "from": {"type": "string", "description": "可选，棋子所在坐标，例如 b2。不填则返回全部合法走法。"},
        }, "required": [], "additionalProperties": False},
    },
    {
        "name": "check_move",
        "description": "检查一步棋是否合法，并说明不合法的具体原因（起点无子、路径阻挡、炮架、马腿等）。它不会落子。",
        "parameters": {"type": "object", "properties": {
            "move": {"type": "string", "description": "四字符着法，例如 b2e2。"},
        }, "required": ["move"], "additionalProperties": False},
    },
    {
        "name": "simulate_line",
        "description": "在棋局副本中执行你提出的变化，返回每一步是否合法、吃子、将军与终局判定。不返回评分或推荐，也不改动真实棋局。",
        "parameters": {"type": "object", "properties": {
            "moves": {"type": "array", "items": {"type": "string"}, "description": "你提出的着法序列，最多 12 步。"},
        }, "required": ["moves"], "additionalProperties": False},
    },
    {
        "name": "get_history",
        "description": "查询真实棋谱的某一段（含中文记法）。start 是起始半回合，end 是结束半回合。",
        "parameters": {"type": "object", "properties": {
            "start": {"type": "integer", "description": "起始半回合（含），从 0 开始。"},
            "end": {"type": "integer", "description": "结束半回合（不含）；不填表示到最后。"},
        }, "required": [], "additionalProperties": False},
    },
    {
        "name": "read_note",
        "description": "读取你上次写给自己的短笔记（最多 400 字）。",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "write_note",
        "description": "用新内容替换你的短笔记（最多 400 字）。笔记只属于你，记录手数；它不是平台事实，每手仍以 get_position 为准。",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "新的笔记内容，最多 400 字。"},
        }, "required": ["text"], "additionalProperties": False},
    },
    {
        "name": "submit_move",
        "description": "正式提交本手走法并结束本手。只调用一次；重复提交或使用过期 position_id 会被拒绝。",
        "parameters": {"type": "object", "properties": {
            "move": {"type": "string", "description": "四字符着法，例如 b2e2。"},
            "position_id": {"type": "string", "description": "最近一次 get_position 返回的 position_id。"},
        }, "required": ["move"], "additionalProperties": False},
    },
]

TOOL_NAMES = [spec["name"] for spec in TOOL_SPECS]
SUBMIT_ONLY = [spec for spec in TOOL_SPECS if spec["name"] == "submit_move"]
