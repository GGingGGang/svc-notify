from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    service_name: str = "svc-notify"
    http_host: str = "0.0.0.0"
    http_port: int = 8080
    app_version: str = "dev"


def load_settings(env: dict[str, str] | None = None) -> Settings:
    source = os.environ if env is None else env
    return Settings(
        service_name=source.get("SERVICE_NAME", "svc-notify"),
        http_host=source.get("HTTP_HOST", "0.0.0.0"),
        http_port=_int_from_env(source, "HTTP_PORT", 8080),
        app_version=source.get("APP_VERSION", "dev"),
    )


def _int_from_env(source: dict[str, str], key: str, default: int) -> int:
    raw = source.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if value <= 0 or value > 65535:
        raise ValueError(f"{key} must be between 1 and 65535")
    return value

