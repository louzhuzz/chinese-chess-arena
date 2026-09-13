from __future__ import annotations

import asyncio
import json
import hashlib
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import ConfigStore
from .schemas import ModelReply, Preset

PROMPT_VERSION = "xiangqi-move-v6-submit-once"
AGENT_PROMPT_VERSION = "xiangqi-agent-v1-tools"
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

AGENT_SYSTEM_PROMPT = """你是中国象棋擂台的棋手，为 player_side 走当前这一手。棋盘事实由平台维护，你负责选招。

坐标 a0—i9 固定红方视角；player_side 的棋子向行号增加（红）或减少（黑）的方向前进。棋种 rook=车、knight=马、cannon=炮、bishop=相/象、advisor=仕/士、king=帅/将、pawn=兵/卒。

每一手你收到一份完整棋子位置表（未列出的格子为空）、手数、行棋方、将军状态、position_id 和你的私有笔记。你可以在同一手里连续行动：先核实事实，再正式提交；也可以直接提交。

可用工具（只给规则事实，不含评分、推荐或搜索）：
- get_position：重新读取完整局面与 position_id。
- get_legal_moves(from?)：全部合法走法，或某一枚棋子的合法走法。
- check_move(move)：这一步是否合法；不合法会说明原因（起点无子、路径阻挡、炮架、马腿、象眼等）。它不落子。
- simulate_line(moves)：只执行你自己提出的变化，返回每步合法性、吃子、将军与终局判定。它不改动真实棋局，也不返回评分。
- get_history(start,end)：真实棋谱的某一段。
- read_note / write_note：读写你自己的短笔记（最多 400 字，写入即替换）。笔记是你的判断，不是平台事实；每手以最新棋盘为准。
- submit_move(move, position_id)：正式落子并结束本手。只调用一次；重复提交或用过期 position_id 会被拒绝。

纪律：
- 不要机械地走一套工具流程。开局一步普通跳马这类已确定的选择，直接 submit_move。
- 只有当你需要确认某一步是否合法、某个变化会走到什么局面，或需要回想早先棋谱时，才调用工具。
- check_move 或 simulate_line 说非法属于正常探索，改试其它候选即可；正式提交非法才会被要求纠正。
- 选好合法着法就立即 submit_move，不要为了好看反复试走，也不要等到预算耗尽。
- 时间与轮数都是整手共享的：临近预算时平台只保留 submit_move，届时直接提交你已经选定的合法着法。"""


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

    async def choose_with_tools(self, preset: Preset, messages: list[dict[str, Any]],
                                tools: list[dict[str, Any]], timeout: float,
                                metrics: RequestMetrics | None = None,
                                system_prompt: str = AGENT_SYSTEM_PROMPT) -> ToolReply:
        """一次带回工具调用的请求。messages 用中性格式，各协议在这里转换。

        中性消息：{"role":"user","content":str} / {"role":"assistant","content":str,"calls":[...]} /
        {"role":"tool","call_id":str,"name":str,"content":str}
        """
        metrics = metrics or RequestMetrics()
        connection, key = self.resolve(preset)
        if connection.protocol == "mock":
            policy = connection.base_url or "agent-check"
            if policy == "agent-timeout-once" and not _is_retry(messages):
                await asyncio.sleep(float(os.getenv("XIANGQI_MOCK_TIMEOUT_SLEEP", "30")) + timeout)
            return _mock_agent_reply(policy, messages, tools)
        headers = dict(connection.extra_headers)
        async with httpx.AsyncClient(timeout=timeout) as client:
            if connection.protocol == "openai_chat":
                headers["Authorization"] = f"Bearer {key}"
                body: dict[str, Any] = {
                    "model": preset.model, "max_tokens": preset.max_tokens,
                    "messages": [{"role": "system", "content": system_prompt}, *_openai_chat_messages(messages)],
                    "tools": [{"type": "function", "function": spec} for spec in tools],
                    "tool_choice": "auto",
                }
                if preset.temperature is not None: body["temperature"] = preset.temperature
                if preset.thinking: body["thinking"] = {"type": preset.thinking}
                if preset.reasoning_effort: body["reasoning_effort"] = preset.reasoning_effort
                data = await _post_json(client, connection.base_url.rstrip("/") + "/chat/completions", headers, body, metrics)
                choice = data["choices"][0]; message = choice.get("message") or {}; usage = data.get("usage", {})
                calls = [_tool_call(item.get("id"), item.get("function") or {}) for item in (message.get("tool_calls") or [])]
                # 有些兼容网关（Command Code 等）用 reasoning / reasoning_details 而不是
                # reasoning_content；提供方给了什么就原样带回去续接。
                reasoning_extra = {key: message[key] for key in ("reasoning", "reasoning_details")
                                   if message.get(key) not in (None, "", [])}
                return ToolReply(text=message.get("content") or "", calls=calls, finish_reason=choice.get("finish_reason"),
                                 input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
                                 raw=data, reasoning_content=message.get("reasoning_content"),
                                 reasoning_extra=reasoning_extra,
                                 **_cache_usage(usage, connection.protocol))
            if connection.protocol == "openai_responses":
                headers["Authorization"] = f"Bearer {key}"
                body = {"model": preset.model, "instructions": system_prompt, "input": _responses_input(messages),
                        "max_output_tokens": preset.max_tokens, "store": False,
                        "tools": [{"type": "function", **spec} for spec in tools], "tool_choice": "auto"}
                if preset.temperature is not None: body["temperature"] = preset.temperature
                if preset.reasoning_effort: body["reasoning"] = {"effort": preset.reasoning_effort}
                data = await _post_json(client, connection.base_url.rstrip("/") + "/responses", headers, body, metrics)
                usage = data.get("usage", {})
                reasoning_items = [item for item in (data.get("output") or [])
                                   if isinstance(item, dict) and item.get("type") == "reasoning"]
                return ToolReply(text=data.get("output_text") or _responses_text(data), calls=_responses_calls(data),
                                 finish_reason=data.get("status"), input_tokens=usage.get("input_tokens"),
                                 output_tokens=usage.get("output_tokens"), raw=data, reasoning_items=reasoning_items,
                                 **_cache_usage(usage, connection.protocol))
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
            system: str | list[dict[str, Any]] = system_prompt
            if _official_anthropic(connection.base_url):
                system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
            body = {"model": preset.model, "system": system, "max_tokens": preset.max_tokens,
                    "messages": _anthropic_agent_messages(messages),
                    "tools": [{"name": spec["name"], "description": spec["description"],
                               "input_schema": spec["parameters"]} for spec in tools]}
            if preset.temperature is not None: body["temperature"] = preset.temperature
            if preset.thinking: body["thinking"] = {"type": preset.thinking}
            if preset.reasoning_effort: body["output_config"] = {"effort": preset.reasoning_effort}
            data = await _post_json(client, connection.base_url.rstrip("/") + "/v1/messages", headers, body, metrics)
            blocks = data.get("content", [])
            text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
            calls = [ToolCall(id=block.get("id") or f"call-{index}", name=block.get("name") or "",
                              arguments=block.get("input") if isinstance(block.get("input"), dict) else {})
                     for index, block in enumerate(blocks) if block.get("type") == "tool_use"]
            usage = data.get("usage", {})
            thinking_blocks = [block for block in blocks if block.get("type") in {"thinking", "redacted_thinking"}]
            return ToolReply(text=text, calls=calls, finish_reason=data.get("stop_reason"),
                             input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
                             raw=data, thinking_blocks=thinking_blocks, **_cache_usage(usage, connection.protocol))


class ToolCall:
    """一次模型提出的工具调用。"""

    def __init__(self, id: str, name: str, arguments: dict[str, Any] | None = None):
        self.id = id
        self.name = name
        self.arguments = arguments or {}

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"ToolCall({self.id!r}, {self.name!r}, {self.arguments!r})"


class ToolReply:
    """带工具调用的回复；token 字段与 ModelReply 同名，便于统一记账。"""

    def __init__(self, *, text: str = "", calls: list[ToolCall] | None = None, finish_reason: str | None = None,
                 input_tokens: int | None = None, output_tokens: int | None = None,
                 total_input_tokens: int | None = None, cache_read_tokens: int | None = None,
                 cache_write_tokens: int | None = None, cache_miss_tokens: int | None = None,
                 raw: dict[str, Any] | None = None, reasoning_content: str | None = None,
                 reasoning_items: list[dict[str, Any]] | None = None,
                 thinking_blocks: list[dict[str, Any]] | None = None,
                 reasoning_extra: dict[str, Any] | None = None):
        self.text = text
        self.calls = calls or []
        self.finish_reason = finish_reason
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_input_tokens = total_input_tokens
        self.cache_read_tokens = cache_read_tokens
        self.cache_write_tokens = cache_write_tokens
        self.cache_miss_tokens = cache_miss_tokens
        self.raw = raw or {}
        self.reasoning_content = reasoning_content
        self.reasoning_items = reasoning_items or []
        self.thinking_blocks = thinking_blocks or []
        # 提供方自己给出的、需要原样回传的推理字段（如 OpenAI 兼容网关的 reasoning /
        # reasoning_details）：只在工具轮次里按原键回传，平台不解释、不改写。
        self.reasoning_extra = reasoning_extra or {}


def _tool_call(call_id: Any, function: dict[str, Any]) -> ToolCall:
    raw = function.get("arguments")
    if isinstance(raw, dict):
        arguments = raw
    else:
        try:
            arguments = json.loads(raw or "{}")
        except json.JSONDecodeError:
            arguments = {"__unparsed__": raw}
    if not isinstance(arguments, dict):
        arguments = {"__unparsed__": arguments}
    return ToolCall(str(call_id or f"call-{function.get('name')}"), str(function.get("name") or ""), arguments)


def _responses_calls(data: dict[str, Any]) -> list[ToolCall]:
    calls = []
    for index, item in enumerate(data.get("output") or []):
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        raw = item.get("arguments")
        try:
            arguments = json.loads(raw or "{}")
        except json.JSONDecodeError:
            arguments = {"__unparsed__": raw}
        calls.append(ToolCall(str(item.get("call_id") or item.get("id") or f"call-{index}"),
                              str(item.get("name") or ""), arguments if isinstance(arguments, dict) else {}))
    return calls


def _openai_chat_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            converted.append({"role": "tool", "tool_call_id": message.get("call_id"),
                              "content": message.get("content") or ""})
            continue
        if role == "assistant" and message.get("calls"):
            assistant = {
                "role": "assistant",
                "content": message.get("content") or None,
                "tool_calls": [{"id": call.id, "type": "function",
                                "function": {"name": call.name,
                                             "arguments": json.dumps(call.arguments, ensure_ascii=False)}}
                               for call in message["calls"]],
            }
            if message.get("reasoning_content") is not None:
                assistant["reasoning_content"] = message["reasoning_content"]
            for key, value in (message.get("reasoning_extra") or {}).items():
                assistant.setdefault(key, value)
            converted.append(assistant)
            continue
        if role == "assistant" and (message.get("reasoning_content") is not None or message.get("reasoning_extra")):
            assistant = {"role": "assistant", "content": message.get("content") or ""}
            if message.get("reasoning_content") is not None:
                assistant["reasoning_content"] = message["reasoning_content"]
            for key, value in (message.get("reasoning_extra") or {}).items():
                assistant.setdefault(key, value)
            converted.append(assistant)
            continue
        converted.append({"role": role, "content": message.get("content") or ""})
    return converted


def _responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            items.append({"type": "function_call_output", "call_id": message.get("call_id"),
                          "output": message.get("content") or ""})
            continue
        if role == "assistant" and message.get("calls"):
            items.extend(message.get("reasoning_items") or [])
            if message.get("content"):
                items.append({"role": "assistant", "content": message["content"]})
            for call in message["calls"]:
                items.append({"type": "function_call", "call_id": call.id, "name": call.name,
                              "arguments": json.dumps(call.arguments, ensure_ascii=False)})
            continue
        if role == "assistant" and message.get("reasoning_items"):
            items.extend(message["reasoning_items"])
        items.append({"role": role, "content": message.get("content") or ""})
    return items


def _anthropic_agent_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            block = {"type": "tool_result", "tool_use_id": message.get("call_id"),
                     "content": message.get("content") or ""}
            if converted and converted[-1]["role"] == "user" and isinstance(converted[-1]["content"], list):
                converted[-1]["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block]})
            continue
        if role == "assistant" and message.get("calls"):
            blocks: list[dict[str, Any]] = list(message.get("thinking_blocks") or [])
            if message.get("content"):
                blocks.append({"type": "text", "text": message["content"]})
            for call in message["calls"]:
                blocks.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments})
            converted.append({"role": "assistant", "content": blocks})
            continue
        if role == "assistant" and message.get("thinking_blocks"):
            blocks = [*message["thinking_blocks"]]
            if message.get("content"):
                blocks.append({"type": "text", "text": message["content"]})
            converted.append({"role": "assistant", "content": blocks})
            continue
        converted.append({"role": role, "content": message.get("content") or ""})
    return converted


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


def describe_error(exc: BaseException) -> str:
    """接口异常的可读描述。

    httpx 的 ConnectError / ReadTimeout 等 `str()` 经常是空串，只看它排查不出任何东西，
    所以这里把异常链（`__cause__`/`__context__`）里的底层原因一起带出来，
    并在可用时附上「方法 + 主机 + 路径」（不含查询串，避免把密钥写进日志）。
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).strip() or repr(current)
        if text and text not in parts:
            parts.append(text)
        current = current.__cause__ or current.__context__
    detail = " ← ".join(parts) or type(exc).__name__
    request = getattr(exc, "request", None)
    url = getattr(request, "url", None)
    if url is not None:
        target = f"{url.scheme}://{url.host}{url.path}"
        method = getattr(request, "method", "")
        detail = f"{method} {target} ← {detail}".strip()
    return detail


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


# ---- mock 智能体：让工具循环可以在没有网络的情况下被完整测试 ----
# 策略写在连接的 base_url 上，与既有的 mock 直接模式同一约定。
MOCK_AGENT_POLICIES = (
    "agent-check", "agent-illegal-fix", "agent-simulate", "agent-note",
    "agent-stale", "agent-illegal-submit", "agent-text", "agent-refuse",
    "agent-timeout-once",
)

ILLEGAL_SAMPLE = "a0a9"  # 红车直上被自己兵挡住：合法格式但非法着法，用来驱动“发现错误再修正”


def _is_retry(messages: list[dict[str, Any]]) -> bool:
    """这一轮是不是「上次超时/故障后只换一次提交」的那一轮。"""
    return any(isinstance(message.get("content"), str) and message["content"].startswith("上一次请求")
               for message in messages)


def _mock_view(messages: list[dict[str, Any]]) -> tuple[list[str], str | None]:
    """从对话里取出已知的合法着法与 position_id（mock 只读，不自己算棋）。"""
    legal: list[str] = []
    position_id: str | None = None
    for message in messages:
        text = message.get("content")
        if not isinstance(text, str):
            continue
        if message.get("role") == "tool":
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                if isinstance(data.get("moves"), list):
                    legal = [item for item in data["moves"] if isinstance(item, str)] or legal
                if isinstance(data.get("position_id"), str):
                    position_id = data["position_id"]
            continue
        if message.get("role") == "user":
            if position_id is None:
                match = re.search(r'"position_id"\s*:\s*"([^"]+)"', text)
                if match:
                    position_id = match.group(1)
            if not legal:
                found = re.search(r'"legal_moves"\s*:\s*\[([^\]]*)\]', text)
                if found:
                    legal = re.findall(r'"([a-i][0-9][a-i][0-9])"', found.group(1))
    return legal, position_id


def _mock_agent_reply(policy: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ToolReply:
    available = [spec["name"] for spec in tools]
    steps = sum(1 for message in messages if message.get("role") == "assistant" and message.get("calls"))
    legal, position_id = _mock_view(messages)
    first = legal[0] if legal else "h2e2"

    def reply(*calls: ToolCall, text: str = "") -> ToolReply:
        return ToolReply(text=text, calls=list(calls), finish_reason="tool_calls" if calls else "stop",
                         input_tokens=11, output_tokens=3,
                         raw={"mock_agent": policy, "step": steps, "tools": available})

    def call(name: str, **arguments: Any) -> ToolCall:
        return ToolCall(f"mock-{steps}-{name}", name, arguments)

    if policy == "agent-refuse":  # 永远不提交：用来验证预算/轮数耗尽后的失败记录
        return reply(text="我还在考虑，不提交。")
    if policy == "agent-text":  # 不用工具提交，只在正文里给出着法：验证文本兜底路径
        if steps == 0 and "get_legal_moves" in available:
            return reply(call("get_legal_moves"))
        return reply(text=json.dumps({"move": first}, ensure_ascii=False))

    def submit(move: str, pid: str | None = None) -> ToolCall:
        return call("submit_move", move=move, **( {"position_id": pid} if pid else {}))

    if policy == "agent-timeout-once":  # 第一次请求必超时（sleep 在 choose_with_tools 里），重试时只提交
        return reply(submit("h2e2", position_id))

    plans: dict[str, list[tuple[str, dict[str, Any]]]] = {
        "agent-check": [("get_legal_moves", {}), ("check_move", {"move": first}), ("submit_move", {"move": first, "position_id": position_id})],
        "agent-illegal-fix": [("get_legal_moves", {}), ("check_move", {"move": ILLEGAL_SAMPLE}), ("submit_move", {"move": first, "position_id": position_id})],
        "agent-simulate": [("get_legal_moves", {}),
                           ("simulate_line", {"moves": [first, legal[1] if len(legal) > 1 else first]}),
                           ("submit_move", {"move": first, "position_id": position_id})],
        "agent-note": [("write_note", {"text": "计划：先出中炮，注意中路。"}), ("read_note", {}),
                       ("get_legal_moves", {}), ("submit_move", {"move": first, "position_id": position_id})],
        "agent-stale": [("get_legal_moves", {}),
                        ("submit_move", {"move": first, "position_id": "stale-position-1"}),
                        ("submit_move", {"move": first, "position_id": position_id})],
        "agent-illegal-submit": [("get_legal_moves", {}), ("submit_move", {"move": ILLEGAL_SAMPLE}),
                                 ("submit_move", {"move": first, "position_id": position_id})],
    }

    plan = plans.get(policy, plans["agent-check"])
    if steps < len(plan):
        name, arguments = plan[steps]
        if name in available:
            return reply(call(name, **arguments))
    # 计划走完（或该工具在当前预算阶段不可用）：能提交就提交，否则退回纯文本
    if "submit_move" in available:
        return reply(submit(first, position_id))
    if "get_position" in available:
        return reply(call("get_position"))
    return reply(text=json.dumps({"move": first}, ensure_ascii=False))
