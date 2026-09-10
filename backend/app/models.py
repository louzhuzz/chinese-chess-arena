from __future__ import annotations

import json
import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import ConfigStore
from .schemas import ModelReply, Preset

PROMPT_VERSION = "xiangqi-move-v6-submit-once"
MOVE_SCHEMA = {"type": "object", "properties": {
    "move": {"type": "string", "pattern": "^[a-i][0-9][a-i][0-9]$"},
    "note": {"type": "string", "maxLength": 80},
}, "required": ["move"], "additionalProperties": False}
SYSTEM_PROMPT = """你是中国象棋擂台的棋手，为 player_side 提交当前一步走法。

pieces 是完整的当前棋子位置表，未列出的格子为空。坐标 a0—i9 固定：红方向行号增加方向前进，黑方向行号减少方向前进。棋种 rook=车、knight=马、cannon=炮、bishop=相/象、advisor=仕/士、king=帅/将、pawn=兵/卒。
legal_moves 已由裁判完成合法性和将帅安全检查，按坐标排序，没有评分。只从中原样选一项，不必重新证明棋子规则或枚举合法走法。

完成条件：选定当前候选，并确认其字符串在 legal_moves 中，就立即提交最终 JSON。不要在选定后重新比较所有候选，也不需要证明它是全局最佳。不要进行穷尽搜索。
history 只有最近走法，history_start_ply 是起始着数；不要从开局重建棋盘。last_move 记录上一手实际移动和吃子，captured=null 表示没吃子。private_memory 只是本方旧计划，可能过时，以当前 pieces 为准。

move_timeout_seconds 为整步总时限；remaining_timeout_seconds 为整步剩余秒数；request_timeout_seconds 为本次请求时限。output_token_limit 是本次推理与最终答案合计的输出上限，不是要求用满的思考量。尽早结束思考并输出，不要等到时间或 token 用尽。
若有 correction，修正其中错误后直接提交，不重新展开长分析。

只返回 {"move":"h2e2"} 这样的 JSON。note 可省略；需要保留计划时最多80字，不为写笔记额外推演。"""


class ModelConfigError(RuntimeError):
    """连接或密钥配置有问题；属于接口故障，不是非法着法。"""


@dataclass(slots=True)
class RequestMetrics:
    """Timings for one provider HTTP request, retained even if it times out."""
    started: float = 0.0
    connect_started: float | None = None
    connect_ms: int | None = None
    first_byte_ms: int | None = None
    provider_request_id: str | None = None
    actual_request: dict[str, Any] | None = None

    def reset(self) -> None:
        self.started = time.perf_counter()
        self.connect_started = None
        self.connect_ms = None
        self.first_byte_ms = None
        self.provider_request_id = None

    async def trace(self, name: str, _: dict[str, Any]) -> None:
        now = time.perf_counter()
        if name.endswith("connect_tcp.started"):
            self.connect_started = now
        elif name.endswith("connect_tcp.complete") and self.connect_started is not None:
            self.connect_ms = int((now - self.connect_started) * 1000)
        elif name.endswith("start_tls.complete") and self.connect_started is not None:
            self.connect_ms = int((now - self.connect_started) * 1000)


class ModelClient:
    def __init__(self, config: ConfigStore): self.config = config

    def resolve(self, preset: Preset):
        try:
            connection = self.config.connection(preset.connection_id)
            key = self.config.api_key(connection)
        except (StopIteration, ValueError) as exc:
            raise ModelConfigError(str(exc)) from exc
        return connection, key

    async def choose(self, preset: Preset, position: dict[str, Any], timeout: float,
                     metrics: RequestMetrics | None = None,
                     context: list[dict[str, str]] | None = None,
                     context_header: str | None = None) -> ModelReply:
        metrics = metrics or RequestMetrics()
        connection, key = self.resolve(preset)
        if connection.protocol == "mock":
            mode = connection.base_url or "first"
            if mode == "invalid_once" and "correction" not in position:
                return ModelReply(text='{"move":"z9z8"}', input_tokens=0, output_tokens=0, raw={"mock": True})
            if mode.startswith("prefer:"):
                wanted = mode.split(":", 1)[1]
                move = wanted if wanted in position["legal_moves"] else position["legal_moves"][0]
            else:
                move = position["legal_moves"][-1 if mode == "last" else 0]
            return ModelReply(text=json.dumps({"move": move}), input_tokens=0, output_tokens=0, raw={"mock": True})
        headers = dict(connection.extra_headers)
        payload_text = serialize_position(position)
        if context_header:
            payload_text = f"{context_header}\n{payload_text}"
        messages = list(context or []) + [{"role": "user", "content": payload_text}]
        async with httpx.AsyncClient(timeout=timeout) as client:
            if connection.protocol == "openai_chat":
                headers["Authorization"] = f"Bearer {key}"
                body: dict[str, Any] = {"model": preset.model, "messages": [{"role":"system","content":SYSTEM_PROMPT}, *messages], "max_tokens":preset.max_tokens}
                if preset.temperature is not None: body["temperature"] = preset.temperature
                if preset.thinking: body["thinking"] = {"type": preset.thinking}
                if preset.reasoning_effort: body["reasoning_effort"] = preset.reasoning_effort
                if preset.structured_output: body["response_format"] = {"type":"json_schema","json_schema":{"name":"xiangqi_move","strict":True,"schema":MOVE_SCHEMA}}
                data = await _post_json(client, connection.base_url.rstrip("/") + "/chat/completions", headers, body, metrics)
                usage = data.get("usage", {}); choice = data["choices"][0]
                text = choice["message"].get("content") or ""
                return ModelReply(text=text, input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
                                  finish_reason=choice.get("finish_reason"), raw=data, **_cache_usage(usage, connection.protocol))
            if connection.protocol == "openai_responses":
                headers["Authorization"] = f"Bearer {key}"
                body = {"model":preset.model,"instructions":SYSTEM_PROMPT,"input":messages,"max_output_tokens":preset.max_tokens,"store":False}
                if cache_key := _prompt_cache_key(preset, position, connection):
                    body["prompt_cache_key"] = cache_key
                if preset.temperature is not None: body["temperature"] = preset.temperature
                if preset.reasoning_effort: body["reasoning"] = {"effort":preset.reasoning_effort}
                if preset.structured_output: body["text"] = {"format":{"type":"json_schema","name":"xiangqi_move","strict":True,"schema":MOVE_SCHEMA}}
                data = await _post_json(client, connection.base_url.rstrip("/") + "/responses", headers, body, metrics)
                text = data.get("output_text") or _responses_text(data); usage = data.get("usage", {})
                return ModelReply(text=text, input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
                                  finish_reason=data.get("status"), raw=data, **_cache_usage(usage, connection.protocol))
            headers.update({"x-api-key":key,"anthropic-version":"2023-06-01"})
            system: str | list[dict[str, Any]] = SYSTEM_PROMPT
            if _official_anthropic(connection.base_url):
                system = [{"type": "text", "text": SYSTEM_PROMPT,
                           "cache_control": {"type": "ephemeral"}}]
            anthropic_messages: list[dict[str, Any]] = messages
            if _official_anthropic(connection.base_url):
                anthropic_messages = [dict(message) for message in messages]
                last = dict(anthropic_messages[-1])
                last["content"] = [{"type": "text", "text": last["content"],
                                    "cache_control": {"type": "ephemeral"}}]
                anthropic_messages[-1] = last
            body = {"model":preset.model,"system":system,"messages":anthropic_messages,"max_tokens":preset.max_tokens}
            if preset.temperature is not None: body["temperature"] = preset.temperature
            output_config: dict[str, Any] = {}
            if preset.structured_output: output_config["format"] = {"type":"json_schema","schema":MOVE_SCHEMA}
            if preset.reasoning_effort: output_config["effort"] = preset.reasoning_effort
            if output_config: body["output_config"] = output_config
            data = await _post_json(client, connection.base_url.rstrip("/") + "/v1/messages", headers, body, metrics)
            text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
            usage = data.get("usage", {})
            return ModelReply(text=text, input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
                              finish_reason=data.get("stop_reason"), raw=data, **_cache_usage(usage, connection.protocol))


MOVE_PATTERN = re.compile(r"^[a-i][0-9][a-i][0-9]$")
JSON_MOVE = re.compile(r'\{[^{}]*"move"\s*:\s*"([a-i][0-9][a-i][0-9])"[^{}]*\}')
ANY_MOVE = re.compile(r"[a-i][0-9][a-i][0-9]")


def parse_move(text: str) -> str:
    """Accept {"move":"b2e2"}, a fenced JSON block, a bare coordinate, or prose."""
    try:
        value = json.loads(text)
        move = value.get("move") if isinstance(value, dict) else value if isinstance(value, str) else None
    except (json.JSONDecodeError, TypeError):
        move = None
    if not isinstance(move, str) or not MOVE_PATTERN.match(move.strip().lower()):
        embedded = JSON_MOVE.search(text)
        found = embedded or ANY_MOVE.search(text.lower())
        move = found.group(1) if embedded else (found.group(0) if found else None)
    if not isinstance(move, str) or not MOVE_PATTERN.match(move.strip().lower()):
        raise ValueError("Response does not contain a coordinate move")
    return move.strip().lower()


def parse_note(text: str) -> str | None:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    note = value.get("note") if isinstance(value, dict) else None
    if not isinstance(note, str):
        return None
    note = note.strip()
    return note[:300] or None


def _responses_text(data: dict) -> str:
    return "".join(part.get("text", "") for item in data.get("output", []) for part in item.get("content", []) if part.get("type") == "output_text")


def serialize_position(position: dict[str, Any]) -> str:
    """Canonical position text so the same context prefix is byte-for-byte stable."""
    return json.dumps(position, ensure_ascii=False, separators=(",", ":"))


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _cache_usage(usage: Any, protocol: str) -> dict[str, int | None]:
    """Normalize provider-specific prompt-cache usage into one schema."""
    if not isinstance(usage, dict):
        return {"total_input_tokens": None, "cache_read_tokens": None,
                "cache_write_tokens": None, "cache_miss_tokens": None}
    details = usage.get("input_tokens_details")
    details = details if isinstance(details, dict) else {}
    if protocol == "openai_chat":
        total = _int(usage.get("prompt_tokens"))
        prompt_details = usage.get("prompt_tokens_details")
        prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
        hit = _int(usage.get("prompt_cache_hit_tokens"))
        if hit is None:
            hit = _int(prompt_details.get("cached_tokens"))
        miss = _int(usage.get("prompt_cache_miss_tokens"))
        write = _int(prompt_details.get("cache_write_tokens"))
    elif protocol == "openai_responses":
        total = _int(usage.get("input_tokens"))
        hit = _int(details.get("cached_tokens"))
        write = _int(details.get("cache_write_tokens"))
        miss = max(total - hit, 0) if total is not None and hit is not None else None
    else:
        ordinary = _int(usage.get("input_tokens"))
        hit = _int(usage.get("cache_read_input_tokens"))
        write = _int(usage.get("cache_creation_input_tokens"))
        total = sum(value for value in (ordinary, hit, write) if value is not None) if any(
            value is not None for value in (ordinary, hit, write)) else None
        miss = (ordinary or 0) + (write or 0) if total is not None else None
    if total is None and hit is not None and miss is not None:
        total = hit + miss
    if miss is None and total is not None and hit is not None:
        miss = max(total - hit, 0)
    return {"total_input_tokens": total, "cache_read_tokens": hit,
            "cache_write_tokens": write, "cache_miss_tokens": miss}


def _official_anthropic(base_url: str) -> bool:
    return (urlparse(base_url).hostname or "").lower().endswith("anthropic.com")


def _prompt_cache_key(preset: Preset, position: dict[str, Any], connection: Any) -> str | None:
    if connection.protocol != "openai_responses":
        return None
    host = (urlparse(connection.base_url).hostname or "").lower()
    if not host.endswith("openai.com"):
        return None
    # Keep the key stable across games and colours.  The game state is already
    # part of the request prefix; including it here would create a fresh cache
    # bucket for every game and defeat reuse of the shared prompt prefix.
    connection_id = getattr(connection, "id", "connection")
    seed = f"{PROMPT_VERSION}:{connection_id}:{preset.model}:{preset.reasoning_effort or 'default'}"
    return "xq-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:40]


async def _post_json(client: httpx.AsyncClient, url: str, headers: dict[str, str],
                     body: dict[str, Any], metrics: RequestMetrics) -> dict[str, Any]:
    metrics.reset()
    metrics.actual_request = {key: body[key] for key in
        ("model", "max_tokens", "max_output_tokens", "temperature", "thinking",
         "reasoning_effort", "reasoning", "response_format", "output_config", "text") if key in body}
    request = client.build_request("POST", url, headers=headers, json=body,
                                   extensions={"trace": metrics.trace})
    response = await client.send(request, stream=True)
    for header in ("x-request-id", "request-id", "x-amzn-requestid", "x-goog-request-id", "cf-ray"):
        if response.headers.get(header):
            metrics.provider_request_id = response.headers[header]
            break
    response.raise_for_status()
    chunks: list[bytes] = []
    try:
        async for chunk in response.aiter_bytes():
            if metrics.first_byte_ms is None:
                metrics.first_byte_ms = int((time.perf_counter() - metrics.started) * 1000)
            chunks.append(chunk)
    finally:
        await response.aclose()
    data = json.loads(b"".join(chunks))
    if metrics.provider_request_id is None and isinstance(data.get("id"), str):
        metrics.provider_request_id = data["id"]
    return data
