"""标准 FEN 棋谱格式（`game-<id8>-fen.txt` / `game-<id8>-positions.fen`）的契约测试。"""
from __future__ import annotations

import re
import time

from backend.app.record import (positions_filename, record_filename, render_fen_positions,
                                render_fen_record)

START = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"
FEN_1 = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C2C4/9/RNBAKABNR b - - 1 1"
FEN_2 = "rnbakab1r/9/1c4nc1/p1p1p1p1p/9/9/P1P1P1P1P/1C2C4/9/RNBAKABNR w - - 2 2"


def sample_game() -> dict:
    """一局两手的假棋谱，外加一条失败尝试（不应出现在棋谱里）。"""
    return {
        "id": "0123456789abcdef0123456789abcdef",
        "red_preset": "red-model", "black_preset": "black-model",
        "initial_fen": START, "current_fen": FEN_2,
        "winner": "black", "reason": "checkmate", "move_timeout": 600, "max_plies": 400,
        "ruleset_id": "xiangqi-bench-rules-1.0",
        "moves": [
            {"ply": 0, "side": "red", "move": "h2e2", "notation": "炮二平五",
             "fen_after": FEN_1, "attempt": 1, "error": None},
            {"ply": 1, "side": "black", "move": "h9g7", "notation": "马八进七",
             "fen_after": FEN_2, "attempt": 1, "error": None},
            {"ply": 1, "side": "black", "move": None, "notation": None,
             "fen_after": FEN_2, "attempt": 2, "error": "boom"},
        ],
    }


def test_render_fen_record_layout():
    text = render_fen_record(sample_game())
    lines = text.splitlines()
    assert lines[0] == "中国象棋擂台 · FEN 棋谱"
    assert lines[1] == "=" * 78
    assert lines[2] == "对局 ID  : 0123456789abcdef0123456789abcdef"
    assert lines[3] == "执红/执黑: red-model / black-model"
    assert lines[4] == "结果     : 黑方胜 · 将死    共 2 半回合"
    assert lines[5] == "规则版本 : xiangqi-bench-rules-1.0    每步时限 600 秒"
    assert lines[7] == "格式: 序号 行棋方 着法(内部坐标) 中文记法 | 走子后的 FEN"
    assert lines[8] == ""
    assert lines[9] == f"  0 开局                      | {START}"
    assert lines[10] == f"  1 红 炮二平五(h2e2)         | {FEN_1}"
    assert lines[11] == f"  2 黑 马八进七(h9g7)         | {FEN_2}"
    assert lines[12] == ""
    assert lines[13] == "终局局面已由裁判确认: 将死"
    assert len(lines) == 14
    assert text.endswith("\n")
    # 「|」列固定：开局行第 28 列，着法行第 25 列。
    assert lines[9].index("|") == 28
    assert lines[10].index("|") == 25
    # 失败尝试不进棋谱
    assert "boom" not in text


def test_render_fen_positions_is_one_fen_per_line():
    text = render_fen_positions(sample_game())
    assert text.splitlines() == [START, FEN_1, FEN_2]
    assert text.endswith("\n")


def test_filenames_use_first_eight_id_characters():
    assert record_filename("0123456789abcdef") == "game-01234567-fen.txt"
    assert positions_filename("0123456789abcdef") == "game-01234567-positions.fen"


def test_draw_and_truncated_labels():
    game = sample_game()
    game.update({"winner": None, "reason": "natural_limit_draw"})
    assert "和棋 · 自然限着判和" in render_fen_record(game)
    game.update({"reason": "max_plies"})
    assert "未计胜负 · 步数上限截断" in render_fen_record(game)
    assert render_fen_record(game).splitlines()[-1] == "记录终止状态: 步数上限截断"


def test_export_endpoint_matches_record_and_positions(api):
    client, _ = api
    game_id = client.post("/api/games", json={"red_preset_id": "red", "black_preset_id": "black",
                                              "move_timeout_seconds": 30, "max_plies": 4}).json()["id"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        game = client.get(f"/api/games/{game_id}").json()
        if game["status"] not in {"queued", "running"}:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("game did not finish")

    record = client.get(f"/api/games/{game_id}/export.fen")
    assert record.status_code == 200
    assert record.headers["content-type"].startswith("text/plain")
    assert record.headers["content-disposition"] == f'attachment; filename="{record_filename(game_id)}"'
    text = record.text
    assert text.splitlines()[0] == "中国象棋擂台 · FEN 棋谱"
    body = [line for line in text.splitlines() if re.match(r"^\s+\d+ ", line) and "|" in line]
    assert body[0].split("|")[1].strip() == game["initial_fen"]
    assert body[-1].split("|")[1].strip() == game["current_fen"]
    assert len(body) == len(game["history"]) + 1

    positions = client.get(f"/api/games/{game_id}/export.fen", params={"positions": "true"})
    assert positions.status_code == 200
    assert positions.headers["content-disposition"] == f'attachment; filename="{positions_filename(game_id)}"'
    assert positions.text.splitlines() == [game["initial_fen"], *[line.split("|")[1].strip() for line in body[1:]]]
    assert positions.text.splitlines()[-1] == game["current_fen"]


def test_export_endpoint_404_for_unknown_game(api):
    client, _ = api
    assert client.get("/api/games/does-not-exist/export.fen").status_code == 404
