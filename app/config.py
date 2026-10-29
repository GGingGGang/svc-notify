from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    service_name: str = "svc-notify"
    http_host: str = "0.0.0.0"
    http_port: int = 8080
    app_version: str = "dev"
    mysql_host: str = ""
    mysql_user: str = ""
    mysql_password: str = ""
    mysql_database: str = "notify"
    auth_jwks_url: str = "http://auth.auth.svc.cluster.local:3000/.well-known/jwks.json"
    auth_introspect_url: str = "http://auth.auth.svc.cluster.local:3000/introspect"
    auth_account_status_url: str = "http://auth.auth.svc.cluster.local:3000/internal/accounts"
    jwt_issuer: str = ""
    core_url: str = "http://core.core.svc.cluster.local:8080"
    internal_service_token: str = ""
    nats_url: str = ""
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    telegram_webhook_secret: str = ""
    encryption_key: str = ""
    public_app_url: str = ""
    cors_allowed_origins: str = ""
    app_env: str = "production"
    mysql_tls_mode: str = "required"


def load_settings(env: dict[str, str] | None = None) -> Settings:
    source = os.environ if env is None else env
    app_env = source.get("APP_ENV", "production").lower()
    mysql_tls_mode = source.get("MYSQL_TLS_MODE", "required").lower()
    if mysql_tls_mode not in {"required", "disabled"} or (mysql_tls_mode == "disabled" and app_env != "test"):
        raise ValueError("MYSQL_TLS_MODE=disabled is permitted only with APP_ENV=test")
    return Settings(
        service_name=source.get("SERVICE_NAME", "svc-notify"),
        http_host=source.get("HTTP_HOST", "0.0.0.0"),
        http_port=_int_from_env(source, "HTTP_PORT", 8080),
        app_version=source.get("APP_VERSION", "dev"),
        mysql_host=source.get("MYSQL_HOST", ""),
        mysql_user=source.get("MYSQL_USER", ""),
        mysql_password=source.get("MYSQL_PASSWORD", ""),
        mysql_database=source.get("MYSQL_DATABASE", "notify"),
        auth_jwks_url=source.get("AUTH_JWKS_URL", "http://auth.auth.svc.cluster.local:3000/.well-known/jwks.json"),
        auth_introspect_url=source.get("AUTH_INTROSPECT_URL", "http://auth.auth.svc.cluster.local:3000/introspect"),
        auth_account_status_url=source.get("AUTH_ACCOUNT_STATUS_URL", "http://auth.auth.svc.cluster.local:3000/internal/accounts"),
        jwt_issuer=source.get("JWT_ISSUER", ""),
        core_url=source.get("CORE_URL", "http://core.core.svc.cluster.local:8080"),
        internal_service_token=source.get("INTERNAL_SERVICE_TOKEN", ""),
        nats_url=source.get("NATS_URL", ""),
        telegram_bot_token=source.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_bot_username=source.get("TELEGRAM_BOT_USERNAME", ""),
        telegram_webhook_secret=source.get("TELEGRAM_WEBHOOK_SECRET", ""),
        encryption_key=source.get("NOTIFY_ENCRYPTION_KEY", ""),
        public_app_url=source.get("PUBLIC_APP_URL", ""),
        cors_allowed_origins=source.get("CORS_ALLOWED_ORIGINS", ""),
        app_env=app_env,
        mysql_tls_mode=mysql_tls_mode,
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
