from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


Protocol = Literal["openai_chat", "openai_responses", "anthropic_messages", "mock", "human"]


class ConnectionIn(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    name: str
    protocol: Protocol
    base_url: str
    # Set for connections created from the built-in provider catalog.
    # Custom connections leave it empty and keep their explicit fields.
    builtin_provider_id: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    extra_headers: dict[str, str] = Field(default_factory=dict)


class ConnectionOut(BaseModel):
    id: str
    name: str
    protocol: Protocol
    base_url: str
    builtin_provider_id: str | None = None
    api_key_masked: str | None
    api_key_env: str | None
    extra_headers: dict[str, str]


class Preset(BaseModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    name: str
    connection_id: str
    model: str
    temperature: float | None = Field(default=0.2, ge=0, le=2)
    max_tokens: int = Field(default=4096, ge=1, le=131072)
    structured_output: bool = True
    reasoning_effort: Literal["low", "high", "max"] | None = None
    thinking: Literal["enabled", "disabled"] | None = None
    input_price_per_million: float | None = Field(default=None, ge=0)
    output_price_per_million: float | None = Field(default=None, ge=0)


class Piece(BaseModel):
    side: Literal["red", "black"]
    type: Literal["rook", "advisor", "cannon", "pawn", "knight", "bishop", "king"]
    square: str


class PositionView(BaseModel):
    game_id: str
    ply: int
    side_to_move: Literal["red", "black"]
    pieces: list[Piece]
    history: list[str]
    legal_moves: list[str]
    in_check: bool
    ruleset_id: str
    fen: str
    move_timeout_seconds: int = Field(default=120, ge=5, le=3600)
    private_memory: list[dict[str, Any]] = Field(default_factory=list)


class GameCreate(BaseModel):
    red_preset_id: str
    black_preset_id: str
    initial_fen: str | None = None
    move_timeout_seconds: int = Field(default=120, ge=5, le=3600)
    max_plies: int = Field(default=400, ge=1, le=2000)
    red_reasoning_effort: Literal["default", "none", "low", "high", "max"] = "default"
    black_reasoning_effort: Literal["default", "none", "low", "high", "max"] = "default"


class BenchmarkCreate(BaseModel):
    preset_a_id: str
    preset_b_id: str
    pairs: int = Field(default=5, ge=1, le=100)
    initial_fen: str | None = None
    move_timeout_seconds: int = Field(default=120, ge=5, le=3600)
    max_plies: int = Field(default=400, ge=1, le=2000)
    preset_a_reasoning_effort: Literal["default", "none", "low", "high", "max"] = "default"
    preset_b_reasoning_effort: Literal["default", "none", "low", "high", "max"] = "default"


class ModelReply(BaseModel):
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_input_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_miss_tokens: int | None = None
    finish_reason: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class MoveChoice(BaseModel):
    move: str


class MoveSubmit(BaseModel):
    move: str = Field(pattern=r"^[a-i][0-9][a-i][0-9]$")
    expected_ply: int = Field(ge=0)
    expected_side: Literal["red", "black"]


class AnalyzeRequest(BaseModel):
    """分析任意局面：给初始 FEN 加历史，或直接给一个 FEN。"""

    initial_fen: str | None = None
    history: list[str] = Field(default_factory=list)
    fen: str | None = None


class AnalyzeResult(BaseModel):
    fen: str
    side_to_move: Literal["red", "black"]
    pieces: list[Piece]
    legal_moves: list[str]
    in_check: bool
    ended: bool
    winner: str | None
    reason: str | None
    backend: str
    source: str
