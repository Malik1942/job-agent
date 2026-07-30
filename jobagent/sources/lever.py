"""Lever public postings API.

Endpoint: https://api.lever.co/v0/postings/{company}?mode=json
The company slug is the path segment in jobs.lever.co/{company}.
"""

from __future__ import annotations

from typing import Iterable

from ..models import Job
from .base import Connector, get_json

API = "https://api.lever.co/v0/postings/{token}?mode=json"


class LeverConnector(Connector):
    kind = "lever"

    def fetch(self) -> Iterable[Job]:
        data = get_json(API.format(token=self.token))
        for j in data:
            cats = j.get("categories") or {}
            yield Job(
                source="lever",
                company=self.label,
                title=j.get("text", ""),
                url=j.get("hostedUrl", ""),
                apply_url=(j.get("applyUrl") or j.get("hostedUrl", "")),
                location=cats.get("location", ""),
                description=j.get("descriptionPlain", "") or "",
                external_id=str(j.get("id", "")),
                posted_at=str(j.get("createdAt", "")),
            )
