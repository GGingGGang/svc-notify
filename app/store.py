from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import ssl
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pymysql
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import Settings


def _utc_naive(value: datetime | None = None) -> datetime:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)


class Store:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.schema_ready = False
        try:
            key = base64.urlsafe_b64decode(settings.encryption_key + "=" * (-len(settings.encryption_key) % 4))
        except Exception as exc:
            raise ValueError("NOTIFY_ENCRYPTION_KEY must be URL-safe base64") from exc
        if len(key) != 32:
            raise ValueError("NOTIFY_ENCRYPTION_KEY must encode exactly 32 bytes")
        self._aes = AESGCM(key)
        self._fingerprint_key = hashlib.sha256(key + b"notify-destination-fingerprint").digest()

    @property
    def available(self) -> bool:
        return bool(self.settings.mysql_host and self.settings.mysql_user and self.settings.mysql_password and self.settings.encryption_key)

    def connect(self):
        if not self.available:
            raise RuntimeError("notification storage is not configured")
        context = ssl.create_default_context() if self.settings.mysql_tls_mode == "required" else None
        connection = pymysql.connect(
            host=self.settings.mysql_host,
            user=self.settings.mysql_user,
            password=self.settings.mysql_password,
            database=self.settings.mysql_database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
            connect_timeout=3,
            read_timeout=5,
            write_timeout=5,
            ssl=context,
        )
        with connection.cursor() as cursor:
            cursor.execute("SET time_zone = '+00:00'")
        return connection

    def ensure_schema(self) -> None:
        with self.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("CREATE TABLE IF NOT EXISTS notify_schema_version (version INT NOT NULL PRIMARY KEY)")
                # The packaged migration is idempotent; replay it to apply additive
                # CREATE TABLE IF NOT EXISTS additions to already initialized stores.
                migration = Path(__file__).parent / "migrations" / "V1__notify.sql"
                for statement in migration.read_text(encoding="utf-8").split(";"):
                    if statement.strip():
                        cursor.execute(statement)
                cursor.execute("INSERT IGNORE INTO notify_schema_version(version) VALUES (1)")
            conn.commit()
        self.schema_ready = True

    def ping(self) -> bool:
        with self.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                return cursor.fetchone() is not None

    def _encrypt(self, value: str) -> bytes:
        nonce = os.urandom(12)
        return nonce + self._aes.encrypt(nonce, value.encode("utf-8"), b"telegram-chat-id")

    def _decrypt(self, value: bytes) -> str:
        return self._aes.decrypt(value[:12], value[12:], b"telegram-chat-id").decode("utf-8")

    def _fingerprint(self, value: str) -> bytes:
        return hmac.new(self._fingerprint_key, value.encode("utf-8"), hashlib.sha256).digest()

    @staticmethod
    def _opaque_id(value: str) -> bytes:
        return hashlib.sha256(value.encode("utf-8")).digest()

    def is_deleted_user(self, user_id: str) -> bool:
        fingerprint = self._opaque_id(user_id)
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT deleted_at FROM notify_user_state WHERE user_fingerprint=%s", (fingerprint,))
            row = cursor.fetchone()
            if row is not None and row["deleted_at"] is not None:
                return True
            cursor.execute("SELECT 1 FROM notify_deleted_users WHERE user_fingerprint=%s", (fingerprint,))
            return cursor.fetchone() is not None

    def _lock_user(self, cursor, user_id: str) -> bool:
        """Lock persistent opaque user state so delete and delayed writes serialize."""
        fingerprint = self._opaque_id(user_id)
        cursor.execute("INSERT IGNORE INTO notify_user_state(user_fingerprint) VALUES(%s)", (fingerprint,))
        cursor.execute("SELECT deleted_at FROM notify_user_state WHERE user_fingerprint=%s FOR UPDATE", (fingerprint,))
        row = cursor.fetchone()
        if row["deleted_at"] is not None:
            return False
        cursor.execute("SELECT 1 FROM notify_deleted_users WHERE user_fingerprint=%s", (fingerprint,))
        if cursor.fetchone() is not None:
            cursor.execute("UPDATE notify_user_state SET deleted_at=UTC_TIMESTAMP(3) WHERE user_fingerprint=%s", (fingerprint,))
            return False
        return True

    def connect_telegram(self, user_id: str, token_hash: bytes, expires_at: datetime) -> str:
        channel_id = str(uuid.uuid4())
        with self.connect() as conn:
            with conn.cursor() as cursor:
                if not self._lock_user(cursor, user_id):
                    conn.rollback()
                    raise ValueError("account_deleted")
                cursor.execute(
                    "INSERT INTO notify_channels(id,user_id,channel) VALUES(%s,%s,'telegram') "
                    "ON DUPLICATE KEY UPDATE id=VALUES(id), destination_ciphertext=NULL, "
                    "destination_fingerprint=NULL, verified_at=NULL, consent_at=NULL, "
                    "disconnected_at=NULL, updated_at=CURRENT_TIMESTAMP(3)",
                    (channel_id, user_id),
                )
                cursor.execute("SELECT id FROM notify_channels WHERE user_id=%s AND channel='telegram'", (user_id,))
                channel_id = cursor.fetchone()["id"]
                cursor.execute("DELETE FROM telegram_challenges WHERE channel_id=%s", (channel_id,))
                cursor.execute(
                    "INSERT INTO telegram_challenges(id,channel_id,user_id,token_hash,expires_at) VALUES(%s,%s,%s,%s,%s)",
                    (str(uuid.uuid4()), channel_id, user_id, token_hash, _utc_naive(expires_at)),
                )
            conn.commit()
        return channel_id

    def verify_telegram_challenge(self, token_hash: bytes, chat_id: str, update_id: int) -> bool:
        with self.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("INSERT IGNORE INTO telegram_updates(update_id) VALUES(%s)", (update_id,))
                if cursor.rowcount == 0:
                    conn.rollback()
                    return False
                cursor.execute(
                    "SELECT id,channel_id FROM telegram_challenges WHERE token_hash=%s AND used_at IS NULL "
                    "AND expires_at > UTC_TIMESTAMP(3) FOR UPDATE",
                    (token_hash,),
                )
                challenge = cursor.fetchone()
                if challenge is None:
                    conn.commit()
                    return False
                ciphertext = self._encrypt(chat_id)
                fingerprint = self._fingerprint(chat_id)
                cursor.execute(
                    "UPDATE notify_channels SET destination_ciphertext=%s,destination_fingerprint=%s,"
                    "verified_at=UTC_TIMESTAMP(3),consent_at=NULL,updated_at=UTC_TIMESTAMP(3) WHERE id=%s",
                    (ciphertext, fingerprint, challenge["channel_id"]),
                )
                cursor.execute("UPDATE telegram_challenges SET used_at=UTC_TIMESTAMP(3) WHERE id=%s", (challenge["id"],))
            conn.commit()
        return True

    def list_channels(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT id,channel,destination_ciphertext,verified_at,consent_at,created_at "
                "FROM notify_channels WHERE user_id=%s AND disconnected_at IS NULL ORDER BY created_at",
                (user_id,),
            )
            rows = cursor.fetchall()
        return [{
            "id": row["id"], "channel": row["channel"],
            "verified": row["verified_at"] is not None,
            "consent": row["consent_at"] is not None,
            "connected_at": _iso(row["verified_at"] or row["created_at"]),
            "masked_destination": _mask_chat(self._decrypt(row["destination_ciphertext"])) if row["destination_ciphertext"] else None,
        } for row in rows]

    def set_consent(self, user_id: str, channel_id: str, enabled: bool) -> bool:
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE notify_channels SET consent_at=IF(%s,UTC_TIMESTAMP(3),NULL),updated_at=UTC_TIMESTAMP(3) "
                "WHERE id=%s AND user_id=%s AND disconnected_at IS NULL AND verified_at IS NOT NULL",
                (enabled, channel_id, user_id),
            )
            changed = cursor.rowcount == 1
            if changed and not enabled:
                cursor.execute(
                    "UPDATE notification_history SET status='cancelled',reason_code='consent_revoked',updated_at=UTC_TIMESTAMP(3) "
                    "WHERE channel_id=%s AND status IN ('pending','processing')",
                    (channel_id,),
                )
            conn.commit()
        return changed

    def disconnect(self, user_id: str, channel_id: str) -> bool:
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE notify_channels SET disconnected_at=UTC_TIMESTAMP(3),consent_at=NULL,updated_at=UTC_TIMESTAMP(3) "
                "WHERE id=%s AND user_id=%s AND disconnected_at IS NULL",
                (channel_id, user_id),
            )
            changed = cursor.rowcount == 1
            if changed:
                cursor.execute(
                    "UPDATE notification_history SET status='cancelled',reason_code='channel_disconnected',updated_at=UTC_TIMESTAMP(3) "
                    "WHERE channel_id=%s AND status IN ('pending','processing')",
                    (channel_id,),
                )
            conn.commit()
        return changed

    def get_preferences(self, user_id: str) -> dict[str, Any]:
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT quiet_start,quiet_end FROM notify_preferences WHERE user_id=%s", (user_id,))
            row = cursor.fetchone()
        return {"quiet_hours": None if row is None or row["quiet_start"] is None else {
            "start": str(row["quiet_start"])[:5], "end": str(row["quiet_end"])[:5]
        }}

    def set_preferences(self, user_id: str, quiet_start: str | None, quiet_end: str | None) -> None:
        with self.connect() as conn, conn.cursor() as cursor:
            if not self._lock_user(cursor, user_id):
                conn.rollback()
                raise ValueError("account_deleted")
            cursor.execute(
                "INSERT INTO notify_preferences(user_id,quiet_start,quiet_end) VALUES(%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE quiet_start=VALUES(quiet_start),quiet_end=VALUES(quiet_end),updated_at=UTC_TIMESTAMP(3)",
                (user_id, quiet_start, quiet_end),
            )
            conn.commit()

    def reserve_test(self, user_id: str, channel_id: str, dispatch_id: str, scheduled_at: datetime) -> tuple[str, str, str | None]:
        with self.connect() as conn, conn.cursor() as cursor:
            if not self._lock_user(cursor, user_id):
                conn.rollback()
                raise ValueError("account_deleted")
            cursor.execute(
                "SELECT id FROM notify_channels WHERE id=%s AND user_id=%s AND channel='telegram' "
                "AND verified_at IS NOT NULL AND consent_at IS NOT NULL AND disconnected_at IS NULL FOR UPDATE",
                (channel_id, user_id),
            )
            if cursor.fetchone() is None:
                conn.rollback()
                raise ValueError("channel_not_connected")
            cursor.execute("SELECT id,status FROM notification_history WHERE dispatch_id=%s FOR UPDATE", (dispatch_id,))
            existing = cursor.fetchone()
            if existing is not None:
                if existing["status"] == "processing":
                    cursor.execute(
                        "SELECT processing_started_at < UTC_TIMESTAMP(3)-INTERVAL 30 SECOND AS stale "
                        "FROM notification_history WHERE dispatch_id=%s",
                        (dispatch_id,),
                    )
                    stale = cursor.fetchone()["stale"]
                    if stale:
                        cursor.execute(
                            "UPDATE notification_history SET status='unknown',reason_code='worker_interrupted',"
                            "processing_started_at=NULL,updated_at=UTC_TIMESTAMP(3) WHERE dispatch_id=%s",
                            (dispatch_id,),
                        )
                        conn.commit()
                        return "unknown", existing["id"], None
                conn.commit()
                return ("processing" if existing["status"] == "processing" else existing["status"], existing["id"], None)
            cursor.execute("SELECT destination_ciphertext FROM notify_channels WHERE id=%s", (channel_id,))
            channel = cursor.fetchone()
            cursor.execute(
                "SELECT id FROM notification_history WHERE user_id=%s AND channel_id=%s AND is_test=1 "
                "AND created_at > UTC_TIMESTAMP(3) - INTERVAL 60 SECOND LIMIT 1",
                (user_id, channel_id),
            )
            if cursor.fetchone() is not None:
                conn.rollback()
                raise ValueError("test_rate_limited")
            cursor.execute(
                "INSERT INTO notification_history(id,dispatch_id,user_id,channel_id,channel,status,scheduled_at,is_test,attempt_count,processing_attempt,processing_started_at) "
                "VALUES(%s,%s,%s,%s,'telegram','processing',%s,1,1,1,UTC_TIMESTAMP(3))",
                (str(uuid.uuid4()), dispatch_id, user_id, channel_id, _utc_naive(scheduled_at)),
            )
            cursor.execute("SELECT id FROM notification_history WHERE dispatch_id=%s", (dispatch_id,))
            history_id = cursor.fetchone()["id"]
            conn.commit()
        return "claimed", history_id, self._decrypt(channel["destination_ciphertext"])

    def finish_test(self, dispatch_id: str, status: str, reason_code: str | None, receipt_id: str | None) -> None:
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                "UPDATE notification_history SET status=%s,reason_code=%s,provider_receipt_id=%s,updated_at=UTC_TIMESTAMP(3) "
                "WHERE dispatch_id=%s AND is_test=1",
                (status, reason_code, receipt_id, dispatch_id),
            )
            conn.commit()

    def history(self, user_id: str, from_at: datetime, to_at: datetime) -> list[dict[str, Any]]:
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT id,channel,status,reason_code,attempt_count,scheduled_at,created_at,is_test "
                "FROM notification_history h WHERE h.user_id=%s AND h.created_at >= %s AND h.created_at < %s "
                "AND h.created_at >= UTC_TIMESTAMP(3)-INTERVAL 30 DAY ORDER BY h.created_at DESC LIMIT 500",
                (user_id, _utc_naive(from_at), _utc_naive(to_at)),
            )
            rows = cursor.fetchall()
        return [{"id": row["id"], "channel": row["channel"], "status": row["status"],
                 "reason_code": row["reason_code"], "attempt_count": row["attempt_count"],
                 "scheduled_at": _iso(row["scheduled_at"]), "created_at": _iso(row["created_at"]),
                 "is_test": bool(row["is_test"])} for row in rows]

    def active_channel(self, user_id: str) -> dict[str, Any] | None:
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT id,destination_ciphertext FROM notify_channels WHERE user_id=%s AND channel='telegram' "
                "AND disconnected_at IS NULL AND verified_at IS NOT NULL AND consent_at IS NOT NULL LIMIT 1",
                (user_id,),
            )
            row = cursor.fetchone()
        if row is None or row["destination_ciphertext"] is None:
            return None
        return {"id": row["id"], "chat_id": self._decrypt(row["destination_ciphertext"])}

    def preferences_quiet(self, user_id: str) -> tuple[str, str] | None:
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT quiet_start,quiet_end FROM notify_preferences WHERE user_id=%s", (user_id,))
            row = cursor.fetchone()
        if row is None or row["quiet_start"] is None or row["quiet_end"] is None:
            return None
        return str(row["quiet_start"]), str(row["quiet_end"])

    def claim_dispatch(self, event: dict[str, Any], channel_id: str, scheduled_at: datetime) -> tuple[str, dict[str, Any] | None]:
        dispatch_id = event["dispatch_id"]
        attempt = int(event["attempt"])
        user_id = event["user_id"]
        with self.connect() as conn, conn.cursor() as cursor:
            if not self._lock_user(cursor, user_id):
                conn.rollback()
                return "deleted", None
            cursor.execute("SELECT * FROM notification_history WHERE dispatch_id=%s FOR UPDATE", (dispatch_id,))
            row = cursor.fetchone()
            if row is not None and row["processing_attempt"] == attempt and row["status"] == "processing":
                cursor.execute("SELECT processing_started_at < UTC_TIMESTAMP(3)-INTERVAL 30 SECOND AS stale FROM notification_history WHERE dispatch_id=%s", (dispatch_id,))
                if not cursor.fetchone()["stale"]:
                    conn.commit()
                    return "in_flight", None
                result = {"dispatch_id": dispatch_id, "attempt": attempt, "status": "unknown", "reason_code": "worker_interrupted"}
                cursor.execute("SELECT 1 FROM notification_attempts WHERE dispatch_id=%s AND attempt=%s", (dispatch_id, attempt))
                result["_new_completion"] = cursor.fetchone() is None
                self._save_result(cursor, dispatch_id, attempt, "unknown", "worker_interrupted", None, user_id, channel_id, event, scheduled_at)
                conn.commit()
                return "replay", result
            cursor.execute("SELECT * FROM notification_attempts WHERE dispatch_id=%s AND attempt=%s", (dispatch_id, attempt))
            prior_attempt = cursor.fetchone()
            if prior_attempt is not None:
                conn.commit()
                return "replay", _result_from_row(dispatch_id, prior_attempt)
            if row is not None and row["status"] in {"sent", "failed", "skipped", "cancelled", "unknown"}:
                result = {"dispatch_id": dispatch_id, "attempt": attempt, "status": "cancelled", "reason_code": "already_terminal",
                          "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
                conn.commit()
                return "replay", result
            if row is not None and attempt != row["attempt_count"] + 1:
                conn.rollback()
                raise ValueError("attempt_out_of_sequence")
            if row is None:
                notification_id = str(uuid.uuid4())
                cursor.execute(
                    "INSERT INTO notification_history(id,dispatch_id,user_id,channel_id,channel,schedule_id,status,attempt_count,scheduled_at,processing_attempt,processing_started_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,'processing',%s,%s,%s,UTC_TIMESTAMP(3))",
                    (notification_id, dispatch_id, user_id, channel_id, event.get("channel", "push"), event.get("schedule_id"), attempt, _utc_naive(scheduled_at), attempt),
                )
            else:
                cursor.execute(
                    "UPDATE notification_history SET status='processing',attempt_count=%s,processing_attempt=%s,processing_started_at=UTC_TIMESTAMP(3),updated_at=UTC_TIMESTAMP(3) WHERE dispatch_id=%s",
                    (attempt, attempt, dispatch_id),
                )
            conn.commit()
        return "claimed", None

    def record_result(self, event: dict[str, Any], channel_id: str, scheduled_at: datetime, status: str,
                      reason_code: str | None, receipt_id: str | None) -> dict[str, Any]:
        dispatch_id = event["dispatch_id"]
        attempt = int(event["attempt"])
        result = {"dispatch_id": dispatch_id, "attempt": attempt, "status": status,
                  "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
        if reason_code:
            result["reason_code"] = reason_code
        if receipt_id:
            result["provider_receipt_id"] = receipt_id
        with self.connect() as conn, conn.cursor() as cursor:
            if not self._lock_user(cursor, event["user_id"]):
                conn.rollback()
                return {"dispatch_id": dispatch_id, "attempt": attempt, "status": "cancelled",
                        "reason_code": "account_deleted", "occurred_at": result["occurred_at"], "_new_completion": False}
            cursor.execute("SELECT 1 FROM notification_attempts WHERE dispatch_id=%s AND attempt=%s", (dispatch_id, attempt))
            is_new = cursor.fetchone() is None
            self._save_result(cursor, dispatch_id, attempt, status, reason_code, receipt_id,
                              event["user_id"], channel_id, event, scheduled_at)
            conn.commit()
        result["_new_completion"] = is_new
        return result

    def _save_result(self, cursor, dispatch_id: str, attempt: int, status: str, reason_code: str | None,
                     receipt_id: str | None, user_id: str, channel_id: str, event: dict[str, Any], scheduled_at: datetime) -> None:
        public = {"accepted":"sent", "retryable_failure":"pending", "permanent_failure":"failed",
                  "unknown":"unknown", "cancelled":"cancelled", "skipped":"skipped"}.get(status, "failed")
        cursor.execute(
            "INSERT INTO notification_attempts(dispatch_id,attempt,status,reason_code,provider_receipt_id) "
            "VALUES(%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE status=VALUES(status),reason_code=VALUES(reason_code),provider_receipt_id=VALUES(provider_receipt_id)",
            (dispatch_id, attempt, status, reason_code, receipt_id),
        )
        cursor.execute(
            "INSERT INTO notification_history(id,dispatch_id,user_id,channel_id,channel,schedule_id,status,reason_code,provider_receipt_id,attempt_count,scheduled_at,processing_attempt) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE status=VALUES(status),reason_code=VALUES(reason_code),provider_receipt_id=VALUES(provider_receipt_id),attempt_count=GREATEST(attempt_count,VALUES(attempt_count)),processing_attempt=VALUES(processing_attempt),processing_started_at=NULL,updated_at=UTC_TIMESTAMP(3)",
            (str(uuid.uuid4()), dispatch_id, user_id, channel_id, event.get("channel", "push"), event.get("schedule_id"), public, reason_code,
             receipt_id, attempt, _utc_naive(scheduled_at), attempt),
        )

    def save_terminal_without_channel(self, event: dict[str, Any], status: str, reason_code: str) -> dict[str, Any]:
        dispatch_id, attempt = event["dispatch_id"], int(event["attempt"])
        result = {"dispatch_id": dispatch_id, "attempt": attempt, "status": status,
                  "reason_code": reason_code, "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
        with self.connect() as conn, conn.cursor() as cursor:
            if not self._lock_user(cursor, event["user_id"]):
                conn.rollback()
                result.update(status="cancelled", reason_code="account_deleted", _new_completion=False)
                return result
            cursor.execute("SELECT 1 FROM notification_attempts WHERE dispatch_id=%s AND attempt=%s", (dispatch_id, attempt))
            is_new = cursor.fetchone() is None
            self._save_result(cursor, dispatch_id, attempt, status, reason_code, None,
                              event["user_id"], None, event, _parse_time(event["remind_at"]))
            conn.commit()
        result["_new_completion"] = is_new
        return result

    def cleanup(self, now: datetime | None = None) -> int:
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute("DELETE FROM notification_attempts WHERE occurred_at < UTC_TIMESTAMP(3)-INTERVAL 30 DAY")
            cursor.execute("DELETE FROM notification_history WHERE created_at < UTC_TIMESTAMP(3)-INTERVAL 30 DAY")
            cursor.execute("DELETE FROM telegram_challenges WHERE expires_at < UTC_TIMESTAMP(3)-INTERVAL 1 DAY OR used_at IS NOT NULL")
            cursor.execute("DELETE FROM telegram_updates WHERE received_at < UTC_TIMESTAMP(3)-INTERVAL 2 DAY")
            deleted = cursor.rowcount
            conn.commit()
        return deleted

    def delete_user(self, user_id: str, event_id: str) -> bool:
        user_fingerprint = self._opaque_id(user_id)
        event_fingerprint = self._opaque_id(event_id)
        with self.connect() as conn, conn.cursor() as cursor:
            cursor.execute("INSERT IGNORE INTO notify_user_state(user_fingerprint) VALUES(%s)", (user_fingerprint,))
            cursor.execute("SELECT deleted_at FROM notify_user_state WHERE user_fingerprint=%s FOR UPDATE", (user_fingerprint,))
            cursor.execute("SELECT event_fingerprint FROM notify_account_events WHERE event_fingerprint=%s", (event_fingerprint,))
            if cursor.fetchone() is not None:
                conn.commit()
                return True
            cursor.execute("SELECT dispatch_id FROM notification_history WHERE user_id=%s", (user_id,))
            dispatch_ids = [row["dispatch_id"] for row in cursor.fetchall()]
            if dispatch_ids:
                placeholders = ",".join(["%s"] * len(dispatch_ids))
                cursor.execute(f"DELETE FROM notification_attempts WHERE dispatch_id IN ({placeholders})", dispatch_ids)
            cursor.execute("DELETE FROM notification_history WHERE user_id=%s", (user_id,))
            cursor.execute("DELETE FROM notify_preferences WHERE user_id=%s", (user_id,))
            cursor.execute("DELETE FROM telegram_challenges WHERE user_id=%s", (user_id,))
            cursor.execute("DELETE FROM notify_channels WHERE user_id=%s", (user_id,))
            cursor.execute("INSERT INTO notify_deleted_users(user_fingerprint) VALUES(%s) ON DUPLICATE KEY UPDATE user_fingerprint=VALUES(user_fingerprint)", (user_fingerprint,))
            cursor.execute("UPDATE notify_user_state SET deleted_at=COALESCE(deleted_at,UTC_TIMESTAMP(3)) WHERE user_fingerprint=%s", (user_fingerprint,))
            cursor.execute("INSERT INTO notify_account_events(event_fingerprint,user_fingerprint) VALUES(%s,%s)", (event_fingerprint, user_fingerprint))
            conn.commit()
        return True


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _mask_chat(chat_id: str) -> str:
    return "••••" + chat_id[-4:]


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _utc_naive(parsed)


def _result_from_row(dispatch_id: str, row: dict[str, Any]) -> dict[str, Any]:
    result = {"dispatch_id": dispatch_id, "attempt": row["attempt"], "status": row["status"],
              "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    if row.get("reason_code"):
        result["reason_code"] = row["reason_code"]
    if row.get("provider_receipt_id"):
        result["provider_receipt_id"] = row["provider_receipt_id"]
    return result
