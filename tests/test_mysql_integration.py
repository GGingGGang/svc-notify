"""Real MySQL transaction tests; enabled in CI with NOTIFY_MYSQL_TEST=1."""
import base64
import hashlib
import os
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.store import Store


@unittest.skipUnless(os.getenv("NOTIFY_MYSQL_TEST") == "1", "requires the MySQL CI service")
class MySQLStoreIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(Settings(
            mysql_host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            mysql_user=os.getenv("MYSQL_USER", "root"),
            mysql_password=os.getenv("MYSQL_PASSWORD", "test"),
            mysql_database=os.getenv("MYSQL_DATABASE", "notify"),
            mysql_tls_mode="disabled",
            app_env="test",
            encryption_key=base64.urlsafe_b64encode(b"i" * 32).decode(),
        ))
        cls.store.ensure_schema()

    def event(self, user_id, dispatch_id=None):
        now = datetime.now(timezone.utc) - timedelta(minutes=1)
        return {"dispatch_id": dispatch_id or str(uuid.uuid4()), "attempt": 1, "user_id": user_id,
                "schedule_id": str(uuid.uuid4()), "title": "integration", "start_at": now.isoformat(),
                "remind_at": now.isoformat(), "minutes_before": 10, "channel": "telegram",
                "schedule_revision": 1}

    def test_schema_initialization_is_repeatable(self):
        self.store.ensure_schema()
        self.assertTrue(self.store.ping())

    def test_legacy_tombstone_blocks_resurrection_and_migrates_to_user_lock(self):
        user_id = str(uuid.uuid4())
        fingerprint = self.store._opaque_id(user_id)
        with self.store.connect() as conn, conn.cursor() as cursor:
            cursor.execute("INSERT INTO notify_deleted_users(user_fingerprint) VALUES(%s)", (fingerprint,))
            conn.commit()
        self.assertTrue(self.store.is_deleted_user(user_id))
        with self.assertRaisesRegex(ValueError, "account_deleted"):
            self.store.connect_telegram(user_id, hashlib.sha256(uuid.uuid4().bytes).digest(),
                                        datetime.now(timezone.utc) + timedelta(minutes=15))
        with self.store.connect() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT deleted_at FROM notify_user_state WHERE user_fingerprint=%s", (fingerprint,))
            self.assertIsNotNone(cursor.fetchone()["deleted_at"])

    def test_channel_ownership_and_telegram_challenge_are_one_time(self):
        user_id = str(uuid.uuid4())
        other_user = str(uuid.uuid4())
        token_hash = hashlib.sha256(uuid.uuid4().bytes).digest()
        expiry = datetime.now(timezone.utc) + timedelta(minutes=15)
        channel_id = self.store.connect_telegram(user_id, token_hash, expiry)
        other_channel = self.store.connect_telegram(other_user, hashlib.sha256(uuid.uuid4().bytes).digest(), expiry)
        update_id = uuid.uuid4().int % 1_000_000_000_000
        self.assertTrue(self.store.verify_telegram_challenge(token_hash, str(uuid.uuid4().int), update_id))
        self.assertFalse(self.store.verify_telegram_challenge(token_hash, str(uuid.uuid4().int), update_id + 1))
        self.assertEqual([item["id"] for item in self.store.list_channels(user_id)], [channel_id])
        self.assertEqual([item["id"] for item in self.store.list_channels(other_user)], [other_channel])
        self.assertTrue(self.store.list_channels(user_id)[0]["verified"])
        self.assertFalse(self.store.list_channels(user_id)[0]["consent"])
        self.store.delete_user(user_id, str(uuid.uuid4()))
        self.store.delete_user(other_user, str(uuid.uuid4()))

    def test_test_notification_in_progress_expires_to_unknown(self):
        user_id = str(uuid.uuid4())
        channel_id = str(uuid.uuid4())
        dispatch_id = str(uuid.uuid4())
        scheduled_at = datetime.now(timezone.utc)
        with self.store.connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO notify_channels(id,user_id,channel,destination_ciphertext,verified_at,consent_at) "
                "VALUES(%s,%s,'telegram',%s,UTC_TIMESTAMP(3),UTC_TIMESTAMP(3))",
                (channel_id, user_id, b"test-encrypted"),
            )
            cursor.execute(
                "INSERT INTO notification_history(id,dispatch_id,user_id,channel_id,channel,status,scheduled_at,is_test,"
                "attempt_count,processing_attempt,processing_started_at) "
                "VALUES(%s,%s,%s,%s,'telegram','processing',%s,1,1,1,UTC_TIMESTAMP(3)-INTERVAL 31 SECOND)",
                (str(uuid.uuid4()), dispatch_id, user_id, channel_id, scheduled_at.replace(tzinfo=None)),
            )
            conn.commit()
        result = self.store.reserve_test(user_id, channel_id, dispatch_id, scheduled_at)
        self.assertEqual((result[0], result[2]), ("unknown", None))
        self.store.delete_user(user_id, str(uuid.uuid4()))

    def test_test_notification_is_rate_limited_per_user_channel(self):
        user_id = str(uuid.uuid4())
        token_hash = hashlib.sha256(uuid.uuid4().bytes).digest()
        channel_id = self.store.connect_telegram(user_id, token_hash, datetime.now(timezone.utc) + timedelta(minutes=15))
        self.assertTrue(self.store.verify_telegram_challenge(token_hash, str(uuid.uuid4().int), uuid.uuid4().int % 1_000_000_000_000))
        self.assertTrue(self.store.set_consent(user_id, channel_id, True))
        now = datetime.now(timezone.utc)
        first_dispatch = str(uuid.uuid4())
        state, _, _ = self.store.reserve_test(user_id, channel_id, first_dispatch, now)
        self.assertEqual(state, "claimed")
        self.store.finish_test(first_dispatch, "sent", None, "fake-receipt")
        with self.assertRaisesRegex(ValueError, "test_rate_limited"):
            self.store.reserve_test(user_id, channel_id, str(uuid.uuid4()), now)
        self.store.delete_user(user_id, str(uuid.uuid4()))

    def test_concurrent_duplicate_attempt_has_single_claimant(self):
        user_id = str(uuid.uuid4())
        event = self.event(user_id)
        barrier = threading.Barrier(2)
        results = []

        def claim():
            barrier.wait()
            results.append(self.store.claim_dispatch(event, "channel", datetime.now(timezone.utc)))

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertCountEqual([result[0] for result in results], ["claimed", "in_flight"])
        self.store.delete_user(user_id, str(uuid.uuid4()))

    def test_delete_racing_with_dispatch_prevents_late_user_data(self):
        user_id = str(uuid.uuid4())
        event = self.event(user_id)
        barrier = threading.Barrier(2)
        outcomes = []

        def claim():
            barrier.wait()
            outcomes.append(self.store.claim_dispatch(event, None, datetime.now(timezone.utc))[0])

        def delete():
            barrier.wait()
            self.store.delete_user(user_id, "delete-" + str(uuid.uuid4()))

        threads = [threading.Thread(target=claim), threading.Thread(target=delete)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(self.store.is_deleted_user(user_id))
        self.assertEqual(self.store.claim_dispatch(event, None, datetime.now(timezone.utc))[0], "deleted")
        with self.store.connect() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS total FROM notification_history WHERE user_id=%s", (user_id,))
            self.assertEqual(cursor.fetchone()["total"], 0)

    def test_duplicate_result_completion_is_durable_once(self):
        user_id = str(uuid.uuid4())
        event = self.event(user_id)
        schedule = datetime.now(timezone.utc)
        barrier = threading.Barrier(2)
        results = []

        def save():
            barrier.wait()
            results.append(self.store.record_result(event, None, schedule, "accepted", None, "receipt"))

        threads = [threading.Thread(target=save) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(sum(bool(result["_new_completion"]) for result in results), 1)
        self.store.delete_user(user_id, str(uuid.uuid4()))


if __name__ == "__main__":
    unittest.main()
