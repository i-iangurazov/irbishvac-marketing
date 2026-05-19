from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from ..config import Settings


logger = logging.getLogger(__name__)


class EmailClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def available(self) -> bool:
        return not self.settings.missing_email_credentials()

    def send_email(self, subject: str, body: str, recipients: list[str]) -> bool:
        if not self.available:
            logger.warning("email_credentials_missing", extra={"missing": self.settings.missing_email_credentials()})
            return False
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.settings.email_from
        msg["To"] = ", ".join(recipients)
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=20) as smtp:
                smtp.starttls()
                smtp.login(self.settings.smtp_user, self.settings.smtp_pass)
                smtp.send_message(msg)
            logger.info("email_sent", extra={"recipients": recipients, "subject": subject})
            return True
        except Exception:
            logger.exception("email_failure", extra={"recipients": recipients, "subject": subject})
            return False

