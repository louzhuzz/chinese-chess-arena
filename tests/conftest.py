from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

MATE_IN_ONE_FEN = "3k5/R8/9/9/9/4P4/9/9/R3C4/5K3 w - - 0 1"


def _prompt(body: dict, path: str) -> str:
    if path.endswith("/chat/completions") or path.endswith("/v1/messages"):
        content = body["messages"][-1]["content"]
        if isinstance(content, list):
            content = "".join(block.get("text", "") for block in content if isinstance(block, dict))
    else:
        content = body["input"]
        if isinstance(content, list):
            content = content[-1].get("content", "")
    if isinstance(content, str) and not content.lstrip().startswith("{"):
        content = content[content.find("{"):]
    return content


def _choose(policy: str, position: dict) -> str | None:
    legal = position["legal_moves"]
    if policy == "last":
        return legal[-1]
    if policy == "illegal":
        return "z9z8"
    if policy == "illegal_once" and "correction" not in position:
        return "z9z8"
    if policy == "truncate_once" and "correction" not in position:
        return None
    if policy.startswith("prefer:"):
        move = policy.split(":", 1)[1]
        return move if move in legal else legal[0]
    return legal[0]


class FakeModelHandler(BaseHTTPRequestHandler):
    """OpenAI Chat, OpenAI Responses and Anthropic Messages, driven by preset.model."""

    protocol_version = "HTTP/1.1"
    requests: list[tuple[str, dict]] = []

    def log_message(self, *args) -> None:  # keep pytest output clean
        pass

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", "fake-provider-request-123")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.endswith("/models"):
            self._send({"data": [{"id": "fake-model"}]})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self.requests.append((self.path, body))
        policy = str(body.get("model", "first"))
        if policy == "error500":
            self._send({"error": "upstream boom"}, 500)
            return
        if policy == "slow":
            time.sleep(8)
        move = _choose(policy, json.loads(_prompt(body, self.path)))
        if move is None:  # 模拟推理模型把预算花光、正文为空的截断情况
            if self.path.endswith("/chat/completions"):
                self._send({"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                            "usage": {"prompt_tokens": 11, "completion_tokens": 4096}})
            else:
                self._send({"content": [], "stop_reason": "max_tokens",
                            "usage": {"input_tokens": 11, "output_tokens": 4096}})
            return
        text = json.dumps({"move": move, "note": f"private plan for {policy}"})
        if self.path.endswith("/chat/completions"):
            self._send({"choices": [{"message": {"content": text}}],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 3,
                                  "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 11},
                        "request_echo": {"thinking": body.get("thinking"),
                                         "reasoning_effort": body.get("reasoning_effort")}})
        elif self.path.endswith("/responses"):
            self._send({"output_text": text, "usage": {"input_tokens": 11, "output_tokens": 3,
                                                         "input_tokens_details": {"cached_tokens": 0,
                                                                                   "cache_write_tokens": 11}}})
        elif self.path.endswith("/v1/messages"):
            self._send({"content": [{"type": "text", "text": text}],
                        "usage": {"input_tokens": 11, "output_tokens": 3,
                                  "cache_read_input_tokens": 0,
                                  "cache_creation_input_tokens": 11}})
        else:
            self._send({"error": "not found"}, 404)


@pytest.fixture(scope="session")
def fake_models() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def api(tmp_path, monkeypatch, fake_models):
    """A TestClient whose config and database live in tmp_path."""
    monkeypatch.setenv("XIANGQI_DB", str(tmp_path / "api.db"))
    monkeypatch.setenv("XIANGQI_CONFIG", str(tmp_path / "models.yaml"))
    (tmp_path / "models.yaml").write_text(yaml.safe_dump({
        "connections": [
            {"id": "openai", "name": "OpenAI fake", "protocol": "openai_chat",
             "base_url": fake_models + "/v1", "api_key": "sk-test-secret",
             "extra_headers": {"X-API-Token": "header-secret", "X-Region": "local"}},
            {"id": "claude", "name": "Claude fake", "protocol": "anthropic_messages",
             "base_url": fake_models, "api_key": "sk-ant-secret"},
            {"id": "human", "name": "Human", "protocol": "human", "base_url": ""},
        ],
        "presets": [
            {"id": "red", "name": "Red", "connection_id": "openai", "model": "first"},
            {"id": "black", "name": "Black", "connection_id": "claude", "model": "first"},
            {"id": "human-red", "name": "我执红", "connection_id": "human", "model": "human"},
            {"id": "human-black", "name": "我执黑", "connection_id": "human", "model": "human"},
        ],
    }, allow_unicode=True), encoding="utf-8")
    FakeModelHandler.requests.clear()

    import importlib

    from backend.app import config as config_module
    from backend.app import db as db_module
    # Reload cached defaults before importing main: its module body opens a DB.
    for module in (config_module, db_module):
        importlib.reload(module)
    from backend.app import main as main_module
    importlib.reload(main_module)
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as client:
        yield client, main_module


@pytest.fixture
def request_log():
    return FakeModelHandler.requests


def set_model(client, preset_id: str, model: str) -> None:
    preset = next(p for p in client.get("/api/config").json()["presets"] if p["id"] == preset_id)
    preset["model"] = model
    assert client.put(f"/api/presets/{preset_id}", json=preset).status_code == 200
