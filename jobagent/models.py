"""Core data structures shared across the pipeline."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    """A single job posting normalized across every source."""

    source: str            # greenhouse | lever | ashby | linkedin | indeed ...
    company: str
    title: str
    url: str
    location: str = ""
    description: str = ""
    external_id: str = ""  # the source's own id, when it gives one
    apply_url: str = ""    # where an application is actually submitted
    scan_only: bool = False  # True for sources we never auto-submit to
    posted_at: str = ""

    # Filled in by the scoring stage.
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    @property
    def uid(self) -> str:
        """Stable id used for dedupe. Prefers a real external id, falls back
        to a hash of company + title + url so the same posting is not applied
        to twice across runs."""
        if self.external_id:
            return f"{self.source}:{self.external_id}"
        raw = f"{self.company}|{self.title}|{self.url}".lower()
        return f"{self.source}:{hashlib.sha1(raw.encode()).hexdigest()[:16]}"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ApplicationRecord:
    """A row in the tracker: one attempt at one job."""

    uid: str
    company: str
    title: str
    url: str
    score: float
    status: str            # needs_review | applied | skipped | error | scan_only
    note: str = ""
    cover_letter_path: str = ""
    location: str = ""
    source: str = ""
    screenshot: str = ""
    filled_fields: list[str] = field(default_factory=list)
    unanswered_required: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
