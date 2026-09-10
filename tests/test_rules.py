from backend.app.rules import START_FEN, State, apply_history, apply_move, chinese_notation, in_check, legal_moves, parse_fen, pieces, terminal, to_fen


def test_start_position_roundtrip_and_moves():
    state=parse_fen(START_FEN)
    assert len(pieces(state)) == 32
    assert len(legal_moves(state)) == 44
    assert parse_fen(to_fen(state)).board == state.board


def test_horse_leg_blocks_two_destinations():
    clear=parse_fen("4k4/9/9/9/4P4/9/9/9/9/1N2K4 w - - 0 1")
    blocked=parse_fen("4k4/9/9/9/4P4/9/9/9/1P7/1N2K4 w - - 0 1")
    assert {m for m in legal_moves(clear) if m.startswith("b0")} == {"b0a2","b0c2","b0d1"}
    assert {m for m in legal_moves(blocked) if m.startswith("b0")} == {"b0d1"}


def test_cannon_requires_exactly_one_screen_to_capture():
    state=parse_fen("4k4/9/9/9/4r4/9/4p4/9/4C4/4K4 w - - 0 1")
    moves=legal_moves(state)
    assert "e1e5" in moves
    assert "e1e4" not in moves


def test_flying_generals_and_self_check():
    state=parse_fen("4k4/9/9/9/9/9/9/9/4R4/4K4 w - - 0 1")
    assert "e1d1" not in legal_moves(state)
    apply_move(state,"e1e8")
    assert in_check(state,"black")


def test_stalemate_is_loss_for_side_to_move():
    state=parse_fen("3k5/4R4/3R5/9/9/9/9/9/9/4K4 b - - 0 1")
    ended,winner,reason=terminal(state)
    assert ended and winner=="red" and reason in {"stalemate","checkmate"}


def test_history_validation():
    state=apply_history(START_FEN,["b0c2","b9c7"])
    assert state.turn=="red"
    try: apply_history(START_FEN,["a0a9"])
    except ValueError as exc: assert "Illegal" in str(exc)
    else: raise AssertionError("illegal history accepted")


def test_replay_without_validation_is_linear_and_still_consistent():
    history=["b0c2","b9c7","c2b0","c7b9"]
    assert apply_history(START_FEN,history,validate=False).board==apply_history(START_FEN,history).board


def test_malformed_move_is_rejected_during_replay():
    for move in ("a0", "z0a1", "e0e0"):
        try: apply_history(START_FEN,[move],validate=False)
        except ValueError: continue
        raise AssertionError(f"malformed move accepted: {move}")


def test_chinese_notation_uses_each_sides_viewpoint():
    assert chinese_notation(START_FEN, "h2e2") == "炮二平五"
    assert chinese_notation(START_FEN, "b2e2") == "炮八平五"
    assert chinese_notation(START_FEN, "b0c2") == "马八进七"
    black = to_fen(apply_history(START_FEN, ["h2e2"]))
    assert chinese_notation(black, "b9c7") == "马二进三"


def test_chinese_notation_front_and_rear_horses():
    fen = "4k4/9/9/9/4P4/9/1N7/9/9/1N2K4 w - - 0 1"
    assert chinese_notation(fen, "b3c5") == "前马进七"
    assert chinese_notation(fen, "b0d1") == "后马进六"


def test_chinese_notation_straight_distance_and_pawn_disambiguation():
    rook_fen = "4k4/9/9/9/4P4/9/9/9/9/R3K4 w - - 0 1"
    assert chinese_notation(rook_fen, "a0a2") == "车九进二"
    three_pawns = "4k4/9/9/9/P3P4/9/P8/9/P8/4K4 w - - 0 1"
    assert chinese_notation(three_pawns, "a5b5") == "前兵平八"
    assert chinese_notation(three_pawns, "a3b3") == "中兵平八"
    assert chinese_notation(three_pawns, "a1b1") == "后兵平八"


def test_chinese_notation_numbers_four_pawns_on_one_file():
    fen = "4k4/9/P8/9/P3P4/9/P8/9/P8/4K4 w - - 0 1"
    assert chinese_notation(fen, "a7b7") == "一兵平八"
    assert chinese_notation(fen, "a3b3") == "三兵平八"
    assert chinese_notation(fen, "a1b1") == "四兵平八"


def test_chinese_notation_numbers_pawns_across_crowded_files():
    fen = "4k4/9/8P/6P2/4P3P/6P2/9/9/9/4K4 w - - 0 1"
    assert chinese_notation(fen, "i7h7") == "一兵平二"
    assert chinese_notation(fen, "i5h5") == "二兵平二"
    assert chinese_notation(fen, "g6f6") == "三兵平四"
