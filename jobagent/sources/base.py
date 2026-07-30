"""Shared plumbing for source connectors."""

from __future__ import annotations

import time
from typing import Iterable

import requests

from ..models import Job

USER_AGENT = "jobagent/0.1 (personal job search tool)"
TIMEOUT = 20
RETRIES = 3
BACKOFF = 1.5  # seconds, multiplied by attempt number
RETRY_STATUS = {429, 500, 502, 503, 504}


def get_json(url: str, retries: int = RETRIES) -> dict | list:
    """GET a public JSON endpoint, retrying transient failures with backoff.

    Retries on 429 / 5xx and network errors so one flaky response doesn't drop
    a whole board for the run. Raises after the last attempt so the caller can
    log it as a per-source error.
    """
    last: Exception | str | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT},
                                timeout=TIMEOUT)
            if resp.status_code in RETRY_STATUS:
                last = f"HTTP {resp.status_code}"
                time.sleep(BACKOFF * attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last = exc
            if attempt < retries:
                time.sleep(BACKOFF * attempt)
    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last}")


class Connector:
    """Base connector. Subclasses turn a board token into a list of Jobs."""

    kind = "base"

    def __init__(self, token: str, label: str = ""):
        self.token = token
        self.label = label or token

    def fetch(self) -> Iterable[Job]:  # pragma: no cover - interface only
        raise NotImplementedError
