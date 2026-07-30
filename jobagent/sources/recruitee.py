"""Recruitee public careers site API.

Docs: https://docs.recruitee.com/reference/offers
Endpoint: GET https://{token}.recruitee.com/api/offers/
The token is the company subdomain of its careers site,
{token}.recruitee.com. No auth required.

Response shape (verified against a live board):
  {"offers": [
      {"id", "title", "slug", "status", "description": <HTML>,
       "requirements": <HTML>, "careers_url", "careers_apply_url",
       "location", "city", "country", "remote", "hybrid",
       "published_at", ...}]}
"""

from __future__ import annotations

import html
import re
from typing import Iterable

from ..models import Job
from .base import Connector, get_json

API = "https://{token}.recruitee.com/api/offers/"


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def _location(o: dict) -> str:
    loc = o.get("location") or ", ".join(
        p for p in [o.get("city") or "", o.get("country") or ""] if p)
    if (o.get("remote") or o.get("hybrid")) and "remote" not in loc.lower():
        loc = f"{loc} (Remote)" if loc else "Remote"
    return loc


class RecruiteeConnector(Connector):
    kind = "recruitee"

    def fetch(self) -> Iterable[Job]:
        data = get_json(API.format(token=self.token))
        for o in data.get("offers", []):
            if o.get("status") and o.get("status") != "published":
                continue
            description = _strip_html(
                f'{o.get("description", "")} {o.get("requirements", "")}')
            yield Job(
                source="recruitee",
                company=self.label,
                title=o.get("title", ""),
                url=o.get("careers_url", ""),
                apply_url=o.get("careers_apply_url", "")
                or o.get("careers_url", ""),
                location=_location(o),
                description=description,
                external_id=str(o.get("id", "")),
                posted_at=o.get("published_at", "") or o.get("created_at", ""),
            )
