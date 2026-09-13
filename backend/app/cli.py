from __future__ import annotations

import argparse
import asyncio
import json

from .main import benchmarks, db, games, get_game
from .record import positions_filename, record_filename, render_fen_positions, render_fen_record
from .schemas import DEFAULT_MOVE_TIMEOUT, BenchmarkCreate, GameCreate
from .timeline import build_timeline, render_timeline


async def run_game(args):
    game_id=games.create(GameCreate(red_preset_id=args.red,black_preset_id=args.black,move_timeout_seconds=args.timeout,
                                    max_plies=args.max_plies,mode=args.mode,agent_max_rounds=args.agent_rounds))
    games.start(game_id)
    while db.game(game_id)["status"] in {"queued","running"}: await asyncio.sleep(.1)
    game=db.game(game_id)
    print(json.dumps(game,ensure_ascii=False,indent=2))
    if args.mode=="agent":
        print(render_timeline(game, build_timeline(db, game, lambda side: games._preset_for(game, side))))


async def run_benchmark(args):
    spec=BenchmarkCreate(preset_a_id=args.a,preset_b_id=args.b,pairs=args.pairs,
                         move_timeout_seconds=args.timeout,max_plies=args.max_plies,
                         mode=args.mode,agent_max_rounds=args.agent_rounds)
    bench_id=benchmarks.create(spec)
    while db.one("SELECT status FROM benchmarks WHERE id=?",(bench_id,))["status"] in {"queued","running"}: await asyncio.sleep(.1)
    print(json.dumps(benchmarks.report(bench_id),ensure_ascii=False,indent=2))


def run_export(args):
    from pathlib import Path
    game=get_game(args.game)
    out=Path(args.out) if args.out else Path(__file__).resolve().parents[2]/"run"
    out.mkdir(parents=True, exist_ok=True)
    record=out/record_filename(game["id"]); positions=out/positions_filename(game["id"])
    record.write_text(render_fen_record(game),encoding="utf-8")
    positions.write_text(render_fen_positions(game),encoding="utf-8")
    print(f"{record}  ({record.stat().st_size} bytes)")
    print(f"{positions}  ({positions.stat().st_size} bytes)")


def run_timeline(args):
    game=get_game(args.game)
    print(render_timeline(game, build_timeline(db, game, lambda side: games._preset_for(game, side))),end="")


def main():
    parser=argparse.ArgumentParser(description="Run Xiangqi LLM matches")
    sub=parser.add_subparsers(dest="command",required=True)
    game=sub.add_parser("game"); game.add_argument("--red",required=True); game.add_argument("--black",required=True)
    game.add_argument("--mode",choices=("direct","agent"),default="direct",help="direct=一次请求定一步；agent=一手内可调用规则工具")
    game.add_argument("--agent-rounds",type=int,default=6,help="智能体模式每手最多几轮模型请求")
    bench=sub.add_parser("benchmark"); bench.add_argument("--a",required=True); bench.add_argument("--b",required=True); bench.add_argument("--pairs",type=int,default=5)
    bench.add_argument("--mode",choices=("direct","agent"),default="direct")
    bench.add_argument("--agent-rounds",type=int,default=6)
    export=sub.add_parser("export"); export.add_argument("--game",required=True); export.add_argument("--out",default=None)
    timeline=sub.add_parser("timeline"); timeline.add_argument("--game",required=True)
    for command in (game,bench): command.add_argument("--timeout",type=int,default=DEFAULT_MOVE_TIMEOUT); command.add_argument("--max-plies",type=int,default=400)
    args=parser.parse_args()
    if args.command=="game": asyncio.run(run_game(args))
    elif args.command=="benchmark": asyncio.run(run_benchmark(args))
    elif args.command=="timeline": run_timeline(args)
    else: run_export(args)


if __name__=="__main__": main()
