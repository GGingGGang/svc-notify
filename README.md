# svc-notify

Notification service for verified Telegram channels and reminder dispatch. With no MySQL, encryption key, NATS, or Telegram credentials configured, the process still serves `/healthz`, reports `/readyz` as unavailable, and reports Telegram delivery as disabled through `/capabilities`. It does not send messages in that state.

Run with Python 3.13+:

```bash
python -m pip install -e .
python -m app.main
```

The user API is mounted at `/v1/notify` by the gateway (the service also accepts the unprefixed paths locally):

- `GET /channels`, `POST /channels/telegram/connect`, `PATCH /channels/{id}/consent`, `DELETE /channels/{id}` manage a Telegram channel. Ownership is proved by completing the returned `t.me` `/start` link through the configured Telegram webhook; a chat ID submitted by the user is never accepted.
- `GET` and `PUT /preferences/notifications` manage quiet hours in the account's authoritative timezone.
- `GET /notifications` returns the caller's 30-day history. `POST /notifications/test` requires an `Idempotency-Key` and is rate-limited to one request per channel per minute.
- `GET /capabilities` reports configured provider availability. `GET /healthz`, `/readyz`, and `/metrics` are service endpoints.

Runtime configuration: `MYSQL_HOST`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE` (default `notify`), `MYSQL_TLS_MODE` (default `required`, `disabled` only with `APP_ENV=test`), `NOTIFY_ENCRYPTION_KEY` (URL-safe base64 encoding of exactly 32 bytes), `NATS_URL`, `INTERNAL_SERVICE_TOKEN`, `AUTH_JWKS_URL`, `AUTH_INTROSPECT_URL`, `AUTH_ACCOUNT_STATUS_URL`, `JWT_ISSUER`, `CORE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `TELEGRAM_WEBHOOK_SECRET`, `PUBLIC_APP_URL`, `CORS_ALLOWED_ORIGINS`, `HTTP_HOST`, and `HTTP_PORT`. Telegram connect/send remains unavailable unless the bot token, username, webhook secret, initialized MySQL, and encryption key are configured. The internal channel capability endpoint is `/internal/users/{user_id}/channels` and requires the service bearer token; only verified, consented channels are returned, without destinations.

Tracing is off by default. To enable OTLP gRPC tracing, set `OTEL_TRACES_EXPORTER=otlp` and configure `OTEL_EXPORTER_OTLP_ENDPOINT` (default `http://localhost:4317`).

Run unit tests (all provider and user flows use fakes; no real recipient is contacted):

```bash
python -m unittest discover -s tests
```
