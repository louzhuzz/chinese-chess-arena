from __future__ import annotations

import csv
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import ConfigStore
from .db import Database
from .provider_catalog import builtin_provider_catalog, provider_for_connection
from .rules import START_FEN, apply_history, chinese_notation, pieces
from .runner import BenchmarkRunner, EventBus, GameRunner
from .schemas import AnalyzeRequest, AnalyzeResult, BenchmarkCreate, ConnectionIn, GameCreate, MoveSubmit, Preset

db=Database(); config=ConfigStore(); bus=EventBus(); games=GameRunner(db,config,bus); benchmarks=BenchmarkRunner(db,games,bus,config)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    for task in list(games.tasks.values()) + list(benchmarks.tasks.values()): task.cancel()
    games.arbiter.close()


app=FastAPI(title="Chinese Chess Arena",version="0.1.0",lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=["http://localhost:5173"],allow_credentials=True,allow_methods=["*"],allow_headers=["*"])


@app.get("/api/health")
def health(): return {"status":"ok","ruleset_id":games.arbiter.backend,"arbiter":games.arbiter.describe()}


@app.get("/api/config")
def get_config():
    return {"connections": config.connections(), "presets": config.presets(),
            "builtin_providers": builtin_provider_catalog()}


@app.get("/api/providers")
def get_provider_catalog():
    """Return the built-in provider catalog without secrets."""
    return {"providers": builtin_provider_catalog()}


@app.put("/api/connections/{connection_id}")
def put_connection(connection_id:str,value:ConnectionIn):
    if connection_id != value.id: raise HTTPException(400,"Connection id does not match path")
    config.upsert_connection(value); return {"ok":True}


@app.post("/api/connections/{connection_id}/test")
async def test_connection(connection_id:str):
    try:
        connection=config.connection(connection_id); key=config.api_key(connection)
        if connection.protocol=="mock": return {"ok":True,"message":"Mock connection is ready"}
        headers=connection.extra_headers.copy()
        if connection.protocol.startswith("openai"):
            headers["Authorization"]=f"Bearer {key}"; url=connection.base_url.rstrip("/")+"/models"
        else:
            headers.update({"x-api-key":key,"anthropic-version":"2023-06-01"}); url=connection.base_url.rstrip("/")+"/v1/models"
        async with httpx.AsyncClient(timeout=15) as client: response=await client.get(url,headers=headers)
        response.raise_for_status(); return {"ok":True,"message":f"HTTP {response.status_code}"}
    except Exception as exc: return JSONResponse(status_code=400,content={"ok":False,"message":str(exc)})


@app.post("/api/connections/{connection_id}/sync-models")
async def sync_models(connection_id: str, refresh: bool = False):
    try:
        connection = config.connection(connection_id)
        builtin = provider_for_connection(connection)
        source = "endpoint"
        if builtin and builtin.supported and not refresh:
            # Built-in provider routes use the local catalog so adding a
            # provider does not depend on a /models endpoint.
            model_ids = list(builtin.models)
            source = "catalog"
        elif connection.protocol in {"mock", "human"}:
            model_ids=[connection.protocol]
            source = "local"
        else:
            key = config.api_key(connection)
            headers=connection.extra_headers.copy()
            if connection.protocol.startswith("openai"):
                headers["Authorization"]=f"Bearer {key}"; url=connection.base_url.rstrip("/")+"/models"
            else:
                headers.update({"x-api-key":key,"anthropic-version":"2023-06-01"})
                url=connection.base_url.rstrip("/")+"/v1/models"
            async with httpx.AsyncClient(timeout=30) as client:
                response=await client.get(url,headers=headers); response.raise_for_status(); payload=response.json()
            model_ids=sorted({item.get("id") for item in payload.get("data",[]) if isinstance(item,dict) and item.get("id")})
        if not model_ids: raise ValueError("提供方没有返回可用模型")
        models=config.sync_models(connection_id,model_ids)
        return {"ok":True,"count":len(models),"source":source,
                "models":[model.model_dump() for model in models]}
    except Exception as exc:
        return JSONResponse(status_code=400,content={"ok":False,"message":str(exc)})


@app.put("/api/presets/{preset_id}")
def put_preset(preset_id:str,value:Preset):
    if preset_id != value.id: raise HTTPException(400,"Preset id does not match path")
    config.upsert_preset(value); return {"ok":True}


@app.post("/api/games",status_code=202)
async def create_game(spec:GameCreate):
    try: game_id=games.create(spec); games.start(game_id); return {"id":game_id}
    except (ValueError,StopIteration) as exc: raise HTTPException(400,str(exc)) from exc


@app.get("/api/games")
def list_games(): return db.all("SELECT id,status,red_preset,black_preset,winner,reason,created_at,updated_at,benchmark_id FROM games ORDER BY created_at DESC LIMIT 200")


@app.get("/api/games/{game_id}")
def get_game(game_id:str, include_context: bool = False):
    game=db.game(game_id)
    if not game: raise HTTPException(404,"Game not found")
    game["moves"]=db.all("""SELECT ply,side,move,fen_before,fen_after,prompt_json,response_text,raw_json,
        duration_ms,input_tokens,output_tokens,attempt,error,created_at,note,connect_ms,first_byte_ms,
        provider_request_id,total_input_tokens,cache_read_tokens,cache_write_tokens,cache_miss_tokens,actual_request_json
        FROM moves WHERE game_id=? ORDER BY ply,attempt""",(game_id,))
    for move in game["moves"]:
        move["prompt"]=json.loads(move.pop("prompt_json"))
        actual=move.pop("actual_request_json")
        move["actual_request"]=json.loads(actual) if actual else None
        raw=move.pop("raw_json")
        move["raw"]=json.loads(raw) if raw else None
        move["notation"]=chinese_notation(move["fen_before"],move["move"]) if move["move"] else None
    game["private_memory"]={side:games._private_memory(game_id,side,include_notation=True) for side in ("red","black")}
    if include_context:
        game["context_messages"]={side:db.all("""SELECT sequence,role,content,ply,attempt,created_at
            FROM context_messages WHERE game_id=? AND side=? ORDER BY sequence""", (game_id, side))
                                   for side in ("red", "black")}
    else:
        game["context_messages"]={side: db.one("""SELECT COUNT(*) AS count, MAX(ply) AS last_ply
            FROM context_messages WHERE game_id=? AND side=?""", (game_id, side))
                                   for side in ("red", "black")}
    game["awaiting"]=games.awaiting(game_id)
    if game["awaiting"]:
        try:
            verdict=games.inspect(game_id)
            game["legal_moves"]=verdict.legal_moves; game["in_check"]=verdict.in_check
        except ValueError:
            pass
    return game


@app.post("/api/games/{game_id}/move")
async def submit_move(game_id:str,spec:MoveSubmit):
    try:
        await games.submit_move(game_id,spec.move,spec.expected_ply,spec.expected_side)
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc
    return {"ok":True,"move":spec.move}


@app.post("/api/analyze",response_model=AnalyzeResult)
def analyze(spec:AnalyzeRequest):
    """裁判分析任意局面：可直接给 FEN，也可给初始 FEN 加完整历史。"""
    fen=spec.initial_fen or spec.fen or START_FEN
    try:
        verdict=games.arbiter.inspect(fen,spec.history)
        state=apply_history(fen,spec.history,validate=False)
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from exc
    return AnalyzeResult(fen=verdict.fen,side_to_move=state.turn,pieces=pieces(state),
                          legal_moves=verdict.legal_moves,in_check=verdict.in_check,ended=verdict.ended,
                          winner=verdict.winner,reason=verdict.reason,backend=verdict.backend,source=verdict.source)


@app.post("/api/games/{game_id}/stop")
async def stop_game(game_id:str):
    if not db.game(game_id): raise HTTPException(404,"Game not found")
    games.stop(game_id); return {"ok":True}


@app.get("/api/games/{game_id}/events")
async def game_events(game_id:str):
    async def event_source():
        yield f"data: {json.dumps({'type':'snapshot','game':db.game(game_id)})}\n\n"
        async for event in bus.stream(game_id): yield f"data: {json.dumps(event)}\n\n"
    return StreamingResponse(event_source(),media_type="text/event-stream",headers={"Cache-Control":"no-cache"})


@app.get("/api/games/{game_id}/export.json")
def export_game(game_id:str): return get_game(game_id, include_context=True)


@app.post("/api/benchmarks",status_code=202)
async def create_benchmark(spec:BenchmarkCreate):
    try: return {"id":benchmarks.create(spec)}
    except (ValueError,StopIteration) as exc: raise HTTPException(400,str(exc)) from exc


@app.get("/api/benchmarks")
def list_benchmarks(): return db.all("SELECT * FROM benchmarks ORDER BY created_at DESC LIMIT 100")


@app.get("/api/benchmarks/{benchmark_id}")
def benchmark_report(benchmark_id:str):
    report=benchmarks.report(benchmark_id)
    if not report: raise HTTPException(404,"Benchmark not found")
    return report


@app.get("/api/benchmarks/{benchmark_id}/export.csv")
def benchmark_csv(benchmark_id:str):
    report=benchmarks.report(benchmark_id)
    if not report: raise HTTPException(404,"Benchmark not found")
    output=io.StringIO(); fields=["row_type","preset_id","wins","losses","draws","score_rate","id","status","red_preset","black_preset","winner","reason","created_at","updated_at"]
    rows=[{"row_type":"model_summary","preset_id":preset_id,**result}
          for preset_id,result in report["model_results"].items()]
    rows.extend({"row_type":"game",**game} for game in report["games"])
    writer=csv.DictWriter(output,fieldnames=fields,extrasaction="ignore"); writer.writeheader(); writer.writerows(rows)
    return StreamingResponse(iter([output.getvalue()]),media_type="text/csv",headers={"Content-Disposition":f'attachment; filename="{benchmark_id}.csv"'})


frontend=Path(__file__).resolve().parents[2]/"frontend"/"dist"
if frontend.exists(): app.mount("/",StaticFiles(directory=frontend,html=True),name="frontend")
