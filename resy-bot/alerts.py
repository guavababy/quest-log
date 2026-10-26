"""Send a short alert by text (Twilio), email, or phone push (ntfy)."""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

import requests

log = logging.getLogger("resybot")


class Alerts:
    def __init__(self, settings: dict):
        self.s = settings
        self.method = settings.get("method", "none")
        if self.method not in ("twilio", "email", "ntfy", "none"):
            raise ValueError(f"alerts.method must be twilio, email, ntfy or none, not {self.method!r}")

    def send(self, text: str) -> bool:
        """Send `text`. Never raises: a failed alert must not stop the bot."""
        if self.method == "none":
            return True
        try:
            getattr(self, "_" + self.method)(text)
            log.info("Alert sent by %s.", self.method)
            return True
        except KeyError as e:
            log.error("Couldn't send %s alert: alerts.%s is missing from config.toml",
                      self.method, e.args[0])
            return False
        except Exception as e:  # noqa: BLE001
            log.error("Couldn't send %s alert: %s", self.method, e)
            return False

    def _secret(self, key: str, env: str) -> str:
        value = os.environ.get(env) or self.s.get(key)
        if not value:
            raise ValueError(f"set alerts.{key} in config.toml or the {env} environment variable")
        return value

    def _twilio(self, text: str):
        sid = self.s["twilio_account_sid"]
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            auth=(sid, self._secret("twilio_auth_token", "TWILIO_AUTH_TOKEN")),
            data={"From": self.s["twilio_from"], "To": self.s["phone"], "Body": text},
            timeout=15,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Twilio HTTP {r.status_code}: {r.text[:200]}")

    def _email(self, text: str):
        msg = EmailMessage()
        msg["Subject"] = "Resy bot"
        msg["From"] = self.s["smtp_user"]
        msg["To"] = self.s["email_to"]
        msg.set_content(text)
        with smtplib.SMTP_SSL(self.s.get("smtp_host", "smtp.gmail.com"),
                              int(self.s.get("smtp_port", 465)), timeout=15) as smtp:
            smtp.login(self.s["smtp_user"], self._secret("smtp_password", "SMTP_PASSWORD"))
            smtp.send_message(msg)

    def _ntfy(self, text: str):
        server = self.s.get("ntfy_server", "https://ntfy.sh").rstrip("/")
        r = requests.post(f"{server}/{self.s['ntfy_topic']}", data=text.encode(),
                          headers={"Title": "Resy bot"}, timeout=15)
        if r.status_code >= 400:
            raise RuntimeError(f"ntfy HTTP {r.status_code}: {r.text[:200]}")
