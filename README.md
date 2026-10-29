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
- `GET /notifications` returns the caller's merged 30-day history of scheduled reminders and notify attempts, bounded to 100 rows. It uses batch's user-scoped reminder ledger plus notify provider/test records; if batch history is unavailable, it returns `503 history_incomplete` rather than an empty partial history. `POST /notifications/test` requires an `Idempotency-Key` and is rate-limited to one request per channel per minute.
- `GET /capabilities` reports configured provider availability. `GET /healthz`, `/readyz`, and `/metrics` are service endpoints.

Runtime configuration: `MYSQL_HOST`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE` (default `notify`), `MYSQL_TLS_MODE` (default `required`, `disabled` only with `APP_ENV=test`), `NOTIFY_ENCRYPTION_KEY` (URL-safe base64 encoding of exactly 32 bytes), `NATS_URL`, `INTERNAL_SERVICE_TOKEN`, `AUTH_JWKS_URL`, `AUTH_INTROSPECT_URL`, `AUTH_ACCOUNT_STATUS_URL`, `JWT_ISSUER`, `CORE_URL`, `BATCH_INTERNAL_BASE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `TELEGRAM_WEBHOOK_SECRET`, `PUBLIC_APP_URL`, `CORS_ALLOWED_ORIGINS`, `HTTP_HOST`, and `HTTP_PORT`. Telegram channel connect and explicit test delivery remain unavailable unless the bot token, username, webhook secret, initialized MySQL, and encryption key are configured; scheduled reminder consumption additionally requires NATS and `INTERNAL_SERVICE_TOKEN`. Before delivery, notify checks the current batch dispatch generation, core's authoritative schedule validity, and batch generation again; metadata-only edits can update the title without creating another dispatch. The internal channel capability endpoint is `/internal/users/{user_id}/channels` and requires the service bearer token; only verified, consented channels are returned, without destinations. The Telegram webhook accepts only private chats where sender and chat IDs match.

The history endpoint uses the same internal token to fetch `/internal/users/{user_id}/reminders` from batch with RFC3339 `from`/`to` and `limit` parameters. Batch and notify rows merge on `dispatch_id`; provider acceptance/test outcomes come from notify, while batch reports pending schedules and final retry exhaustion. No titles, destinations, or raw provider errors are exposed in history.

Tracing is off by default. To enable OTLP gRPC tracing, set `OTEL_TRACES_EXPORTER=otlp` and configure `OTEL_EXPORTER_OTLP_ENDPOINT` (default `http://localhost:4317`).

Run unit and mock-provider tests (no real recipient is contacted):

```bash
python -m unittest discover -s tests
```

The GitHub Actions workflow starts MySQL and a local JetStream server and enables the real database transaction/concurrency and NATS publish-failure regression tests. Set `NOTIFY_MYSQL_TEST=1` and `NOTIFY_NATS_TEST=1` only when those local services are available.
