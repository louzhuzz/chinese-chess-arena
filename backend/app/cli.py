from __future__ import annotations

import argparse
import asyncio
import json

from .main import benchmarks, db, games
from .schemas import BenchmarkCreate, GameCreate


async def run_game(args):
    game_id=games.create(GameCreate(red_preset_id=args.red,black_preset_id=args.black,move_timeout_seconds=args.timeout,max_plies=args.max_plies)); games.start(game_id)
    while db.game(game_id)["status"] in {"queued","running"}: await asyncio.sleep(.1)
    print(json.dumps(db.game(game_id),ensure_ascii=False,indent=2))


async def run_benchmark(args):
    spec=BenchmarkCreate(preset_a_id=args.a,preset_b_id=args.b,pairs=args.pairs,move_timeout_seconds=args.timeout,max_plies=args.max_plies)
    bench_id=benchmarks.create(spec)
    while db.one("SELECT status FROM benchmarks WHERE id=?",(bench_id,))["status"] in {"queued","running"}: await asyncio.sleep(.1)
    print(json.dumps(benchmarks.report(bench_id),ensure_ascii=False,indent=2))


def main():
    parser=argparse.ArgumentParser(description="Run Xiangqi LLM matches")
    sub=parser.add_subparsers(dest="command",required=True)
    game=sub.add_parser("game"); game.add_argument("--red",required=True); game.add_argument("--black",required=True)
    bench=sub.add_parser("benchmark"); bench.add_argument("--a",required=True); bench.add_argument("--b",required=True); bench.add_argument("--pairs",type=int,default=5)
    for command in (game,bench): command.add_argument("--timeout",type=int,default=120); command.add_argument("--max-plies",type=int,default=400)
    args=parser.parse_args(); asyncio.run(run_game(args) if args.command=="game" else run_benchmark(args))


if __name__=="__main__": main()
