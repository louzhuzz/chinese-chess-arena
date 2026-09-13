from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.getenv("XIANGQI_DB", ROOT / "data" / "xiangqi.db"))


class Database:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path; self.lock = threading.RLock()
        self.init()

    @contextmanager
    def connect(self):
        with self.lock:
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn; conn.commit()
            finally: conn.close()

    def init(self):
        with self.connect() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS games (
              id TEXT PRIMARY KEY, status TEXT NOT NULL, red_preset TEXT NOT NULL, black_preset TEXT NOT NULL,
              initial_fen TEXT NOT NULL, current_fen TEXT NOT NULL, history_json TEXT NOT NULL DEFAULT '[]',
              winner TEXT, reason TEXT, move_timeout INTEGER NOT NULL, max_plies INTEGER NOT NULL,
              ruleset_id TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, benchmark_id TEXT,
              red_config_json TEXT, black_config_json TEXT, prompt_version TEXT
            );
            CREATE TABLE IF NOT EXISTS moves (
              id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT NOT NULL, ply INTEGER NOT NULL, side TEXT NOT NULL,
              move TEXT, fen_before TEXT NOT NULL, fen_after TEXT, prompt_json TEXT NOT NULL, response_text TEXT,
              raw_json TEXT, duration_ms INTEGER, input_tokens INTEGER, output_tokens INTEGER, attempt INTEGER NOT NULL,
              error TEXT, created_at TEXT NOT NULL, note TEXT, connect_ms INTEGER, first_byte_ms INTEGER,
              provider_request_id TEXT, total_input_tokens INTEGER, cache_read_tokens INTEGER,
              cache_write_tokens INTEGER, cache_miss_tokens INTEGER, UNIQUE(game_id, ply, attempt)
            );
            CREATE TABLE IF NOT EXISTS context_messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT NOT NULL, side TEXT NOT NULL,
              sequence INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
              ply INTEGER NOT NULL, attempt INTEGER NOT NULL, created_at TEXT NOT NULL,
              UNIQUE(game_id, side, sequence)
            );
            CREATE TABLE IF NOT EXISTS benchmarks (
              id TEXT PRIMARY KEY, status TEXT NOT NULL, preset_a TEXT NOT NULL, preset_b TEXT NOT NULL,
              pairs INTEGER NOT NULL, settings_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_actions (
              id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT NOT NULL, ply INTEGER NOT NULL, side TEXT NOT NULL,
              attempt INTEGER NOT NULL, sequence INTEGER NOT NULL, kind TEXT NOT NULL, name TEXT,
              args_json TEXT, result_json TEXT, text TEXT, duration_ms INTEGER, input_tokens INTEGER,
              output_tokens INTEGER, total_input_tokens INTEGER, cache_read_tokens INTEGER,
              cache_write_tokens INTEGER, connect_ms INTEGER, first_byte_ms INTEGER,
              provider_request_id TEXT, finish_reason TEXT, error TEXT, created_at TEXT NOT NULL,
              request_json TEXT, raw_json TEXT,
              UNIQUE(game_id, ply, attempt, sequence)
            );
            CREATE TABLE IF NOT EXISTS agent_notes (
              game_id TEXT NOT NULL, side TEXT NOT NULL, text TEXT NOT NULL, ply INTEGER NOT NULL,
              updated_at TEXT NOT NULL, PRIMARY KEY(game_id, side)
            );
            """)
            self._add_column(c, "games", "red_config_json", "TEXT")
            self._add_column(c, "games", "black_config_json", "TEXT")
            self._add_column(c, "games", "prompt_version", "TEXT")
            self._add_column(c, "games", "mode", "TEXT")
            self._add_column(c, "games", "agent_max_rounds", "INTEGER")
            self._add_column(c, "agent_actions", "cache_write_tokens", "INTEGER")
            self._add_column(c, "agent_actions", "connect_ms", "INTEGER")
            self._add_column(c, "agent_actions", "first_byte_ms", "INTEGER")
            self._add_column(c, "agent_actions", "finish_reason", "TEXT")
            self._add_column(c, "agent_actions", "request_json", "TEXT")
            self._add_column(c, "agent_actions", "raw_json", "TEXT")
            self._add_column(c, "moves", "note", "TEXT")
            self._add_column(c, "moves", "actual_request_json", "TEXT")
            self._add_column(c, "moves", "connect_ms", "INTEGER")
            self._add_column(c, "moves", "first_byte_ms", "INTEGER")
            self._add_column(c, "moves", "provider_request_id", "TEXT")
            self._add_column(c, "moves", "total_input_tokens", "INTEGER")
            self._add_column(c, "moves", "cache_read_tokens", "INTEGER")
            self._add_column(c, "moves", "cache_write_tokens", "INTEGER")
            self._add_column(c, "moves", "cache_miss_tokens", "INTEGER")
            c.execute("UPDATE games SET status='interrupted', reason='process_restart', updated_at=datetime('now') WHERE status IN ('queued','running')")
            c.execute("UPDATE benchmarks SET status='interrupted', updated_at=datetime('now') WHERE status IN ('queued','running')")

    @staticmethod
    def _add_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self.connect() as c: c.execute(sql, params)

    def one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self.connect() as c:
            row = c.execute(sql, params).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self.connect() as c: return [dict(row) for row in c.execute(sql, params).fetchall()]

    def game(self, game_id: str) -> dict[str, Any] | None:
        row = self.one("SELECT * FROM games WHERE id=?", (game_id,))
        if row: row["history"] = json.loads(row.pop("history_json"))
        return row
