"""一手之内的智能体循环。

一次落子 = 若干轮「模型请求 → 工具执行 → 结果回传」，直到模型正式提交、预算耗尽或轮数用尽。
预算按整手管理：时间、轮数、输出用量都在这一手里累计，单次请求另有自己的时限；
临近结束时只保留 submit_move，并要求模型提交自己已经选定的合法着法。

本模块只依赖 tools.py 与 models.py 的接口，落子本身仍由 GameRunner 写入数据库。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .models import AGENT_SYSTEM_PROMPT, RequestMetrics, ToolReply, describe_error, parse_move
from .tools import RuleTools, TOOL_SPECS, SUBMIT_ONLY

AGENT_MAX_ROUNDS = int(os.getenv("XIANGQI_AGENT_MAX_ROUNDS", "6"))
SUBMIT_RESERVE_RATIO = float(os.getenv("XIANGQI_AGENT_SUBMIT_RESERVE", "0.2"))
SUBMIT_RESERVE_CAP = float(os.getenv("XIANGQI_AGENT_SUBMIT_RESERVE_CAP", "30"))
REQUEST_CAP_RATIO = float(os.getenv("XIANGQI_AGENT_REQUEST_CAP", "0.4"))
REQUEST_CAP_FLOOR = float(os.getenv("XIANGQI_AGENT_REQUEST_CAP_FLOOR", "30"))
MIN_USEFUL_REQUEST = float(os.getenv("XIANGQI_AGENT_MIN_REQUEST", "15"))
# 实测一次请求要多久 × 这个系数，才是「还值得再发一次请求」的剩余时间。
REQUEST_MARGIN_RATIO = float(os.getenv("XIANGQI_AGENT_REQUEST_MARGIN", "1.2"))
# 落在提交阶段的请求，留这么多秒给「收下这一步、写库、推事件」。
SUBMIT_SLACK = 1.0

TERMINAL_ERRORS = {"timeout", "cancelled", "api_error"}


@dataclass
class AgentAction:
    """一条可回放的行动：一次请求、一次工具调用或一次提交。"""

    sequence: int
    kind: str                      # request | tool | note | submit | text_submit
    name: str | None = None
    args: dict[str, Any] | None = None
    result: Any = None
    text: str = ""
    duration_ms: int | None = None
    error: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_input_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    connect_ms: int | None = None
    first_byte_ms: int | None = None
    provider_request_id: str | None = None
    finish_reason: str | None = None
    request: dict[str, Any] | None = None   # 本轮真正发出去的参数（已去密钥），用于核对「参数有没有生效」
    raw: dict[str, Any] | None = None       # 提供方原始回复，用于核对「输出耗尽还是接口异常」

    def to_row(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "name": self.name,
            "args_json": json.dumps(self.args, ensure_ascii=False) if self.args is not None else None,
            "result_json": json.dumps(self.result, ensure_ascii=False) if self.result is not None else None,
            "text": self.text or None, "duration_ms": self.duration_ms, "error": self.error,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "total_input_tokens": self.total_input_tokens, "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens, "connect_ms": self.connect_ms,
            "first_byte_ms": self.first_byte_ms, "provider_request_id": self.provider_request_id,
            "finish_reason": self.finish_reason,
            "request_json": json.dumps(self.request, ensure_ascii=False) if self.request else None,
            "raw_json": json.dumps(self.raw, ensure_ascii=False) if self.raw else None,
        }


@dataclass
class TurnOutcome:
    move: str | None = None
    error: str | None = None
    actions: list[AgentAction] = field(default_factory=list)
    requests: int = 0
    rounds_limit_hit: bool = False
    output_tokens: int | None = 0
    input_tokens: int | None = 0
    cache_read_tokens: int | None = 0
    cache_write_tokens: int | None = 0
    note: str | None = None


class AgentTurn:
    """把一手棋当作一个小型循环来跑。"""

    def __init__(self, *, client, preset, tools: RuleTools, side: str, ply: int, deadline: float,
                 position_message: str, max_rounds: int = AGENT_MAX_ROUNDS,
                 cancelled: Callable[[], bool] | None = None, system_prompt: str = AGENT_SYSTEM_PROMPT,
                 request_metrics_factory: Callable[[], RequestMetrics] = RequestMetrics,
                 on_action: Callable[[AgentAction], Awaitable[None]] | None = None):
        self.client = client
        self.preset = preset
        self.tools = tools
        self.side = side
        self.ply = ply
        self.deadline = deadline
        self.started_at = time.monotonic()
        self.total_budget = max(1.0, deadline - self.started_at)
        self.position_message = position_message
        self.max_rounds = max(1, max_rounds)
        self.cancelled = cancelled or (lambda: False)
        self.system_prompt = system_prompt
        self.metrics_factory = request_metrics_factory
        self.on_action = on_action
        self.request_seconds: list[float] = []   # 本手每次请求的实测耗时

    async def _emit(self, action: AgentAction, outcome: TurnOutcome) -> None:
        """逐条落盘 + 推事件：网页不必等整手结束就能看到行动链条。"""
        outcome.actions.append(action)
        if self.on_action is None:
            return
        try:
            await self.on_action(action)
        except Exception:  # noqa: BLE001 - 记录失败不该打断这一手
            pass

    # ---- 预算 ----

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def measured_request_seconds(self) -> float:
        """最近一次请求的实测耗时；还没发过请求时为 0。"""
        return self.request_seconds[-1] if self.request_seconds else 0.0

    def submit_window_threshold(self) -> float:
        """进入「只允许提交」的时间阈值。

        取整手预算的 20%（上限 30 秒，且不超过整手的三分之一）；
        但如果这一手已经实测出单次请求要多久，就按「实测耗时 × 1.2」抬高阈值：
        剩下的时间不够再发一次请求时，直接进入提交窗口，而不是发出去再超时。
        """
        budget = self.total_budget
        base = max(1.0, min(budget * SUBMIT_RESERVE_RATIO, SUBMIT_RESERVE_CAP, budget / 3))
        measured = self.measured_request_seconds() * REQUEST_MARGIN_RATIO
        if measured <= 0:
            return base
        return max(base, min(measured, max(1.0, budget - 1.0)))

    def useful_request_floor(self) -> float:
        """一次请求还值得发出去的最短剩余时间；短预算下同样按比例缩小。"""
        base = max(1.0, min(MIN_USEFUL_REQUEST, self.total_budget / 3))
        measured = self.measured_request_seconds() * REQUEST_MARGIN_RATIO
        if measured <= 0:
            return base
        return max(base, min(measured, self.submit_window_threshold()))

    def submit_reserve(self) -> float:
        """为正式提交保留的时间，不超过阈值本身。"""
        return min(max(0.0, self.remaining()), self.submit_window_threshold())

    def in_submit_window(self) -> bool:
        remaining = max(0.0, self.remaining())
        if remaining <= self.submit_window_threshold():
            return True
        return remaining - self.submit_reserve() < self.useful_request_floor()

    def request_budget(self, submit_only: bool = False) -> float:
        """单次请求时限：提交窗口内给完剩余时间，否则不超过整手的一半。

        `submit_only=True` 表示这次请求本身就是「上一次超时/故障后只换一次提交」的那一次，
        此时不再为后面的提交预留时间，直接把剩下的时间全给它。
        """
        remaining = max(0.0, self.remaining())
        if remaining <= 0:
            return 0.0
        if submit_only or self.in_submit_window():
            return max(1.0, remaining - SUBMIT_SLACK)
        usable = remaining - self.submit_reserve()
        cap = max(REQUEST_CAP_FLOOR, self.total_budget * REQUEST_CAP_RATIO)
        return max(1.0, min(usable, cap))

    def retry_floor(self) -> float:
        """还值得为「只提交一次」再花的最短剩余时间。

        比一次完整请求的门槛低得多：重试只要求模型交一步已经想好的棋。
        取提交预留的四分之一，短预算按比例缩小。
        """
        return max(1.0, min(self.submit_window_threshold() * 0.25, MIN_USEFUL_REQUEST))

    @staticmethod
    def _add_usage(current: int | None, value: int | None) -> int | None:
        return None if current is None or value is None else current + value

    def _can_retry(self, retry_used: bool, round_index: int) -> bool:
        """一次请求超时或接口故障后，还值不值得用剩下的时间再要一次提交。

        与直接模式一致：整手只重试一次；剩余时间太短就不再浪费。
        """
        if retry_used or round_index >= self.max_rounds - 1:
            return False
        return max(0.0, self.remaining()) > self.retry_floor()

    # ---- 主循环 ----

    async def run(self) -> TurnOutcome:
        outcome = TurnOutcome()
        messages: list[dict[str, Any]] = [{"role": "user", "content": self.position_message}]
        asked_to_submit = False
        submit_only = False        # 上一次请求超时/接口故障后，剩下时间只换一次提交
        retry_used = False
        for round_index in range(self.max_rounds):
            if self.cancelled():
                outcome.error = "cancelled"
                return outcome
            if self.remaining() <= 0:
                outcome.error = "timeout"
                break
            if round_index == self.max_rounds - 1:
                outcome.rounds_limit_hit = True
            submit_window = self.in_submit_window()
            if submit_window and not asked_to_submit:
                messages.append({"role": "user", "content": _SUBMIT_WINDOW_MESSAGE})
                asked_to_submit = True
            tools = SUBMIT_ONLY if (submit_window or submit_only) else TOOL_SPECS
            remaining_output = None if outcome.output_tokens is None else self.preset.max_tokens - outcome.output_tokens
            if remaining_output is not None and remaining_output <= 0:
                outcome.error = "agent_output_limit"
                break
            request_preset = self.preset.model_copy(update={
                "max_tokens": max(1, remaining_output) if remaining_output is not None else self.preset.max_tokens
            })
            budget = self.request_budget(submit_only)
            if budget <= 0:
                outcome.error = "timeout"
                break
            metrics = self.metrics_factory()
            started = time.perf_counter()
            action = AgentAction(sequence=len(outcome.actions), kind="request", name="model",
                                 args={"round": round_index + 1, "tools": [spec["name"] for spec in tools],
                                       "max_tokens": request_preset.max_tokens,
                                       "timeout_seconds": round(budget, 3),
                                       "reasoning_effort": request_preset.reasoning_effort},
                                 input_tokens=None)
            try:
                reply: ToolReply = await asyncio.wait_for(
                    self.client.choose_with_tools(request_preset, messages, tools, budget,
                                                  metrics=metrics, system_prompt=self.system_prompt),
                    budget)
            except asyncio.TimeoutError:
                action.duration_ms = int((time.perf_counter() - started) * 1000)
                action.error = "request_timeout"
                action.request = metrics.actual_request
                outcome.input_tokens = outcome.output_tokens = None
                outcome.cache_read_tokens = outcome.cache_write_tokens = None
                await self._emit(action, outcome)
                if self._can_retry(retry_used, round_index):
                    retry_used = True
                    outcome.error = None
                    submit_only = True
                    messages.append({"role": "user", "content": _RETRY_MESSAGE})
                    continue
                outcome.error = "timeout"
                break
            except Exception as exc:  # noqa: BLE001 - 接口故障原样上报
                action.duration_ms = int((time.perf_counter() - started) * 1000)
                action.error = f"api_error: {type(exc).__name__}: {describe_error(exc)}"
                action.request = metrics.actual_request
                outcome.input_tokens = outcome.output_tokens = None
                outcome.cache_read_tokens = outcome.cache_write_tokens = None
                await self._emit(action, outcome)
                if self._can_retry(retry_used, round_index):
                    retry_used = True
                    outcome.error = None
                    submit_only = True
                    messages.append({"role": "user", "content": _RETRY_MESSAGE})
                    continue
                outcome.error = action.error
                break
            action.duration_ms = int((time.perf_counter() - started) * 1000)
            action.input_tokens = reply.input_tokens
            action.output_tokens = reply.output_tokens
            action.total_input_tokens = reply.total_input_tokens
            action.cache_read_tokens = reply.cache_read_tokens
            action.cache_write_tokens = reply.cache_write_tokens
            action.connect_ms = metrics.connect_ms
            action.first_byte_ms = metrics.first_byte_ms
            action.provider_request_id = metrics.provider_request_id
            action.finish_reason = reply.finish_reason
            action.text = reply.text or ""
            action.request = metrics.actual_request
            action.raw = reply.raw or None
            await self._emit(action, outcome)
            self.request_seconds.append(action.duration_ms / 1000 if action.duration_ms else 0.0)
            outcome.requests += 1
            outcome.output_tokens = self._add_usage(outcome.output_tokens, reply.output_tokens)
            input_used = reply.total_input_tokens if reply.total_input_tokens is not None else reply.input_tokens
            outcome.input_tokens = self._add_usage(outcome.input_tokens, input_used)
            outcome.cache_read_tokens = self._add_usage(outcome.cache_read_tokens, reply.cache_read_tokens)
            outcome.cache_write_tokens = self._add_usage(outcome.cache_write_tokens, reply.cache_write_tokens)

            calls = reply.calls
            if not calls:
                # 没有工具调用：接受正文里的着法（兼容不擅长工具调用的模型），否则追问一次
                move = _extract_move(reply.text)
                if move and self.remaining() <= 0:
                    # 正文落子同样受整手截止时间约束：时限已过就不再受理这一步。
                    outcome.error = "timeout"
                    return outcome
                if move:
                    decision = AgentAction(sequence=len(outcome.actions), kind="text_submit", name="submit_move",
                                           args={"move": move, "via": "text"})
                    result = self.tools.submit_move(move, self.tools.position_id)
                    decision.result = result.payload
                    decision.error = None if result.ok else "rejected"
                    await self._emit(decision, outcome)
                    if result.ok:
                        outcome.move = result.submitted
                        return outcome
                if reply.text.strip():
                    messages.append({
                        "role": "assistant", "content": reply.text,
                        "reasoning_content": reply.reasoning_content,
                        "reasoning_items": reply.reasoning_items,
                        "thinking_blocks": reply.thinking_blocks,
                        "reasoning_extra": reply.reasoning_extra,
                    })
                if round_index == self.max_rounds - 1:
                    break
                messages.append({"role": "user", "content": _NO_TOOL_MESSAGE})
                continue

            assistant_message: dict[str, Any] = {
                "role": "assistant", "content": reply.text or "", "calls": calls,
                "reasoning_content": reply.reasoning_content,
                "reasoning_items": reply.reasoning_items,
                "thinking_blocks": reply.thinking_blocks,
                "reasoning_extra": reply.reasoning_extra,
            }
            messages.append(assistant_message)
            for call in calls:
                if self.cancelled():
                    outcome.error = "cancelled"
                    return outcome
                if self.remaining() <= 0:
                    outcome.error = "timeout"
                    return outcome
                result = await asyncio.to_thread(self.tools.call, call.name, call.arguments)
                if self.remaining() <= 0:
                    outcome.error = "timeout"
                    return outcome
                if result.submitted:
                    # 正式落子只记一条 submit：行动回放里一手就一个落子点
                    await self._emit(AgentAction(sequence=len(outcome.actions), kind="submit", name=call.name,
                                                 args=call.arguments, result=result.payload), outcome)
                    outcome.move = result.submitted
                    return outcome
                tool_action = AgentAction(sequence=len(outcome.actions), kind="tool", name=call.name,
                                          args=call.arguments, result=result.payload, error=None if result.ok else "error")
                await self._emit(tool_action, outcome)
                if call.name == "submit_move" and not result.ok and self.tools.submit_state.attempts >= 2:
                    outcome.error = "invalid_move"
                    return outcome
                if call.name == "write_note" and result.ok:
                    outcome.note = (result.payload or {}).get("private_note", {}).get("text") if isinstance(
                        (result.payload or {}).get("private_note"), dict) else (result.payload or {}).get("private_note")
                messages.append({"role": "tool", "call_id": call.id, "name": call.name, "content": result.render()})
            if self.tools.submit_state.submitted:
                outcome.move = self.tools.submit_state.submitted
                return outcome
        if not outcome.error:
            outcome.error = "agent_no_submit" if not outcome.rounds_limit_hit else "agent_rounds_exhausted"
        return outcome


_SUBMIT_WINDOW_MESSAGE = ("时间快到整手预算了。现在只保留 submit_move：立刻提交你已经选定的合法着法，不要再试走或重新分析。")
_RETRY_MESSAGE = ("上一次请求在期限内没有返回（超时或接口故障）。现在只保留 submit_move："
                  "用剩下的时间立刻提交你已经选定的合法着法，不要再调用其它工具。")
_NO_TOOL_MESSAGE = ("你没有调用任何工具，也没有给出可用的着法。请用 submit_move(move, position_id) 正式提交一步合法走法；"
                    "如果不确定，先用 get_legal_moves 查询。")


def _extract_move(text: str) -> str | None:
    if not text:
        return None
    try:
        return parse_move(text)
    except Exception:  # noqa: BLE001 - 文本兜底解析失败就当没给着法
        return None
