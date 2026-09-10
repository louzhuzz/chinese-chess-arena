"""Native Python rule arbiter for Chinese Chess Arena.

This module has no engine process or search dependency. It replays the complete
history and delegates legality and adjudication to :mod:`backend.app.rules`.
"""
from __future__ import annotations

from dataclasses import dataclass

from .rules import RULESET_ID, START_FEN, adjudicate, to_fen


@dataclass(slots=True)
class Verdict:
    fen: str
    legal_moves: list[str]
    in_check: bool
    ended: bool
    winner: str | None
    reason: str | None
    backend: str = RULESET_ID
    source: str = "none"


class Arbiter:
    """Adjudicate from an initial FEN and the complete move history."""

    def __init__(self, *_args, **_kwargs):
        self.last_error: str | None = None

    @property
    def backend(self) -> str:
        return RULESET_ID

    def describe(self) -> dict:
        return {"backend": RULESET_ID, "implementation": "python", "dependency": None,
                "last_error": self.last_error}

    def inspect(self, initial_fen: str, history: list[str]) -> Verdict:
        try:
            result = adjudicate(initial_fen or START_FEN, list(history))
        except ValueError as exc:
            self.last_error = str(exc)
            raise
        return Verdict(fen=to_fen(result.state), legal_moves=result.legal_moves,
                       in_check=result.in_check, ended=result.ended,
                       winner=result.winner, reason=result.reason,
                       source=result.source)

    def close(self) -> None:
        return None
