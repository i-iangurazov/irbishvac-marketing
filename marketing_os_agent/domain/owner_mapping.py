from __future__ import annotations

import logging

from ..config import Settings
from ..models import Owner
from ..persistence import Persistence


logger = logging.getLogger(__name__)


class OwnerResolver:
    def __init__(self, settings: Settings, db: Persistence) -> None:
        self.settings = settings
        self.db = db

    def seed_from_config(self) -> None:
        for key, slack_user_id in self.settings.owner_slack_map.items():
            self.db.upsert_owner_mapping(key, key, slack_user_id)

    def resolve_slack_user(self, owner: Owner | None) -> str | None:
        if not owner:
            return None
        for key in owner.mapping_keys:
            configured = self.settings.owner_slack_map.get(key)
            if configured:
                return configured
        return self.db.get_slack_user_for_owner(owner.mapping_keys)

    def resolve_owner_email(self, owner: Owner | None) -> str | None:
        if not owner:
            return None
        for key in owner.mapping_keys:
            configured = self.settings.owner_email_map.get(key)
            if configured:
                return configured
        return owner.email or None

    def mentions_for_text(self, text: str) -> list[str]:
        lower = text.lower()
        mentions: list[str] = []
        for key, slack_user_id in self.settings.owner_slack_map.items():
            if not key or key.startswith("user_") or "@" in key:
                continue
            if key.lower() in lower and slack_user_id not in mentions:
                mentions.append(slack_user_id)
        return mentions

