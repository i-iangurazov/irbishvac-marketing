from __future__ import annotations

import logging
from typing import Any

from ..config import Settings
from .http import HttpClient


logger = logging.getLogger(__name__)


class ClaudeClient:
    base_url = "https://api.anthropic.com/v1/messages"
    models_url = "https://api.anthropic.com/v1/models"

    def __init__(self, settings: Settings, http: HttpClient | None = None) -> None:
        self.settings = settings
        self.http = http or HttpClient(timeout_seconds=30, retries=1)

    @property
    def available(self) -> bool:
        return bool(self.settings.anthropic_api_key)

    def complete(self, system: str, user: str, *, max_tokens: int = 800) -> str | None:
        if not self.available:
            logger.warning("claude_credentials_missing")
            return None
        body = {
            "model": self.settings.claude_model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "x-api-key": self.settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
        }
        try:
            response = self.http.request_json("POST", self.base_url, headers=headers, body=body)
        except Exception:
            logger.exception("claude_api_failure")
            return None
        if response.status >= 400:
            logger.warning("claude_api_error", extra={"status": response.status, "response": response.data})
            return None
        usage = response.data.get("usage", {})
        logger.info(
            "claude_usage",
            extra={
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "model": self.settings.claude_model,
            },
        )
        parts = response.data.get("content", [])
        return "\n".join(part.get("text", "") for part in parts if part.get("type") == "text").strip()

    def list_models(self) -> list[str]:
        if not self.available:
            logger.warning("claude_credentials_missing")
            return []
        headers = {
            "x-api-key": self.settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
        }
        try:
            response = self.http.request_json("GET", self.models_url, headers=headers)
        except Exception:
            logger.exception("claude_models_api_failure")
            return []
        if response.status >= 400:
            logger.warning("claude_models_api_error", extra={"status": response.status, "response": response.data})
            return []
        models = response.data.get("data", [])
        return sorted(
            model["id"]
            for model in models
            if isinstance(model, dict) and isinstance(model.get("id"), str)
        )

    def draft_monday_owner_message(self, owner_name: str, task_lines: list[str]) -> str:
        fallback = f"Good morning {owner_name}. Monday marketing task update:\n" + "\n".join(task_lines)
        prompt = {
            "owner": owner_name,
            "tasks": task_lines,
            "instruction": "Draft a concise Slack DM. Preserve the sections and facts. Do not add tasks or dates.",
        }
        return self.complete("You draft concise operational marketing task messages.", str(prompt), max_tokens=500) or fallback

    def draft_friday_roundup(self, structured_sections: dict[str, list[str]]) -> str:
        fallback_lines = ["Friday Marketing Roundup"]
        for section, lines in structured_sections.items():
            fallback_lines.append(f"\n{section}")
            fallback_lines.extend(lines or ["- None"])
        prompt = {
            "sections": structured_sections,
            "instruction": "Normalize into a concise Friday roundup. Preserve all facts. Do not invent metrics.",
        }
        return self.complete("You turn structured marketing task data into concise executive summaries.", str(prompt), max_tokens=1200) or "\n".join(fallback_lines)

    def draft_verification_comment(self, status: str, issues: list[str]) -> str:
        fallback = f"Marked {status.lower()} — " + " ".join(issues)
        prompt = {
            "status": status,
            "issues": issues,
            "instruction": "Draft one concise Notion task comment. Preserve requirements exactly.",
        }
        return self.complete("You write clear operational follow-up comments.", str(prompt), max_tokens=300) or fallback
