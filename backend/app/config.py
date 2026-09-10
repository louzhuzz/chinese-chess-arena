from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from .schemas import ConnectionIn, ConnectionOut, Preset

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.getenv("XIANGQI_CONFIG", ROOT / "config" / "models.yaml"))


class ConfigStore:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        if not path.exists():
            example = path.with_name("models.example.yaml")
            path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")

    def _read(self) -> dict:
        return yaml.safe_load(self.path.read_text(encoding="utf-8")) or {"connections": [], "presets": []}

    def _write(self, data: dict) -> None:
        self.path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def connections(self, reveal: bool = False) -> list[ConnectionIn | ConnectionOut]:
        values = [ConnectionIn.model_validate(item) for item in self._read().get("connections", [])]
        if reveal:
            return values
        return [ConnectionOut(id=c.id, name=c.name, protocol=c.protocol, base_url=c.base_url,
                               builtin_provider_id=c.builtin_provider_id,
                               api_key_masked=_mask(c.api_key), api_key_env=c.api_key_env,
                               extra_headers=_masked_headers(c.extra_headers)) for c in values]

    def presets(self) -> list[Preset]:
        return [Preset.model_validate(item) for item in self._read().get("presets", [])]

    def connection(self, connection_id: str) -> ConnectionIn:
        return next(c for c in self.connections(reveal=True) if c.id == connection_id)

    def preset(self, preset_id: str) -> Preset:
        return next(p for p in self.presets() if p.id == preset_id)

    def upsert_connection(self, value: ConnectionIn) -> None:
        data = self._read(); items = data.setdefault("connections", [])
        existing = next((i for i, item in enumerate(items) if item.get("id") == value.id), None)
        if existing is not None and not value.api_key:
            value.api_key = items[existing].get("api_key")
        if existing is not None and not value.extra_headers:
            value.extra_headers = items[existing].get("extra_headers", {})
        if existing is not None and not value.builtin_provider_id:
            value.builtin_provider_id = items[existing].get("builtin_provider_id")
        payload = value.model_dump(exclude_none=True)
        if existing is None: items.append(payload)
        else: items[existing] = payload
        self._write(data)

    def upsert_preset(self, value: Preset) -> None:
        self.connection(value.connection_id)
        data = self._read(); items = data.setdefault("presets", [])
        existing = next((i for i, item in enumerate(items) if item.get("id") == value.id), None)
        if existing is None: items.append(value.model_dump(exclude_none=True))
        else: items[existing] = value.model_dump(exclude_none=True)
        self._write(data)

    def sync_models(self, connection_id: str, model_ids: list[str]) -> list[Preset]:
        connection = self.connection(connection_id)
        data = self._read(); items = data.setdefault("presets", [])
        existing = {(item.get("connection_id"), item.get("model")) for item in items}
        supports_schema = "deepseek" not in connection.name.lower() and "deepseek" not in connection.base_url.lower()
        for model_id in sorted(set(model_ids)):
            if (connection_id, model_id) in existing:
                continue
            slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", model_id).strip("-").lower() or "model"
            candidate = f"{connection_id}-{slug}"
            suffix = 2
            while any(item.get("id") == candidate for item in items):
                candidate = f"{connection_id}-{slug}-{suffix}"; suffix += 1
            items.append(Preset(id=candidate,name=model_id,connection_id=connection_id,model=model_id,
                                structured_output=supports_schema).model_dump(exclude_none=True))
        self._write(data)
        return [Preset.model_validate(item) for item in items if item.get("connection_id") == connection_id]

    def api_key(self, connection: ConnectionIn) -> str:
        key = connection.api_key or (os.getenv(connection.api_key_env) if connection.api_key_env else None)
        if not key and connection.protocol not in {"mock", "human"}:
            raise ValueError(f"No API key configured for {connection.name}")
        return key or connection.protocol


def _mask(value: str | None) -> str | None:
    if not value: return None
    return "••••" + value[-4:] if len(value) > 4 else "••••"


def _masked_headers(headers: dict[str, str]) -> dict[str, str]:
    sensitive = ("authorization", "api-key", "apikey", "token", "secret")
    return {key: (_mask(value) or "••••") if any(part in key.lower() for part in sensitive) else value
            for key, value in headers.items()}
