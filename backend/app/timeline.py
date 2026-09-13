"""智能体模式的行动回放与费用统计。

一手棋 = 若干次模型请求 + 若干次工具调用 + 最多一次正式落子。本模块把 `agent_actions`
里的事实整理成可直接展示/导出的数据，并把 `moves` 表里这一手的全部尝试（含失败请求）
汇总成每手费用，供 API、网页与命令行复用。

约定：费用只在这里按 preset 的单价估算；单价未知时留空，不用「0」冒充已知。
"""
from __future__ import annotations

import json
from typing import Any

# 工具名 → 网页上的中文说法，回放时读起来是「查询/试走」而不是函数名。
TOOL_LABELS: dict[str, str] = {
    "get_position": "查询局面",
    "get_legal_moves": "查询合法走法",
    "check_move": "校验一步",
    "simulate_line": "试走变化",
    "get_history": "查询棋谱",
    "read_note": "读取本方笔记",
    "write_note": "写入本方笔记",
    "submit_move": "正式落子",
}
KIND_LABELS: dict[str, str] = {
    "request": "模型请求",
    "tool": "工具调用",
    "submit": "正式落子",
    "text_submit": "正文落子",
}
ACTION_COLUMNS = ("ply, side, attempt, sequence, kind, name, args_json, result_json, text, "
                  "duration_ms, error, input_tokens, output_tokens, total_input_tokens, "
                  "cache_read_tokens, cache_write_tokens, connect_ms, first_byte_ms, "
                  "provider_request_id, finish_reason, request_json, raw_json, created_at")


def tool_label(name: str | None) -> str:
    return TOOL_LABELS.get(name or "", name or "")


def load_actions(db, game_id: str) -> list[dict[str, Any]]:
    """一手之内的行动序列，按手数与发生顺序。"""
    rows = db.all(f"SELECT {ACTION_COLUMNS} FROM agent_actions WHERE game_id=? ORDER BY ply, attempt, sequence",
                  (game_id,))
    for row in rows:
        row["args"] = json.loads(row.pop("args_json")) if row["args_json"] else None
        row["result"] = json.loads(row.pop("result_json")) if row["result_json"] else None
        # 本轮真正发出去的参数与提供方原始回复：排查「参数没生效 / 输出耗尽 / 接口异常」用。
        row["request"] = json.loads(row.pop("request_json")) if row["request_json"] else None
        row["raw"] = json.loads(row.pop("raw_json")) if row["raw_json"] else None
        row["label"] = KIND_LABELS.get(row["kind"], row["kind"]) if row["kind"] != "tool" else tool_label(row["name"])
    return rows


def _estimate_cost(preset, input_tokens: int | None, output_tokens: int | None) -> float | None:
    if preset is None or input_tokens is None or output_tokens is None:
        return None
    input_price = getattr(preset, "input_price_per_million", None)
    output_price = getattr(preset, "output_price_per_million", None)
    if input_price is None or output_price is None:
        return None
    return round(input_tokens / 1_000_000 * input_price + output_tokens / 1_000_000 * output_price, 6)


def load_move_costs(db, game_id: str, preset_for) -> list[dict[str, Any]]:
    """每一手的费用：请求次数、工具次数、token（含缓存）、耗时与估算费用。

    这里的 token 与耗时包含同一手里的重试与失败请求，因为那同样是真实开销。
    """
    rows = db.all("""SELECT ply, side, COUNT(*) AS attempts,
                            SUM(COALESCE(duration_ms,0)) AS duration_ms,
                            CASE WHEN COUNT(*)=COUNT(COALESCE(total_input_tokens,input_tokens))
                                 THEN SUM(COALESCE(total_input_tokens,input_tokens)) END AS input_tokens,
                            CASE WHEN COUNT(*)=COUNT(output_tokens) THEN SUM(output_tokens) END AS output_tokens,
                            CASE WHEN COUNT(*)=COUNT(cache_read_tokens) THEN SUM(cache_read_tokens) END AS cache_read_tokens,
                            CASE WHEN COUNT(*)=COUNT(cache_write_tokens) THEN SUM(cache_write_tokens) END AS cache_write_tokens,
                            MAX(CASE WHEN error IS NULL THEN 0 ELSE 1 END) AS failed
                     FROM moves WHERE game_id=? GROUP BY ply, side ORDER BY ply""", (game_id,))
    counts = {(row["ply"], row["side"]): row for row in db.all(
        """SELECT ply, side,
                  SUM(CASE WHEN kind='request' THEN 1 ELSE 0 END) AS requests,
                  SUM(CASE WHEN kind='tool' THEN 1 ELSE 0 END) AS tools,
                  SUM(CASE WHEN kind IN ('submit','text_submit') THEN 1 ELSE 0 END) AS submits,
                  SUM(CASE WHEN kind='tool' AND error IS NOT NULL THEN 1 ELSE 0 END) AS tool_errors
           FROM agent_actions WHERE game_id=? GROUP BY ply, side""", (game_id,))}
    for row in rows:
        extra = counts.get((row["ply"], row["side"]), {})
        row["requests"] = extra.get("requests") or 0
        row["tools"] = extra.get("tools") or 0
        row["submits"] = extra.get("submits") or 0
        row["tool_errors"] = extra.get("tool_errors") or 0
        row["failed"] = bool(row["failed"])
        row["cost_estimate"] = _estimate_cost(preset_for(row["side"]), row["input_tokens"], row["output_tokens"])
    return rows


def build_timeline(db, game: dict[str, Any], preset_for) -> dict[str, Any]:
    """网页/命令行共用的行动回放：行动序列 + 每手费用 + 合计。"""
    actions = load_actions(db, game["id"])
    moves = load_move_costs(db, game["id"], preset_for)
    by_move: dict[tuple[int, str], dict[str, Any]] = {}
    for move in moves:
        move["actions"] = []
        by_move[(move["ply"], move["side"])] = move
    for action in actions:
        entry = by_move.setdefault((action["ply"], action["side"]), {
            "ply": action["ply"], "side": action["side"], "attempts": 0, "duration_ms": 0,
            "input_tokens": None, "output_tokens": None, "cache_read_tokens": None, "cache_write_tokens": None,
            "requests": 0, "tools": 0, "submits": 0, "tool_errors": 0, "failed": False,
            "cost_estimate": None, "actions": [],
        })
        entry["actions"].append(action)
    # 还在进行中的那一手：moves 表还没有行，就用已经记录下来的请求用量兜底（而不是假装是 0）。
    for entry in by_move.values():
        if entry["attempts"]:
            continue
        requests = [a for a in entry["actions"] if a["kind"] == "request"]
        entry["requests"] = len(requests)
        entry["tools"] = sum(1 for a in entry["actions"] if a["kind"] == "tool")
        entry["submits"] = sum(1 for a in entry["actions"] if a["kind"] in {"submit", "text_submit"})
        entry["tool_errors"] = sum(1 for a in entry["actions"] if a["kind"] == "tool" and a["error"])
        def request_sum(primary: str, fallback: str | None = None) -> int | None:
            values = [a[primary] if a[primary] is not None else a[fallback] if fallback else None for a in requests]
            return sum(values) if all(value is not None for value in values) else None
        entry["input_tokens"] = request_sum("total_input_tokens", "input_tokens")
        entry["output_tokens"] = request_sum("output_tokens")
        entry["cache_read_tokens"] = request_sum("cache_read_tokens")
        entry["cache_write_tokens"] = request_sum("cache_write_tokens")
        entry["duration_ms"] = sum(a["duration_ms"] or 0 for a in requests)
        entry["failed"] = any(a["error"] for a in requests)
    ordered = [by_move[key] for key in sorted(by_move, key=lambda item: (item[0], item[1]))]
    def total_or_unknown(field: str) -> int | None:
        values = [item[field] for item in ordered]
        return sum(values) if all(value is not None for value in values) else None
    total = {
        "requests": sum(item["requests"] for item in ordered),
        "tools": sum(item["tools"] for item in ordered),
        "input_tokens": total_or_unknown("input_tokens"),
        "output_tokens": total_or_unknown("output_tokens"),
        "cache_read_tokens": total_or_unknown("cache_read_tokens"),
        "cache_write_tokens": total_or_unknown("cache_write_tokens"),
        "duration_ms": sum(item["duration_ms"] or 0 for item in ordered),
        "cost_estimate": (round(sum(item["cost_estimate"] for item in ordered), 6)
                          if ordered and all(item["cost_estimate"] is not None for item in ordered) else None),
    }
    return {"actions": actions, "moves": ordered, "total": total,
            "mode": game.get("mode") or "direct", "max_rounds": game.get("agent_max_rounds") or 6}


def _tool_brief(action: dict[str, Any]) -> str:
    """工具调用的短摘要：查询/试走的结果比参数更重要，但参数要能对上模型的原话。"""
    result = action.get("result") or {}
    name = action.get("name")
    if name == "get_legal_moves":
        return f"{result.get('count', '?')} 个合法着法"
    if name == "check_move":
        return "合法" if result.get("legal") else f"非法：{result.get('reason', '')}"
    if name == "simulate_line":
        steps = result.get("steps") or []
        bad = [step for step in steps if not step.get("legal")]
        verdict = ""
        if result.get("ended"):
            verdict = f"，终局：{result.get('reason')}（{result.get('winner') or '和'}）"
        if bad:
            return f"{len(steps)} 步变化，第 {bad[0].get('index', 0) + 1} 步非法{verdict}"
        return f"{len(steps)} 步变化全部合法{verdict}"
    if name == "get_position":
        return f"手数 {result.get('ply')}，行棋方 {result.get('side_to_move')}"
    if name == "get_history":
        return f"{len(result.get('moves') or [])} 个半回合"
    if name == "read_note":
        note = result.get("private_note")
        return "无笔记" if not note else f"{len(note.get('text') or '')} 字"
    if name == "write_note":
        return "已替换为本手笔记"
    if name == "submit_move":
        if result.get("accepted"):
            return f"接受 {result.get('move')}（{result.get('notation') or ''}）"
        return f"拒绝：{result.get('reason', '')}"
    return str(result.get("error") or "")


def render_timeline(game: dict[str, Any], timeline: dict[str, Any]) -> str:
    """命令行用的文本回放：每手一次汇总，下面是这一手的每个动作。"""
    side_labels = {"red": "红", "black": "黑"}
    shown = lambda value: "未知" if value is None else str(value)
    moves = timeline["moves"]
    lines = [f"对局 {game['id']} · 模式 {timeline['mode']} · 每手最多 {timeline['max_rounds']} 轮请求",
             f"结果: {game.get('winner') or '和'} / {game.get('reason')} · 半回合 {len(game.get('history') or [])}"]
    if not moves:
        lines.append("（没有智能体行动记录）")
        return "\n".join(lines) + "\n"
    for move in moves:
        cost = "未知" if move["cost_estimate"] is None else f"{move['cost_estimate']:.4f}"
        lines.append(f"[第 {move['ply'] + 1} 手 {side_labels.get(move['side'], move['side'])}方] "
                     f"请求 {move['requests']} 次 · 工具 {move['tools']} 次 · "
                     f"输入 {shown(move['input_tokens'])} token（缓存读 {shown(move['cache_read_tokens'])}）· "
                     f"输出 {shown(move['output_tokens'])} token · {move['duration_ms']} ms · 估算费用 {cost}"
                     + ("　⚠ 本手有失败请求" if move["failed"] else ""))
        for action in move["actions"]:
            if action["kind"] == "request":
                args = action.get("args") or {}
                sent = action.get("request") or {}
                detail = f"第 {args.get('round', '?')} 轮，可用工具 {len(args.get('tools') or [])} 个"
                limits = f"max_tokens {sent.get('max_tokens', args.get('max_tokens', '?'))}，时限 {sent.get('timeout_seconds', args.get('timeout_seconds', '?'))} s"
                tokens = f"in {action.get('total_input_tokens') or action.get('input_tokens') or '?'} / out {action.get('output_tokens') or '?'}"
                lines.append(f"    · {action['label']}（{detail}；{limits}；{tokens}；{action['duration_ms']} ms"
                             + (f"；结束原因 {action['finish_reason']}" if action.get("finish_reason") else "")
                             + (f"；{action['error']}" if action.get("error") else "") + "）")
            elif action["kind"] == "tool":
                lines.append(f"    · {action['label']} {json.dumps(action.get('args') or {}, ensure_ascii=False)} → "
                             f"{_tool_brief(action)}")
            else:
                lines.append(f"    · {action['label']} → {_tool_brief(action)}")
    total = timeline["total"]
    cost = "未知" if total["cost_estimate"] is None else f"{total['cost_estimate']:.4f}"
    lines.append(f"合计: 请求 {total['requests']} 次 · 工具 {total['tools']} 次 · 输入 {shown(total['input_tokens'])} token"
                 f"（缓存读 {shown(total['cache_read_tokens'])}）· 输出 {shown(total['output_tokens'])} token · "
                 f"{round(total['duration_ms'] / 1000, 1)} s · 估算费用 {cost}")
    return "\n".join(lines) + "\n"


def move_headline(move: dict[str, Any]) -> str:
    """一行的行动链条，例如「查询合法走法 → 试走变化 → 校验一步 → 正式落子 a0a1」。"""
    parts: list[str] = []
    for action in move.get("actions") or []:
        if action["kind"] == "tool":
            parts.append(action["label"])
        elif action["kind"] in {"submit", "text_submit"}:
            parts.append(f"{action['label']} {(action.get('args') or {}).get('move', '')}".strip())
    return " → ".join(parts)
