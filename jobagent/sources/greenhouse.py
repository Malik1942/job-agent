"""Greenhouse public job board API.

Docs: https://developers.greenhouse.io/job-board.html
Endpoint: https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
The token is the board name in a careers URL like boards.greenhouse.io/{token}.
"""

from __future__ import annotations

import html
import re
from typing import Iterable

from ..models import Job
from .base import Connector, get_json

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


class GreenhouseConnector(Connector):
    kind = "greenhouse"

    def fetch(self) -> Iterable[Job]:
        data = get_json(API.format(token=self.token))
        for j in data.get("jobs", []):
            loc = (j.get("location") or {}).get("name", "")
            yield Job(
                source="greenhouse",
                company=self.label,
                title=j.get("title", ""),
                url=j.get("absolute_url", ""),
                apply_url=j.get("absolute_url", ""),
                location=loc,
                description=_strip_html(j.get("content", "")),
                external_id=str(j.get("id", "")),
                posted_at=j.get("updated_at", ""),
            )
