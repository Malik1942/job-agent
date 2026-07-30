"""Send a summary of new matches to email and/or Slack.

Secrets (SMTP password, Slack webhook URL) are read from environment variables,
never from config, so nothing sensitive lives in your repo. Every channel fails
soft: a missing secret or a send error is logged to the returned notes and the
pipeline carries on. It notifies only about records created in the current run
(the pipeline dedupes, so those are genuinely new).
"""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage

import requests

from .config import Config
from .models import ApplicationRecord


class Notifier:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.n = cfg.notify

    def enabled(self) -> bool:
        return self.n.email.enabled or self.n.slack.enabled

    def _filter(self, records: list[ApplicationRecord]) -> list[ApplicationRecord]:
        keep = []
        for r in records:
            if self.n.only_status and r.status not in self.n.only_status:
                continue
            if r.score < self.n.min_score:
                continue
            keep.append(r)
        keep.sort(key=lambda r: r.score, reverse=True)
        return keep

    def _summary(self, records: list[ApplicationRecord]) -> tuple[str, str]:
        subject = f"jobagent: {len(records)} new match" + ("es" if len(records) != 1 else "")
        lines = []
        for r in records:
            lines.append(f"[{r.status}] {r.score:.2f}  {r.company} - {r.title}")
            lines.append(f"    {r.url}")
            if r.note:
                lines.append(f"    {r.note}")
            lines.append("")
        return subject, "\n".join(lines).strip()

    def notify(self, records: list[ApplicationRecord]) -> list[str]:
        """Returns human-readable notes about what happened per channel."""
        notes: list[str] = []
        if not self.enabled():
            return notes

        matches = self._filter(records)
        if not matches:
            notes.append("notify: no new records matched the notify filter")
            return notes

        subject, body = self._summary(matches)

        if self.n.email.enabled:
            notes.append(self._send_email(subject, body))
        if self.n.slack.enabled:
            notes.append(self._send_slack(f"*{subject}*\n\n{body}"))
        return notes

    # ---- channels ----------------------------------------------------------
    def _send_email(self, subject: str, body: str) -> str:
        e = self.n.email
        password = os.getenv(e.password_env, "")
        if not (e.smtp_host and e.from_addr and e.to_addrs and password):
            return (f"notify/email: skipped (need smtp_host, from_addr, to_addrs, "
                    f"and ${e.password_env})")
        try:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = e.from_addr
            msg["To"] = ", ".join(e.to_addrs)
            msg.set_content(body)
            with smtplib.SMTP(e.smtp_host, e.smtp_port, timeout=30) as s:
                s.starttls()
                s.login(e.username or e.from_addr, password)
                s.send_message(msg)
            return f"notify/email: sent to {len(e.to_addrs)} recipient(s)"
        except Exception as exc:
            return f"notify/email: failed ({exc})"

    def _send_slack(self, text: str) -> str:
        url = os.getenv(self.n.slack.webhook_env, "")
        if not url:
            return f"notify/slack: skipped (set ${self.n.slack.webhook_env})"
        try:
            resp = requests.post(url, json={"text": text}, timeout=20)
            resp.raise_for_status()
            return "notify/slack: sent"
        except Exception as exc:
            return f"notify/slack: failed ({exc})"
