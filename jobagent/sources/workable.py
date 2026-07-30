"""Workable public widget API.

Docs: https://workable.readme.io/ (Job Board / widget API)
Endpoint: https://apply.workable.com/api/v1/widget/accounts/{token}?details=true
Verified 2026-07: the legacy documented URL www.workable.com/api/accounts/{token}
301-redirects here, making this the canonical public endpoint. The token is
the account subdomain in apply.workable.com/{token}.

Response shape (verified against a live board):
  {"name": ..., "description": ..., "jobs": [
      {"title", "shortcode", "department", "url", "shortlink",
       "application_url", "published_on", "created_at", "country", "city",
       "state", "telecommuting", "locations": [...], "description": <HTML>}]}

A valid account with no published jobs returns "jobs": [] — that is not an
error, the connector just yields nothing.
"""

from __future__ import annotations

import html
import re
from typing import Iterable

from ..models import Job
from .base import Connector, get_json

API = "https://apply.workable.com/api/v1/widget/accounts/{token}?details=true"


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def _location(j: dict) -> str:
    parts = [j.get("city") or "", j.get("state") or "", j.get("country") or ""]
    loc = ", ".join(p for p in parts if p)
    if j.get("telecommuting") and "remote" not in loc.lower():
        loc = f"{loc} (Remote)" if loc else "Remote"
    return loc


class WorkableConnector(Connector):
    kind = "workable"

    def fetch(self) -> Iterable[Job]:
        data = get_json(API.format(token=self.token))
        for j in data.get("jobs", []):
            url = j.get("url") or j.get("shortlink") or ""
            yield Job(
                source="workable",
                company=self.label,
                title=j.get("title", ""),
                url=url,
                apply_url=j.get("application_url", "") or url,
                location=_location(j),
                description=_strip_html(j.get("description", "")),
                external_id=str(j.get("shortcode", "")),
                posted_at=j.get("published_on", "") or j.get("created_at", ""),
            )
