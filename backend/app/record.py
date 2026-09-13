"""FEN 棋谱导出。

本项目对外交换棋谱的标准格式就是这里生成的 `game-<id8>-fen.txt`：
抬头是对局信息，正文每个半回合一行「序号 行棋方 着法(内部坐标) 中文记法 | 走子后的 FEN」，
末尾附裁判确认的终局判定。配套的 `game-<id8>-positions.fen` 是同一局面的纯 FEN 序列
（一行一个，含起始局面），便于直接喂给其它程序。

改动这里的排版等于改动对外格式，请同步更新 README 的「棋谱格式」一节和相关测试。
"""
from __future__ import annotations

from typing import Any

# 结果码的中文说明；与网页 REASON 表保持同一套说法。
REASON_LABELS: dict[str, str] = {
    "checkmate": "将死",
    "stalemate": "困毙",
    "repetition_draw": "三次重复判和",
    "natural_limit_draw": "自然限着判和",
    "insufficient_material": "子力不足判和",
    "perpetual_check": "长将判负",
    "perpetual_chase": "长捉判负",
    "invalid_move": "非法着法判负",
    "timeout": "超时判负",
    "api_failure": "接口故障（不计胜负）",
    "max_plies": "步数上限截断",
    "user_stopped": "手动停止",
    "process_restart": "进程重启中断",
}
SIDE_LABELS = {"red": "红", "black": "黑"}
HEADER_RULE = "=" * 78
MOVE_COLUMN = 18
OPENING_PAD = " " * 22  # 开局行没有着法，用固定空白对齐「|」列
TITLE = "中国象棋擂台 · FEN 棋谱"
DRAW_REASONS = {"repetition_draw", "natural_limit_draw", "insufficient_material"}
RULE_END_REASONS = DRAW_REASONS | {"checkmate", "stalemate", "perpetual_check", "perpetual_chase"}


def record_filename(game_id: str) -> str:
    return f"game-{game_id[:8]}-fen.txt"


def positions_filename(game_id: str) -> str:
    return f"game-{game_id[:8]}-positions.fen"


def plays(game: dict[str, Any]) -> list[dict[str, Any]]:
    """只取真正走成的半回合；失败尝试仍留在棋谱 JSON 里。"""
    return [move for move in game.get("moves", []) if move.get("move")]


def fen_positions(game: dict[str, Any]) -> list[str]:
    """起始局面 + 每个半回合后的局面。"""
    return [game["initial_fen"], *(move["fen_after"] for move in plays(game))]


def result_text(game: dict[str, Any]) -> str:
    """结局说法：胜方 / 和棋 / 未计胜负 / 进行中。

    只有裁判真正判定的和棋（重复、自然限着、子力不足）才叫和棋；手动停止、步数截断、
    接口故障这些都是「未计胜负」，棋谱与行动回放共用这一份说法。
    """
    winner = game.get("winner")
    reason_code = game.get("reason")
    if winner:
        return SIDE_LABELS.get(winner, winner) + "方胜"
    if reason_code in DRAW_REASONS:
        return "和棋"
    if reason_code:
        return "未计胜负"
    if game.get("status") in {"queued", "running"}:
        return "对局进行中"
    return "未计胜负"


def render_fen_record(game: dict[str, Any]) -> str:
    """标准棋谱文本（`game-<id8>-fen.txt` 的内容）。"""
    played = plays(game)
    reason_code = game.get("reason")
    reason = REASON_LABELS.get(reason_code, reason_code)
    result = result_text(game)
    lines = [
        TITLE,
        HEADER_RULE,
        f"对局 ID  : {game['id']}",
        f"执红/执黑: {game.get('red_preset')} / {game.get('black_preset')}",
        f"结果     : {result}" + (f" · {reason}" if reason else "") + f"    共 {len(played)} 半回合",
        f"规则版本 : {game.get('ruleset_id')}    每步时限 {game.get('move_timeout')} 秒",
        HEADER_RULE,
        "格式: 序号 行棋方 着法(内部坐标) 中文记法 | 走子后的 FEN",
        "",
        f"{0:>3} 开局{OPENING_PAD}| {game.get('initial_fen')}",
    ]
    for move in played:
        label = f"{move.get('notation') or ''}({move['move']})"
        lines.append(f"{move['ply'] + 1:>3} {SIDE_LABELS.get(move['side'], move['side'])} "
                     f"{label:<{MOVE_COLUMN}} | {move['fen_after']}")
    ending = "终局局面已由裁判确认" if reason_code in RULE_END_REASONS else "记录终止状态"
    lines += ["", f"{ending}: {reason or result}"]
    return "\n".join(lines) + "\n"


def render_fen_positions(game: dict[str, Any]) -> str:
    """纯 FEN 序列（`game-<id8>-positions.fen` 的内容）。"""
    return "\n".join(fen_positions(game)) + "\n"
