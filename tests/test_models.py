from __future__ import annotations

import pytest

from backend.app.models import _cache_usage, parse_move


@pytest.mark.parametrize("text,expected", [
    ('{"move":"b2e2"}', "b2e2"),
    ('{"move":"B2E2"}', "b2e2"),
    ('{"move": "b2e2", "reason": "center"}', "b2e2"),
    ("b2e2", "b2e2"),
    ("b2e2\n", "b2e2"),
    ("```json\n{\"move\":\"b2e2\"}\n```", "b2e2"),
    ("我认为最好的着法是 b2e2。", "b2e2"),
])
def test_parse_move_accepts_supported_shapes(text, expected):
    assert parse_move(text) == expected


@pytest.mark.parametrize("text", [
    "", "没有着法", '{"move": 42}', '{"move": "z9z8"}', '{"move": "b2"}', "null", "[]",
])
def test_parse_move_rejects_everything_else(text):
    with pytest.raises(ValueError):
        parse_move(text)


def test_cache_usage_is_normalized_without_turning_zero_into_unknown():
    assert _cache_usage({"prompt_tokens": 100, "prompt_cache_hit_tokens": 0,
                         "prompt_cache_miss_tokens": 100}, "openai_chat") == {
        "total_input_tokens": 100, "cache_read_tokens": 0,
        "cache_write_tokens": None, "cache_miss_tokens": 100}
    assert _cache_usage({"input_tokens": 120,
                         "input_tokens_details": {"cached_tokens": 80,
                                                   "cache_write_tokens": 20}},
                        "openai_responses")["cache_miss_tokens"] == 40
    assert _cache_usage({"input_tokens": 10, "cache_read_input_tokens": 90,
                         "cache_creation_input_tokens": 20},
                        "anthropic_messages")["total_input_tokens"] == 120
