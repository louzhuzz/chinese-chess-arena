import os
import subprocess
import sys
from pathlib import Path


def test_api_fixture_does_not_touch_a_previously_imported_database(tmp_path):
    # Simulate cached production defaults using a sacrificial temporary database.
    script = r"""
import sys
from pathlib import Path
sys.path.insert(0, 'tests')
import pytest
from backend.app.db import Database, DB_PATH
from backend.app import config
from conftest import api
config.CONFIG_PATH.write_text('connections: []\npresets: []\n')
db = Database(DB_PATH)
db.execute('''INSERT INTO games
    (id,status,red_preset,black_preset,initial_fen,current_fen,move_timeout,max_plies,ruleset_id,created_at,updated_at)
    VALUES ('live','running','a','b','fen','fen',120,400,'rules','now','now')''')
with pytest.MonkeyPatch.context() as patch:
    test_dir = Path(sys.argv[1]) / 'fixture'
    test_dir.mkdir()
    fixture = api.__wrapped__(test_dir, patch, 'http://127.0.0.1:1')
    client, main = next(fixture)
    try:
        assert main.db.path == test_dir / 'api.db'
        assert db.game('live')['status'] == 'running', 'fixture interrupted the previously configured database'
    finally:
        fixture.close()
"""
    env = dict(os.environ, XIANGQI_DB=str(tmp_path / "sentinel.db"),
               XIANGQI_CONFIG=str(tmp_path / "sentinel.yaml"))
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
