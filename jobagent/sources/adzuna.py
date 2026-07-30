"""Adzuna job search API — a DISCOVERY source, not an auto-apply source.

Docs: https://developer.adzuna.com/ (free app id/key required)
Endpoint: GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}
              ?app_id=...&app_key=...&results_per_page=50&what=...&where=...

Credentials come from the ADZUNA_APP_ID / ADZUNA_APP_KEY environment
variables, never from config. If they are unset the source is skipped with a
clear per-board note instead of failing the run.

Token format: "country[:what[:where]]", e.g.
  "us"                              — everything, ranked by your profile
  "us:product designer"             — keyword search
  "us:product designer:seattle"     — keyword + location

Routing: Adzuna aggregates postings from everywhere, including boards this
tool must never auto-submit to. Every job is therefore marked scan_only=True
UNLESS its apply URL points at an ATS this tool has a real connector for
(greenhouse / lever / ashby / workable / smartrecruiters / recruitee) — those
are routed as normal appliable jobs.
"""

from __future__ import annotations

import html
import os
import re
from typing import Iterable
from urllib.parse import quote

from ..models import Job
from .base import Connector, get_json

API = ("https://api.adzuna.com/v1/api/jobs/{country}/search/1"
       "?app_id={app_id}&app_key={app_key}&results_per_page=50"
       "&content-type=application/json")

ID_ENV = "ADZUNA_APP_ID"
KEY_ENV = "ADZUNA_APP_KEY"

# Apply-URL hosts that map onto our real ATS connectors. Anything else stays
# scan_only. Deliberately NOT here: LinkedIn, Indeed, ZipRecruiter, Workday.
APPLIABLE_ATS = re.compile(
    r"https?://("
    r"(boards|job-boards)\.greenhouse\.io/"
    r"|jobs\.lever\.co/"
    r"|jobs\.ashbyhq\.com/"
    r"|apply\.workable\.com/"
    r"|(jobs|careers)\.smartrecruiters\.com/"
    r"|[a-z0-9-]+\.recruitee\.com/"
    r")", re.I)


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    return html.unescape(re.sub(r"\s+", " ", text)).strip()


def is_appliable_ats(url: str) -> bool:
    return bool(APPLIABLE_ATS.match(url or ""))


class AdzunaConnector(Connector):
    kind = "adzuna"

    def fetch(self) -> Iterable[Job]:
        app_id = os.environ.get(ID_ENV, "").strip()
        app_key = os.environ.get(KEY_ENV, "").strip()
        if not app_id or not app_key:
            raise RuntimeError(
                f"skipped — {ID_ENV}/{KEY_ENV} not set; get free keys at "
                "https://developer.adzuna.com and export both to enable "
                "this source")

        parts = (self.token or "us").split(":")
        country = (parts[0] or "us").lower()
        what = parts[1].strip() if len(parts) > 1 else ""
        where = parts[2].strip() if len(parts) > 2 else ""

        url = API.format(country=country, app_id=app_id, app_key=app_key)
        if what:
            url += f"&what={quote(what)}"
        if where:
            url += f"&where={quote(where)}"

        data = get_json(url)
        for r in data.get("results", []):
            link = r.get("redirect_url", "") or ""
            yield Job(
                source="adzuna",
                company=(r.get("company") or {}).get("display_name", "")
                or "Unknown",
                title=r.get("title", ""),
                url=link,
                apply_url=link,
                location=(r.get("location") or {}).get("display_name", ""),
                description=_strip_html(r.get("description", "")),
                external_id=str(r.get("id", "")),
                posted_at=r.get("created", ""),
                scan_only=not is_appliable_ats(link),
            )
