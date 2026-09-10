from __future__ import annotations

from backend.app.provider_catalog import builtin_provider_catalog, provider_for_connection
from backend.app.schemas import ConnectionIn


def test_catalog_contains_only_supported_compatible_api_key_routes() -> None:
    providers = builtin_provider_catalog()
    ids = {item["id"] for item in providers}
    assert {"deepseek", "openai", "anthropic", "openrouter", "zai-coding-cn"} <= ids
    assert all(item["supported"] for item in providers)
    assert all(item["auth_mode"] == "api_key" for item in providers)
    assert all(item["protocol"] in {"openai_chat", "openai_responses", "anthropic_messages"}
               for item in providers)
    assert "google" not in ids
    assert "amazon-bedrock" not in ids


def test_legacy_connection_alias_resolves_to_builtin_template() -> None:
    connection = ConnectionIn(
        id="openai-default",
        name="OpenAI",
        protocol="openai_responses",
        base_url="https://api.openai.com/v1",
    )
    provider = provider_for_connection(connection)
    assert provider is not None
    assert provider.id == "openai"
    assert provider.api_key_env == "OPENAI_API_KEY"


def test_builtin_sync_uses_catalog_without_network(api) -> None:
    client, _ = api
    response = client.put("/api/connections/deepseek", json={
        "id": "deepseek",
        "name": "DeepSeek",
        "protocol": "openai_chat",
        "base_url": "https://api.deepseek.com/v1",
        "builtin_provider_id": "deepseek",
        "api_key": None,
        "api_key_env": "DEEPSEEK_API_KEY",
        "extra_headers": {},
    })
    assert response.status_code == 200
    response = client.post("/api/connections/deepseek/sync-models")
    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "catalog"
    assert {"deepseek-flash", "deepseek-v4-pro"} <= {item["model"] for item in payload["models"]}
    assert "deepseek-v4-flash-vision-exp" not in {item["model"] for item in payload["models"]}
