from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re
from typing import Iterable

from .schemas import Piece

START_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"
RULESET_ID = "xiangqi-bench-rules-1.0"
MOVE_RE = re.compile(r"^[a-i][0-9][a-i][0-9]$")

PIECE_TYPES = {
    "r": "rook", "a": "advisor", "c": "cannon", "p": "pawn",
    "n": "knight", "b": "bishop", "k": "king",
}
CHINESE_PIECES = {"r": "车", "n": "马", "b": "相", "a": "仕", "k": "帅", "c": "炮", "p": "兵"}
BLACK_CHINESE_PIECES = {"r": "车", "n": "马", "b": "象", "a": "士", "k": "将", "c": "炮", "p": "卒"}
CHINESE_NUMBERS = "零一二三四五六七八九"


@dataclass(slots=True)
class State:
    board: dict[str, str]
    turn: str
    halfmove: int = 0
    fullmove: int = 1


def parse_fen(fen: str) -> State:
    parts = fen.split()
    if len(parts) < 2:
        raise ValueError("FEN must include board and side to move")
    ranks = parts[0].split("/")
    if len(ranks) != 10:
        raise ValueError("Xiangqi FEN must contain 10 ranks")
    board: dict[str, str] = {}
    valid = set("racpnbkRACPNBK")
    for row, rank in enumerate(ranks):
        file_index = 0
        for token in rank:
            if token.isdigit():
                file_index += int(token)
            elif token in valid and file_index < 9:
                board[f"{chr(97 + file_index)}{9 - row}"] = token
                file_index += 1
            else:
                raise ValueError(f"Invalid FEN token: {token}")
        if file_index != 9:
            raise ValueError("Every FEN rank must contain 9 files")
    if sum(piece == "K" for piece in board.values()) != 1 or sum(piece == "k" for piece in board.values()) != 1:
        raise ValueError("Position must contain exactly one king per side")
    if parts[1] not in {"w", "b"}:
        raise ValueError("FEN side to move must be w or b")
    try:
        halfmove = int(parts[4]) if len(parts) > 4 else 0
        fullmove = int(parts[5]) if len(parts) > 5 else 1
    except ValueError as exc:
        raise ValueError("FEN move counters must be integers") from exc
    if halfmove < 0 or fullmove < 1:
        raise ValueError("FEN move counters are out of range")
    return State(board, "red" if parts[1] == "w" else "black", halfmove, fullmove)


def to_fen(state: State) -> str:
    ranks: list[str] = []
    for rank in range(9, -1, -1):
        cells: list[str] = []
        empty = 0
        for file_index in range(9):
            piece = state.board.get(_sq(file_index, rank))
            if piece:
                if empty:
                    cells.append(str(empty)); empty = 0
                cells.append(piece)
            else:
                empty += 1
        if empty:
            cells.append(str(empty))
        ranks.append("".join(cells))
    side = "w" if state.turn == "red" else "b"
    return f"{'/'.join(ranks)} {side} - - {state.halfmove} {state.fullmove}"


def pieces(state: State) -> list[Piece]:
    return [Piece(side=_side(p), type=PIECE_TYPES[p.lower()], square=s) for s, p in sorted(state.board.items())]


def chinese_notation(fen_before: str, move: str) -> str:
    """Convert an internal coordinate move to standard four-character notation."""
    state = parse_fen(fen_before)
    if not MOVE_RE.fullmatch(move) or move[:2] not in state.board:
        raise ValueError(f"Cannot notate move: {move}")
    origin, target = move[:2], move[2:]
    piece = state.board[origin]
    side = _side(piece)
    names = CHINESE_PIECES if side == "red" else BLACK_CHINESE_PIECES
    name = names[piece.lower()]
    ox, oy = _xy(origin)
    tx, ty = _xy(target)
    prefix = _notation_prefix(state, origin, piece, side, name)
    if ty == oy:
        return prefix + "平" + _file_number(tx, side)
    action = "进" if (ty > oy) == (side == "red") else "退"
    if piece.lower() in {"n", "b", "a"}:
        suffix = _file_number(tx, side)
    else:
        suffix = CHINESE_NUMBERS[abs(ty - oy)]
    return prefix + action + suffix


def _file_number(file_index: int, side: str) -> str:
    return CHINESE_NUMBERS[9 - file_index if side == "red" else file_index + 1]


def _notation_prefix(state: State, origin: str, piece: str, side: str, name: str) -> str:
    same_file = _front_to_back(
        [square for square, candidate in state.board.items()
         if candidate == piece and square[0] == origin[0]], side)
    # Advisors and bishops on one file remain distinguishable by whether the
    # recorded action is advance or retreat, so traditional notation keeps
    # their piece name and file number.
    if len(same_file) == 1 or piece.lower() in {"a", "b"}:
        return name + _file_number(_xy(origin)[0], side)

    index = same_file.index(origin)
    if piece.lower() == "p":
        crowded_files: list[list[str]] = []
        for file_index in range(9):
            group = _front_to_back(
                [square for square, candidate in state.board.items()
                 if candidate == piece and _xy(square)[0] == file_index], side)
            if len(group) > 1:
                crowded_files.append(group)
        if len(crowded_files) > 1:
            # Across multiple crowded files, number from the mover's right to
            # left (file 1 to 9), and front to rear within each file.
            crowded_files.sort(key=lambda group: 9 - _xy(group[0])[0]
                               if side == "red" else _xy(group[0])[0] + 1)
            ordered = [square for group in crowded_files for square in group]
            return CHINESE_NUMBERS[ordered.index(origin) + 1] + name
        if len(same_file) >= 4:
            return CHINESE_NUMBERS[index + 1] + name
    return _position_prefix(index, len(same_file)) + name


def _front_to_back(squares: list[str], side: str) -> list[str]:
    return sorted(squares, key=lambda square: _xy(square)[1], reverse=side == "red")


def _position_prefix(index: int, count: int) -> str:
    if index == 0:
        return "前"
    if index == count - 1:
        return "后"
    if count == 3:
        return "中"
    if count == 5 and index == 2:
        return "中"
    return CHINESE_NUMBERS[index + 1]


def apply_history(initial_fen: str, history: Iterable[str], validate: bool = True) -> State:
    """Replay a move list from the initial position.

    ``validate`` checks every move against the generator; callers that already
    hold an arbiter verdict can pass ``False`` to replay in linear time.
    """
    state = parse_fen(initial_fen)
    for move in history:
        if validate and move not in legal_moves(state):
            raise ValueError(f"Illegal move in history: {move}")
        apply_move(state, move)
    return state


def apply_move(state: State, move: str) -> None:
    if not MOVE_RE.fullmatch(move):
        raise ValueError(f"Malformed move: {move}")
    origin, target = move[:2], move[2:]
    if origin == target:
        raise ValueError(f"Move must change squares: {move}")
    if origin not in state.board:
        raise ValueError(f"No piece on {origin}")
    moving = state.board.pop(origin)
    if _side(moving) != state.turn:
        raise ValueError(f"{moving} on {origin} does not belong to {state.turn}")
    captured = state.board.pop(target, None)
    state.board[target] = moving
    state.halfmove = 0 if captured or moving.lower() == "p" else state.halfmove + 1
    if state.turn == "black":
        state.fullmove += 1
    state.turn = "black" if state.turn == "red" else "red"


def legal_moves(state: State) -> list[str]:
    result: list[str] = []
    for origin, piece in state.board.items():
        if _side(piece) != state.turn:
            continue
        for target in _pseudo_targets(state, origin, piece):
            clone = State(dict(state.board), state.turn, state.halfmove, state.fullmove)
            apply_move(clone, origin + target)
            if not in_check(clone, state.turn):
                result.append(origin + target)
    return sorted(result)


def in_check(state: State, side: str | None = None) -> bool:
    side = side or state.turn
    king = next((sq for sq, p in state.board.items() if p == ("K" if side == "red" else "k")), None)
    if king is None:
        return True
    enemy = "black" if side == "red" else "red"
    return any(king in _pseudo_targets(state, sq, p, attacks=True) for sq, p in state.board.items() if _side(p) == enemy)


def terminal(state: State) -> tuple[bool, str | None, str | None]:
    moves = legal_moves(state)
    if moves:
        return False, None, None
    winner = "black" if state.turn == "red" else "red"
    return True, winner, "checkmate" if in_check(state) else "stalemate"


@dataclass(slots=True)
class RuleResult:
    state: State
    legal_moves: list[str]
    in_check: bool
    ended: bool
    winner: str | None
    reason: str | None
    source: str


@dataclass(slots=True)
class MoveEvent:
    side: str
    gives_check: bool
    chased: frozenset[int]


@dataclass(slots=True)
class Replay:
    state: State
    identities: dict[str, int]
    positions: tuple[tuple, ...]
    events: tuple[MoveEvent, ...]


def adjudicate(initial_fen: str, history: list[str]) -> RuleResult:
    """Replay and adjudicate a complete game using the bench's own ruleset."""
    replay = _replay_full(initial_fen, tuple(history)) if len(history) > 800 else _replay(initial_fen, tuple(history))
    state = State(dict(replay.state.board), replay.state.turn,
                  replay.state.halfmove, replay.state.fullmove)
    positions = replay.positions
    events = replay.events

    legal = legal_moves(state)
    checked = in_check(state)
    if not legal:
        winner = _other(state.turn)
        return RuleResult(state, legal, checked, True, winner,
                          "checkmate" if checked else "stalemate", "no_legal_moves")
    repetition = _repetition_distance(positions)
    if repetition:
        winner, reason = _judge_cycle(events[-repetition:], state.turn)
        return RuleResult(state, legal, checked, True, winner, reason, "threefold")
    if state.halfmove >= 120:
        return RuleResult(state, legal, checked, True, None, "natural_limit_draw", "natural_limit")
    if _insufficient_material(state):
        return RuleResult(state, legal, checked, True, None, "insufficient_material", "material")
    return RuleResult(state, legal, checked, False, None, None, "none")


@lru_cache(maxsize=8192)
def _replay(initial_fen: str, history: tuple[str, ...]) -> Replay:
    if not history:
        state = parse_fen(initial_fen)
        identities = {square: index for index, square in enumerate(sorted(state.board))}
        return Replay(state, identities, (_position_key(state),), ())
    previous = _replay(initial_fen, history[:-1])
    state = State(dict(previous.state.board), previous.state.turn,
                  previous.state.halfmove, previous.state.fullmove)
    identities = dict(previous.identities)
    move = history[-1]
    if move not in legal_moves(state):
        raise ValueError(f"Illegal move in history: {move}")
    side = state.turn
    before = State(dict(state.board), state.turn, state.halfmove, state.fullmove)
    moving_id = identities.pop(move[:2])
    identities.pop(move[2:], None)
    identities[move[2:]] = moving_id
    apply_move(state, move)
    event = MoveEvent(side, in_check(state),
                      frozenset(_new_chase_targets(before, state, move, identities)))
    return Replay(state, identities, previous.positions + (_position_key(state),),
                  previous.events + (event,))


def _replay_full(initial_fen: str, history: tuple[str, ...]) -> Replay:
    state = parse_fen(initial_fen)
    identities = {square: index for index, square in enumerate(sorted(state.board))}
    positions = [_position_key(state)]
    events: list[MoveEvent] = []
    for move in history:
        if move not in legal_moves(state):
            raise ValueError(f"Illegal move in history: {move}")
        side = state.turn
        before = State(dict(state.board), state.turn, state.halfmove, state.fullmove)
        moving_id = identities.pop(move[:2])
        identities.pop(move[2:], None)
        identities[move[2:]] = moving_id
        apply_move(state, move)
        events.append(MoveEvent(side, in_check(state),
                                frozenset(_new_chase_targets(before, state, move, identities))))
        positions.append(_position_key(state))
    return Replay(state, identities, tuple(positions), tuple(events))


def _position_key(state: State) -> tuple[tuple[tuple[str, str], ...], str]:
    return tuple(sorted(state.board.items())), state.turn


def _repetition_distance(positions: list[tuple]) -> int:
    target = positions[-1]
    earlier = [index for index, key in enumerate(positions[:-1]) if key == target]
    if len(earlier) < 2:
        return 0
    distance = len(positions) - 1 - earlier[-1]
    return distance if earlier[-1] - distance >= 0 and positions[earlier[-1] - distance] == target else 0


def _judge_cycle(events: list[MoveEvent], side_to_move: str) -> tuple[str | None, str]:
    sides = {side: [event for event in events if event.side == side] for side in ("red", "black")}
    perpetual_check = {side for side, turns in sides.items() if turns and all(turn.gives_check for turn in turns)}
    if len(perpetual_check) == 1:
        offender = perpetual_check.pop()
        return _other(offender), "perpetual_check"
    if perpetual_check:
        return None, "repetition_draw"
    # Checks inside a mixed cycle make chase attribution ambiguous; treat the cycle as mutual play.
    if any(event.gives_check for event in events):
        return None, "repetition_draw"
    perpetual_chase: set[str] = set()
    for side, turns in sides.items():
        if not turns:
            continue
        common = set(turns[0].chased)
        for turn in turns[1:]:
            common.intersection_update(turn.chased)
        if common:
            perpetual_chase.add(side)
    if len(perpetual_chase) == 1:
        offender = perpetual_chase.pop()
        return _other(offender), "perpetual_chase"
    return None, "repetition_draw"


def _new_chase_targets(before: State, after: State, move: str, identities: dict[str, int]) -> set[int]:
    moving = after.board[move[2:]]
    if moving.lower() in {"k", "p"}:
        return set()
    side = _side(moving)
    before_targets = set(_pseudo_targets(before, move[:2], moving, attacks=True))
    chased: set[int] = set()
    for square in _pseudo_targets(after, move[2:], moving, attacks=True):
        victim = after.board.get(square)
        if not victim or _side(victim) == side or victim.lower() == "k" or square in before_targets:
            continue
        probe = State(dict(after.board), side, after.halfmove, after.fullmove)
        capture = move[2:] + square
        if capture in legal_moves(probe):
            chased.add(identities[square])
    return chased


def _insufficient_material(state: State) -> bool:
    return all(piece.lower() in {"k", "a", "b"} for piece in state.board.values())


def _other(side: str) -> str:
    return "black" if side == "red" else "red"


def _side(piece: str) -> str:
    return "red" if piece.isupper() else "black"


def _xy(square: str) -> tuple[int, int]:
    return ord(square[0]) - 97, int(square[1])


def _sq(x: int, y: int) -> str:
    return f"{chr(97 + x)}{y}"


def _inside(x: int, y: int) -> bool:
    return 0 <= x < 9 and 0 <= y < 10


def _friendly(state: State, square: str, side: str) -> bool:
    piece = state.board.get(square)
    return bool(piece and _side(piece) == side)


def _pseudo_targets(state: State, origin: str, piece: str, attacks: bool = False) -> list[str]:
    x, y = _xy(origin); side = _side(piece); kind = piece.lower(); out: list[str] = []

    def add(nx: int, ny: int) -> None:
        if _inside(nx, ny) and not _friendly(state, _sq(nx, ny), side):
            out.append(_sq(nx, ny))

    if kind in {"r", "c"}:
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            jumped = False; nx, ny = x + dx, y + dy
            while _inside(nx, ny):
                square = _sq(nx, ny); occupied = square in state.board
                if kind == "r":
                    if not occupied: out.append(square)
                    else:
                        if not _friendly(state, square, side): out.append(square)
                        break
                elif not jumped:
                    if not occupied: out.append(square)
                    else: jumped = True
                elif occupied:
                    if not _friendly(state, square, side): out.append(square)
                    break
                nx += dx; ny += dy
    elif kind == "n":
        for dx, dy, lx, ly in ((2,1,1,0),(2,-1,1,0),(-2,1,-1,0),(-2,-1,-1,0),(1,2,0,1),(-1,2,0,1),(1,-2,0,-1),(-1,-2,0,-1)):
            if _sq(x + lx, y + ly) not in state.board: add(x + dx, y + dy)
    elif kind == "b":
        for dx, dy in ((2,2),(2,-2),(-2,2),(-2,-2)):
            ny = y + dy
            own_half = ny <= 4 if side == "red" else ny >= 5
            if own_half and _sq(x + dx // 2, y + dy // 2) not in state.board: add(x + dx, ny)
    elif kind == "a":
        for dx, dy in ((1,1),(1,-1),(-1,1),(-1,-1)):
            nx, ny = x + dx, y + dy
            if 3 <= nx <= 5 and ((0 <= ny <= 2) if side == "red" else (7 <= ny <= 9)): add(nx, ny)
    elif kind == "k":
        for dx, dy in ((1,0),(-1,0),(0,1),(0,-1)):
            nx, ny = x + dx, y + dy
            if 3 <= nx <= 5 and ((0 <= ny <= 2) if side == "red" else (7 <= ny <= 9)): add(nx, ny)
        direction = 1 if side == "red" else -1
        ny = y + direction
        while 0 <= ny < 10:
            target = _sq(x, ny)
            if target in state.board:
                if state.board[target].lower() == "k" and _side(state.board[target]) != side: out.append(target)
                break
            ny += direction
    elif kind == "p":
        forward = 1 if side == "red" else -1
        add(x, y + forward)
        crossed = y >= 5 if side == "red" else y <= 4
        if crossed:
            add(x - 1, y); add(x + 1, y)
    return out
