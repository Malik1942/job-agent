"""SmartRecruiters public Posting API.

Docs: https://developers.smartrecruiters.com/docs/endpoints (Customer API ->
Posting API). No auth required for published postings.

  List:   GET https://api.smartrecruiters.com/v1/companies/{token}/postings
          (paginated: limit/offset, response has totalFound + content[])
  Detail: GET .../postings/{id}
          (adds applyUrl and jobAd.sections.*.text description HTML)

The token is the company identifier at the end of the default career site,
careers.smartrecruiters.com/{token}. The list result deliberately omits the
description and applyUrl, so we fetch details per posting; a failed detail
fetch degrades that one job (no description, hosted URL) instead of dropping
the whole board.
"""

from __future__ import annotations

import html
import re
from typing import Iterable

from ..models import Job
from .base import Connector, get_json

LIST = ("https://api.smartrecruiters.com/v1/companies/{token}/postings"
        "?limit={limit}&offset={offset}")
DETAIL = "https://api.smartrecruiters.com/v1/companies/{token}/postings/{pid}"
HOSTED = "https://jobs.smartrecruiters.com/{token}/{pid}"

PAGE = 100          # max the API allows per request
MAX_POSTINGS = 500  # safety cap so one giant board cannot stall a run


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def _location(loc: dict) -> str:
    parts = [loc.get("city") or "", loc.get("region") or "",
             (loc.get("country") or "").upper()]
    out = ", ".join(p for p in parts if p)
    if loc.get("remote") and "remote" not in out.lower():
        out = f"{out} (Remote)" if out else "Remote"
    return out


def _description(detail: dict) -> str:
    sections = ((detail.get("jobAd") or {}).get("sections") or {})
    texts = [s.get("text", "") for s in sections.values() if isinstance(s, dict)]
    return _strip_html(" ".join(t for t in texts if t))


class SmartRecruitersConnector(Connector):
    kind = "smartrecruiters"

    def _postings(self) -> Iterable[dict]:
        offset = 0
        while offset < MAX_POSTINGS:
            data = get_json(LIST.format(token=self.token, limit=PAGE,
                                        offset=offset))
            content = data.get("content", [])
            if not content:
                return
            yield from content
            offset += len(content)
            if offset >= int(data.get("totalFound", 0)):
                return

    def fetch(self) -> Iterable[Job]:
        for p in self._postings():
            pid = str(p.get("id", ""))
            hosted = HOSTED.format(token=self.token, pid=pid)
            description, apply_url = "", ""
            try:
                detail = get_json(DETAIL.format(token=self.token, pid=pid),
                                  retries=1)
                description = _description(detail)
                apply_url = detail.get("applyUrl", "") or ""
            except Exception:
                pass  # degrade this job, keep the board
            yield Job(
                source="smartrecruiters",
                company=self.label,
                title=p.get("name", ""),
                url=hosted,
                apply_url=apply_url or hosted,
                location=_location(p.get("location") or {}),
                description=description,
                external_id=pid,
                posted_at=p.get("releasedDate", ""),
            )
