"""Source registry.

Auto-apply connectors talk to a documented public job-board API. LinkedIn,
Indeed, and ZipRecruiter are deliberately not here as auto-apply sources:
scripting submissions to them violates their terms of service and gets
accounts banned. Configure those as kind: scan_only instead, which finds and
ranks matches for you to open by hand.
"""

from __future__ import annotations

from typing import Iterable

from ..config import Source
from ..models import Job
from .adzuna import AdzunaConnector
from .ashby import AshbyConnector
from .base import Connector
from .greenhouse import GreenhouseConnector
from .lever import LeverConnector
from .recruitee import RecruiteeConnector
from .smartrecruiters import SmartRecruitersConnector
from .workable import WorkableConnector

REGISTRY: dict[str, type[Connector]] = {
    "greenhouse": GreenhouseConnector,
    "lever": LeverConnector,
    "ashby": AshbyConnector,
    "workable": WorkableConnector,
    "smartrecruiters": SmartRecruitersConnector,
    "recruitee": RecruiteeConnector,
    # Discovery source: jobs are scan_only unless their apply URL points at
    # one of the real ATS connectors above.
    "adzuna": AdzunaConnector,
}


def build_connector(src: Source) -> Connector | None:
    cls = REGISTRY.get(src.kind)
    if cls is None:
        return None
    return cls(token=src.token, label=src.label)


def fetch_source(src: Source) -> tuple[list[Job], str | None]:
    """Return (jobs, error). scan_only sources return nothing to fetch here;
    they are handled by whatever authorized feed you wire into the stub below."""
    if src.kind == "scan_only":
        return [], None

    connector = build_connector(src)
    if connector is None:
        return [], f"unknown source kind: {src.kind}"

    try:
        return list(connector.fetch()), None
    except Exception as exc:  # network, JSON, or schema drift
        return [], f"{src.kind}:{src.label} fetch failed: {exc}"
