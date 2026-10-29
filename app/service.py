from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.auth import AuthClient, AuthenticationError
from app.batch import BatchClient, BatchUnavailable
from app.config import Settings
from app.core import CoreClient, CoreUnavailable
from app.store import Store
from app.telegram import DeliveryResult, TelegramAdapter


class ServiceError(Exception):
    def __init__(self, status: int, code: str) -> None:
        self.status = status
        self.code = code
        super().__init__(code)


class NotifyService:
    def __init__(self, settings: Settings, store: Store | None = None, auth: AuthClient | None = None,
                 core: CoreClient | None = None, telegram: TelegramAdapter | None = None,
                 batch: BatchClient | None = None) -> None:
        self.settings = settings
        self.store = store
        self.auth = auth or AuthClient(settings)
        self.core = core or CoreClient(settings)
        self.batch = batch or BatchClient(settings)
        self.telegram = telegram or TelegramAdapter(
            settings.telegram_bot_token, settings.telegram_bot_username, settings.telegram_webhook_secret,
        )
        self.metrics = None

    @property
    def storage_available(self) -> bool:
        return bool(self.store and self.store.available and getattr(self.store, "schema_ready", False))

    @property
    def telegram_available(self) -> bool:
        return bool(self.telegram.configured and self.storage_available)

    def capabilities(self) -> dict[str, Any]:
        return {"delivery_enabled": self.telegram_available, "channels": {
            "telegram": {"configured": self.telegram.configured, "available": self.telegram_available}
        }}

    def user_id(self, authorization: str | None) -> str:
        try:
            return self.auth.user_id(authorization)
        except AuthenticationError as exc:
            raise ServiceError(503 if exc.code == "auth_unavailable" else 401, exc.code) from exc

    def channels(self, user_id: str) -> dict[str, Any]:
        store = self._store()
        return {"channels": store.list_channels(user_id), "available": self.telegram_available}

    def internal_channels(self, user_id: str) -> dict[str, Any]:
        if not user_id or len(user_id) > 128:
            raise ServiceError(400, "invalid_user_id")
        if not self.storage_available:
            raise ServiceError(503, "storage_unavailable")
        if self.store.is_deleted_user(user_id):
            return {"user_id": user_id, "channels": []}
        channel = self.store.active_channel(user_id)
        return {"user_id": user_id, "channels": ([{"channel": "telegram", "verified": True, "consent": True}]
                if channel and self.telegram_available else [])}

    def connect_telegram(self, user_id: str) -> dict[str, Any]:
        if not self.telegram_available:
            raise ServiceError(503, "channel_unavailable")
        token = secrets.token_urlsafe(24)
        expires = datetime.now(timezone.utc) + timedelta(minutes=15)
        channel_id = self._store().connect_telegram(user_id, hashlib.sha256(token.encode()).digest(), expires)
        return {"channel_id": channel_id, "verification_url": self.telegram.verification_url(token),
                "expires_at": expires.isoformat().replace("+00:00", "Z")}

    def consent(self, user_id: str, channel_id: str, enabled: bool) -> dict[str, Any]:
        if not isinstance(enabled, bool):
            raise ServiceError(400, "invalid_consent")
        changed = self._store().set_consent(user_id, channel_id, enabled)
        if not changed:
            raise ServiceError(404, "channel_not_found")
        return {"id": channel_id, "consent": enabled,
                "consented_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z") if enabled else None}

    def disconnect(self, user_id: str, channel_id: str) -> None:
        if not self._store().disconnect(user_id, channel_id):
            raise ServiceError(404, "channel_not_found")

    def preferences(self, user_id: str) -> dict[str, Any]:
        preferences = self._store().get_preferences(user_id)
        status = self._account(user_id)
        return {**preferences, "timezone": status["timezone"]}

    def set_preferences(self, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if set(body) != {"quiet_hours"}:
            raise ServiceError(400, "invalid_preferences")
        quiet = body["quiet_hours"]
        if quiet is None:
            start = end = None
        elif isinstance(quiet, dict) and set(quiet) == {"start", "end"} and all(
            isinstance(quiet[k], str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", quiet[k]) for k in ("start", "end")
        ) and quiet["start"] != quiet["end"]:
            start, end = quiet["start"], quiet["end"]
        else:
            raise ServiceError(400, "invalid_quiet_hours")
        status = self._account(user_id)
        self._store().set_preferences(user_id, start, end)
        return {"quiet_hours": quiet, "timezone": status["timezone"]}

    def history(self, user_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        try:
            from_at = _query_date(query, "from", now - timedelta(days=30))
            to_at = _query_date(query, "to", now)
        except ValueError as exc:
            raise ServiceError(400, "invalid_date_range") from exc
        if from_at >= to_at or to_at - from_at > timedelta(days=30) or to_at > now + timedelta(minutes=1):
            raise ServiceError(400, "invalid_date_range")
        return {"notifications": self._store().history(user_id, from_at, to_at)}

    def request_test(self, user_id: str, channel_id: str, idempotency_key: str) -> dict[str, Any]:
        if not self.telegram_available:
            raise ServiceError(503, "channel_unavailable")
        account = self._account(user_id)
        if not idempotency_key or len(idempotency_key) > 128:
            raise ServiceError(400, "idempotency_key_required")
        dispatch_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"notify-test:{user_id}:{idempotency_key}"))
        try:
            state, history_id, chat_id = self._store().reserve_test(
                user_id, channel_id, dispatch_id, datetime.now(timezone.utc),
            )
        except ValueError as exc:
            if str(exc) == "test_rate_limited":
                raise ServiceError(429, "test_rate_limited") from exc
            raise ServiceError(404, "channel_not_found") from exc
        if state != "claimed":
            return {"notification_id": history_id, "status": "unknown" if state == "processing" else state}
        quiet = self._store().preferences_quiet(user_id)
        if quiet:
            try:
                timezone_name = account.get("timezone")
                if not isinstance(timezone_name, str):
                    raise ZoneInfoNotFoundError("missing timezone")
                local_now = datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name)).strftime("%H:%M")
            except (ZoneInfoNotFoundError, ValueError):
                self._store().finish_test(dispatch_id, "failed", "timezone_unavailable", None)
                raise ServiceError(503, "timezone_unavailable")
            if _in_quiet_hours(local_now, quiet[0], quiet[1]):
                self._store().finish_test(dispatch_id, "skipped", "do_not_disturb", None)
                return {"notification_id": history_id, "status": "skipped", "reason_code": "do_not_disturb"}
        result = self.telegram.send_test(chat_id)
        status = _history_status(result.status)
        self._store().finish_test(dispatch_id, status, result.reason_code, result.receipt_id)
        return {"notification_id": history_id, "status": status}

    def telegram_update(self, secret_header: str | None, body: dict[str, Any]) -> bool:
        expected = self.settings.telegram_webhook_secret
        if not expected or not secret_header or not hmac.compare_digest(secret_header, expected):
            raise ServiceError(401, "unauthorized")
        message = body.get("message") or body.get("edited_message")
        if not isinstance(message, dict):
            return False
        text = message.get("text")
        chat = message.get("chat")
        sender = message.get("from")
        update_id = body.get("update_id")
        if not isinstance(text, str) or not isinstance(chat, dict) or not isinstance(sender, dict) or not isinstance(update_id, int):
            return False
        if chat.get("type") != "private" or sender.get("id") != chat.get("id"):
            return False
        match = re.fullmatch(r"/start(?:@[A-Za-z0-9_]+)?\s+([A-Za-z0-9_-]{20,80})", text.strip())
        if not match or not isinstance(chat.get("id"), (str, int)):
            return False
        token_hash = hashlib.sha256(match.group(1).encode()).digest()
        return self._store().verify_telegram_challenge(token_hash, str(chat["id"]), update_id)

    def deliver(self, event: dict[str, Any]) -> dict[str, Any]:
        try:
            _validate_due(event)
        except ValueError:
            return _event_result(event, "permanent_failure", "invalid_event")
        scheduled_at = _parse_date(event["remind_at"])
        if self.metrics is not None:
            self.metrics.record_dispatch_lag((datetime.now(timezone.utc) - scheduled_at).total_seconds())
        if self.storage_available and self.store.is_deleted_user(event["user_id"]):
            return _event_result(event, "cancelled", "account_deleted")
        if int(event["attempt"]) > 3:
            return self._persist_result(event, None, scheduled_at, "permanent_failure", "attempt_limit")
        if event["channel"] != "telegram":
            return self._persist_result(event, None, scheduled_at, "skipped", "channel_unsupported")
        if not self.telegram_available:
            return self._persist_result(event, None, scheduled_at, "skipped", "provider_unavailable")
        store = self._store()
        channel = store.active_channel(event["user_id"])
        channel_id = channel["id"] if channel else None
        claim, prior = store.claim_dispatch(event, channel_id, scheduled_at)
        if claim == "deleted":
            return _event_result(event, "cancelled", "account_deleted")
        if claim == "in_flight":
            return {"in_flight": True}
        if prior is not None:
            self._count_completion(prior)
            return prior
        if channel is None:
            return self._persist_result(event, None, scheduled_at, "skipped", "channel_not_connected")
        try:
            account = self._account(event["user_id"])
            if account.get("active") is not True:
                return self._persist_result(event, channel_id, scheduled_at, "cancelled", "account_inactive")
            batch_snapshot = self.batch.reminder_validity(event)
            if batch_snapshot.get("valid") is not True:
                return self._persist_result(event, channel_id, scheduled_at, "cancelled", "stale_dispatch")
            validity = self.core.reminder_validity(event, revision=batch_snapshot["revision"])
            if validity.get("valid") is not True:
                return self._persist_result(event, channel_id, scheduled_at, "cancelled", "stale_schedule")
        except (AuthenticationError, CoreUnavailable, BatchUnavailable) as exc:
            code = exc.code if isinstance(exc, AuthenticationError) else (
                "batch_unavailable" if isinstance(exc, BatchUnavailable) else "core_unavailable")
            if code == "account_inactive":
                return self._persist_result(event, channel_id, scheduled_at, "cancelled", code)
            return self._persist_result(event, channel_id, scheduled_at, "retryable_failure", code)
        now = datetime.now(timezone.utc)
        age = now - scheduled_at
        if age > timedelta(minutes=30):
            return self._persist_result(event, channel_id, scheduled_at, "skipped", "grace_expired")
        if age < timedelta(0):
            return self._persist_result(event, channel_id, scheduled_at, "retryable_failure", "not_due")
        quiet = store.preferences_quiet(event["user_id"])
        if quiet:
            try:
                timezone_name = account.get("timezone")
                if not isinstance(timezone_name, str):
                    raise ZoneInfoNotFoundError("missing timezone")
                local_now = now.astimezone(ZoneInfo(timezone_name)).strftime("%H:%M")
            except (ZoneInfoNotFoundError, ValueError):
                return self._persist_result(event, channel_id, scheduled_at, "retryable_failure", "timezone_unavailable")
            if _in_quiet_hours(local_now, quiet[0], quiet[1]):
                return self._persist_result(event, channel_id, scheduled_at, "skipped", "do_not_disturb")
        try:
            latest_batch = self.batch.reminder_validity(event)
        except BatchUnavailable:
            return self._persist_result(event, channel_id, scheduled_at, "retryable_failure", "batch_unavailable")
        if latest_batch.get("valid") is not True:
            return self._persist_result(event, channel_id, scheduled_at, "cancelled", "stale_dispatch")
        if not _same_schedule_generation(batch_snapshot, latest_batch):
            return self._persist_result(event, channel_id, scheduled_at, "retryable_failure", "dispatch_changed")
        validity = {**validity, "title": validity.get("title") or latest_batch["title"]}
        latest_channel = store.active_channel(event["user_id"])
        if latest_channel is None or latest_channel["id"] != channel_id:
            return self._persist_result(event, channel_id, scheduled_at, "skipped", "consent_revoked")
        result = self.telegram.send_reminder(latest_channel["chat_id"], event, validity, self.settings.public_app_url)
        return self._persist_result(event, channel_id, scheduled_at, result.status, result.reason_code, result.receipt_id)

    def _persist_result(self, event: dict, channel_id: str | None, scheduled_at: datetime, status: str,
                        reason_code: str, receipt_id: str | None = None) -> dict[str, Any]:
        result = self._store().record_result(event, channel_id, scheduled_at, status, reason_code, receipt_id)
        self._count_completion(result)
        return result

    def _count_completion(self, result: dict[str, Any]) -> None:
        is_new = result.pop("_new_completion", False)
        metrics = self.metrics
        if is_new and metrics is not None:
            metrics.record_attempt_completion(result.get("status", "unknown"))

    def _account(self, user_id: str) -> dict[str, Any]:
        try:
            account = self.auth.account(user_id)
        except AuthenticationError as exc:
            if exc.code == "unauthorized":
                raise ServiceError(401, "account_inactive") from exc
            raise
        return account

    def _store(self) -> Store:
        if not self.store or not self.store.available:
            raise ServiceError(503, "storage_unavailable")
        return self.store


def _validate_due(event: dict[str, Any]) -> None:
    required = ("dispatch_id", "attempt", "user_id", "schedule_id", "title", "start_at", "remind_at",
                "minutes_before", "channel", "schedule_revision")
    if not isinstance(event, dict) or any(key not in event for key in required):
        raise ValueError("missing fields")
    for key in ("dispatch_id", "user_id", "schedule_id", "title", "channel"):
        if not isinstance(event[key], str) or not event[key] or len(event[key]) > (255 if key == "title" else 128):
            raise ValueError("invalid field")
    for key in ("attempt", "minutes_before", "schedule_revision"):
        if not isinstance(event[key], int) or isinstance(event[key], bool) or event[key] < 0:
            raise ValueError("invalid field")
    if event["attempt"] < 1 or event["attempt"] > 3:
        raise ValueError("invalid attempt")
    _parse_date(event["start_at"])
    _parse_date(event["remind_at"])


def _parse_date(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid date")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result.astimezone(timezone.utc)


def _query_date(query: dict[str, list[str]], key: str, default: datetime) -> datetime:
    value = query.get(key, [None])[0]
    return _parse_date(value) if value is not None else default


def _same_schedule_generation(first: dict[str, Any], second: dict[str, Any]) -> bool:
    if any(first.get(key) != second.get(key) for key in ("user_id", "schedule_id", "minutes_before", "channel")):
        return False
    try:
        return (_parse_date(first["start_at"]) == _parse_date(second["start_at"])
                and _parse_date(first["remind_at"]) == _parse_date(second["remind_at"]))
    except (KeyError, ValueError, TypeError):
        return False


def _in_quiet_hours(now: str, start: str, end: str) -> bool:
    return (start <= now < end) if start < end else (now >= start or now < end)


def _history_status(status: str) -> str:
    return {"accepted": "sent", "retryable_failure": "failed", "permanent_failure": "failed",
            "unknown": "unknown", "cancelled": "cancelled", "skipped": "skipped"}.get(status, "failed")


def _event_result(event: dict[str, Any], status: str, reason: str) -> dict[str, Any]:
    return {"dispatch_id": event.get("dispatch_id"), "attempt": event.get("attempt", 1), "status": status,
            "reason_code": reason, "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
