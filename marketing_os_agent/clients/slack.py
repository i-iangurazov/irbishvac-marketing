from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any

from ..config import Settings
from .http import HttpClient


logger = logging.getLogger(__name__)


class SlackClient:
    base_url = "https://slack.com/api"

    def __init__(self, settings: Settings, http: HttpClient | None = None) -> None:
        self.settings = settings
        self.http = http or HttpClient()

    @property
    def available(self) -> bool:
        return bool(self.settings.slack_bot_token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.slack_bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    def _api(self, method: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not self.available:
            logger.warning("slack_credentials_missing", extra={"method": method})
            return None
        try:
            response = self.http.request_json("POST", f"{self.base_url}/{method}", headers=self._headers(), body=payload)
        except Exception:
            logger.exception("slack_api_failure", extra={"method": method})
            return None
        if not response.data.get("ok"):
            logger.warning("slack_api_error", extra={"method": method, "response": response.data})
            return None
        return response.data

    def post_message(self, channel: str, text: str, blocks: list[dict[str, Any]] | None = None, thread_ts: str | None = None) -> str | None:
        if not channel:
            logger.warning("slack_channel_missing", extra={"text": text[:120]})
            return None
        payload: dict[str, Any] = {"channel": channel, "text": text}
        if blocks:
            payload["blocks"] = blocks
        if thread_ts:
            payload["thread_ts"] = thread_ts
        response = self._api("chat.postMessage", payload)
        ts = response.get("ts") if response else None
        if ts:
            logger.info("slack_message_posted", extra={"channel": channel, "ts": ts})
        return ts

    def reply(self, channel: str, thread_ts: str, text: str) -> str | None:
        return self.post_message(channel, text, thread_ts=thread_ts)

    def dm_user(self, user_id: str, text: str) -> str | None:
        if not user_id:
            logger.warning("slack_dm_user_missing", extra={"text": text[:120]})
            return None
        opened = self._api("conversations.open", {"users": user_id})
        channel = ((opened or {}).get("channel") or {}).get("id")
        if not channel:
            logger.warning("slack_dm_open_failed", extra={"user_id": user_id})
            return None
        return self.post_message(channel, text)

    def verify_signature(self, timestamp: str, body: bytes, signature: str) -> bool:
        if not self.settings.slack_signing_secret:
            return False
        try:
            ts = int(timestamp)
        except ValueError:
            return False
        if abs(time.time() - ts) > 60 * 5:
            return False
        basestring = b"v0:" + timestamp.encode("utf-8") + b":" + body
        expected = "v0=" + hmac.new(
            self.settings.slack_signing_secret.encode("utf-8"),
            basestring,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)


def slack_mention(user_id: str | None) -> str:
    return f"<@{user_id}>" if user_id else ""
