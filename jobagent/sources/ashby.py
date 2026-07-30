"""Ashby public job board API.

Endpoint: https://api.ashbyhq.com/posting-api/job-board/{token}
The token is the job board name in jobs.ashbyhq.com/{token}.
"""

from __future__ import annotations

from typing import Iterable

from ..models import Job
from .base import Connector, get_json

API = "https://api.ashbyhq.com/posting-api/job-board/{token}"


class AshbyConnector(Connector):
    kind = "ashby"

    def fetch(self) -> Iterable[Job]:
        data = get_json(API.format(token=self.token))
        for j in data.get("jobs", []):
            yield Job(
                source="ashby",
                company=self.label,
                title=j.get("title", ""),
                url=j.get("jobUrl", ""),
                apply_url=j.get("applyUrl", "") or j.get("jobUrl", ""),
                location=j.get("location", "") or "",
                description=j.get("descriptionPlain", "") or "",
                external_id=str(j.get("id", "")),
                posted_at=j.get("publishedAt", "") or "",
            )
