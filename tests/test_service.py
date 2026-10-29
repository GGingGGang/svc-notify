import unittest
import base64
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from app.config import Settings
from app.service import NotifyService, _in_quiet_hours
from app.telegram import DeliveryResult
from app.store import Store


class FakeStore:
    available = True
    schema_ready = True

    def __init__(self):
        self.deleted = False
        self.quiet = None
        self.saved = []
        self.history_range = None

    def is_deleted_user(self, _user):
        return self.deleted

    def active_channel(self, _user):
        return {"id": "channel", "chat_id": "12345"}

    def claim_dispatch(self, event, _channel_id, _scheduled_at):
        return "deleted" if self.deleted else ("claimed", None)

    def preferences_quiet(self, _user):
        return self.quiet

    def record_result(self, event, _channel_id, _scheduled, status, reason, receipt):
        result = {"dispatch_id": event["dispatch_id"], "attempt": event["attempt"], "status": status,
                  "reason_code": reason, "provider_receipt_id": receipt}
        self.saved.append(result)
        return result

    def history(self, _user, from_at, to_at):
        self.history_range = (from_at, to_at)
        return []

    def reserve_test(self, _user, _channel, _dispatch, _scheduled):
        return "claimed", "notification-id", "12345"

    def finish_test(self, dispatch, status, reason, receipt):
        self.test_result = (dispatch, status, reason, receipt)


class NotifyServiceTest(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.auth = Mock()
        self.auth.account.return_value = {"active": True, "timezone": "UTC"}
        self.core = Mock()
        self.core.reminder_validity.return_value = {"valid": True}
        self.batch = Mock()
        self.telegram = Mock()
        self.telegram.configured = True
        self.telegram.send_reminder.return_value = DeliveryResult("accepted", None, "receipt")
        self.service = NotifyService(Settings(telegram_bot_token="token"), self.store, self.auth,
                                     self.core, self.telegram)
        when = datetime.now(timezone.utc) - timedelta(minutes=1)
        self.event = {"dispatch_id": "dispatch", "attempt": 1, "user_id": "user", "schedule_id": "schedule",
                      "title": "Example", "start_at": when.isoformat(), "remind_at": when.isoformat(),
                      "minutes_before": 10, "channel": "telegram", "schedule_revision": 1}
        snapshot = {"valid": True, "dispatch_id": "dispatch", "attempt": 1, "user_id": "user",
                    "schedule_id": "schedule", "revision": 4, "start_at": self.event["start_at"],
                    "remind_at": self.event["remind_at"], "minutes_before": 10, "channel": "telegram",
                    "title": "Example"}
        self.snapshot = snapshot
        self.batch.reminder_validity.side_effect = [snapshot, snapshot]
        self.service = NotifyService(Settings(telegram_bot_token="token"), self.store, self.auth,
                                     self.core, self.telegram, self.batch)

    def test_dispatch_preflights_and_records_provider_acceptance(self):
        result = self.service.deliver(self.event)
        self.assertEqual(result["status"], "accepted")
        self.core.reminder_validity.assert_called_once_with(self.event, revision=4)
        self.assertEqual(self.batch.reminder_validity.call_count, 2)
        self.telegram.send_reminder.assert_called_once()
        self.assertEqual(self.store.saved[0]["status"], "accepted")

    def test_unverified_or_unconsented_channel_is_skipped(self):
        self.store.active_channel = lambda _user: None
        result = self.service.deliver(self.event)
        self.assertEqual((result["status"], result["reason_code"]), ("skipped", "channel_not_connected"))
        self.telegram.send_reminder.assert_not_called()

    def test_deleted_user_due_event_does_not_recreate_history(self):
        self.store.deleted = True
        result = self.service.deliver(self.event)
        self.assertEqual((result["status"], result["reason_code"]), ("cancelled", "account_deleted"))
        self.assertEqual(self.store.saved, [])

    def test_unsupported_channels_are_not_mapped_to_telegram(self):
        self.event["channel"] = "push"
        result = self.service.deliver(self.event)
        self.assertEqual((result["status"], result["reason_code"]), ("skipped", "channel_unsupported"))
        self.telegram.send_reminder.assert_not_called()

    def test_dnd_handles_midnight_wrap(self):
        self.assertTrue(_in_quiet_hours("23:30", "22:00", "07:00"))
        self.assertTrue(_in_quiet_hours("06:59", "22:00", "07:00"))
        self.assertFalse(_in_quiet_hours("07:00", "22:00", "07:00"))

    def test_history_default_range_is_valid_30_days(self):
        response = self.service.history("user", {})
        self.assertEqual(response, {"notifications": []})
        start, end = self.store.history_range
        self.assertEqual(end - start, timedelta(days=30))

    def test_metadata_only_title_change_uses_latest_batch_title(self):
        first = dict(self.snapshot)
        latest = dict(first, revision=5, title="New title")
        self.batch.reminder_validity.side_effect = [first, latest]
        self.service.deliver(self.event)
        sent_validity = self.telegram.send_reminder.call_args.args[2]
        self.assertEqual(sent_validity["title"], "New title")

    def test_changed_reminder_generation_is_not_sent(self):
        first = dict(self.snapshot)
        changed = dict(first, remind_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        self.batch.reminder_validity.side_effect = [first, changed]
        result = self.service.deliver(self.event)
        self.assertEqual((result["status"], result["reason_code"]), ("retryable_failure", "dispatch_changed"))
        self.telegram.send_reminder.assert_not_called()

    def test_consent_revoked_before_provider_call_is_not_sent(self):
        original = self.store.active_channel
        self.store.active_channel = Mock(side_effect=[original("user"), None])
        result = self.service.deliver(self.event)
        self.assertEqual((result["status"], result["reason_code"]), ("skipped", "consent_revoked"))
        self.telegram.send_reminder.assert_not_called()

    def test_explicit_test_notification_respects_dnd(self):
        self.store.preferences_quiet = lambda _user: ("00:00", "23:59")
        result = self.service.request_test("user", "channel-id", "idempotency-key")
        self.assertEqual((result["status"], result["reason_code"]), ("skipped", "do_not_disturb"))
        self.assertEqual(self.store.test_result[1:3], ("skipped", "do_not_disturb"))
        self.telegram.send_test.assert_not_called()

    def test_reminder_during_dnd_is_durable_skip(self):
        self.store.preferences_quiet = lambda _user: ("00:00", "23:59")
        result = self.service.deliver(self.event)
        self.assertEqual((result["status"], result["reason_code"]), ("skipped", "do_not_disturb"))
        self.telegram.send_reminder.assert_not_called()

    def test_telegram_start_webhook_rejects_group_and_non_owner(self):
        settings = Settings(telegram_webhook_secret="secret")
        service = NotifyService(settings, self.store, self.auth, self.core, self.telegram, self.batch)
        payload = {"update_id": 1, "message": {"text": "/start " + "x" * 24,
                   "chat": {"id": 123, "type": "group"}, "from": {"id": 123}}}
        self.assertFalse(service.telegram_update("secret", payload))
        self.store.verify_telegram_challenge = Mock(return_value=True)
        payload["message"]["chat"]["type"] = "private"
        payload["message"]["from"]["id"] = 456
        self.assertFalse(service.telegram_update("secret", payload))
        self.store.verify_telegram_challenge.assert_not_called()

    def test_destination_encryption_and_stable_deletion_tombstone(self):
        settings = Settings(encryption_key=base64.urlsafe_b64encode(b"k" * 32).decode())
        store = Store(settings)
        encrypted = store._encrypt("private-chat-id")
        self.assertNotIn(b"private-chat-id", encrypted)
        self.assertEqual(store._decrypt(encrypted), "private-chat-id")
        self.assertEqual(store._opaque_id("user-uuid"), Store._opaque_id("user-uuid"))


if __name__ == "__main__":
    unittest.main()
