from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    reason_code: str | None = None
    receipt_id: str | None = None


class TelegramAdapter:
    def __init__(self, bot_token: str, bot_username: str, webhook_secret: str) -> None:
        self.bot_token = bot_token
        self.bot_username = bot_username.lstrip("@")
        self.webhook_secret = webhook_secret

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.bot_username and self.webhook_secret)

    def verification_url(self, token: str) -> str:
        if not self.configured:
            raise RuntimeError("telegram_not_configured")
        return f"https://t.me/{self.bot_username}?start={token}"

    def send_test(self, chat_id: str) -> DeliveryResult:
        return self._send(chat_id, "Notification delivery test.")

    def send_reminder(self, chat_id: str, event: dict, validity: dict, app_url: str) -> DeliveryResult:
        title = str(validity.get("title") or event.get("title") or "Reminder")[:255]
        timezone_name = str(validity.get("timezone") or event.get("timezone") or "UTC")
        instant = validity.get("start_at") or event.get("start_at")
        try:
            local = datetime.fromisoformat(str(instant).replace("Z", "+00:00")).astimezone(ZoneInfo(timezone_name))
            shown_time = local.strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError, ZoneInfoNotFoundError):
            return DeliveryResult("permanent_failure", "invalid_schedule_time")
        schedule_id = str(event.get("schedule_id", ""))
        url = f"{app_url.rstrip('/')}/schedules/{schedule_id}" if app_url else f"/schedules/{schedule_id}"
        text = f"{title}\n{shown_time} ({timezone_name})\n{url}"
        return self._send(chat_id, text[:4096])

    def _send(self, chat_id: str, text: str) -> DeliveryResult:
        if not self.configured:
            return DeliveryResult("skipped", "provider_unavailable")
        body = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=5) as response:
                if response.status < 200 or response.status >= 300:
                    return _http_result(response.status)
                payload = json.loads(response.read(16 * 1024))
        except urllib.error.HTTPError as exc:
            return _http_result(exc.code)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            # A lost response may follow successful provider acceptance; do not retry blindly.
            return DeliveryResult("unknown", "provider_response_lost")
        if payload.get("ok") is True and isinstance(payload.get("result"), dict):
            receipt = payload["result"].get("message_id")
            if isinstance(receipt, (str, int)):
                return DeliveryResult("accepted", receipt_id=str(receipt))
            return DeliveryResult("unknown", "provider_receipt_missing")
        code = payload.get("error_code")
        if code == 429:
            return DeliveryResult("retryable_failure", "provider_rate_limited")
        if isinstance(code, int) and code >= 500:
            return DeliveryResult("unknown", "provider_response_ambiguous")
        return DeliveryResult("permanent_failure", "provider_rejected")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_result(code: int) -> DeliveryResult:
    if code == 429:
        return DeliveryResult("retryable_failure", "provider_rate_limited")
    if 400 <= code < 500:
        return DeliveryResult("permanent_failure", "provider_rejected")
    return DeliveryResult("unknown", "provider_response_ambiguous")
