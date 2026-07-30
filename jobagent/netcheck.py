"""Egress-IP reputation preflight.

Greenhouse's single highest-risk fraud signal is "IP address linked to a data
center rather than a residential location," and Ashby flags the same class of
anonymized traffic. This module refuses to click submit from a datacenter /
VPN / proxy egress so a genuine application is never filed through the exact
infrastructure an ATS reads as fraud.

This is a COMPLIANCE gate, not evasion: when the IP looks like anonymizing
infra we STOP and tell you to turn the VPN off. We never try to hide, spoof,
or rotate the IP.

Lookup uses ip-api.com's free, no-key endpoint (hosting/proxy/mobile flags).
It fails OPEN — if the check cannot run (offline, API down, rate limited) we
allow the submit rather than break the pipeline on a flaky network; the gate
is a safety net, not a hard dependency. It fails CLOSED only on a positive
datacenter/proxy verdict, which is the case we actually care about.
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass

# Only the fields we act on; keeps the response small and the intent legible.
_IP_API_URL = (
    "http://ip-api.com/json/?fields="
    "status,message,query,proxy,hosting,mobile,isp,org,as"
)


@dataclass
class IPVerdict:
    checked: bool                 # did the lookup actually complete?
    ip: str = ""
    is_datacenter: bool = False   # hosting / datacenter range
    is_proxy: bool = False        # proxy / VPN / Tor exit
    org: str = ""
    reason: str = ""

    @property
    def safe_to_submit(self) -> bool:
        # Unknown (checked is False) is treated as safe: fail open so a network
        # hiccup never blocks the whole pipeline. Only a positive verdict stops.
        if not self.checked:
            return True
        return not (self.is_datacenter or self.is_proxy)


def egress_ip_reputation(timeout: float = 6.0) -> IPVerdict:
    """Look up the current egress IP's reputation. Never raises."""
    try:
        req = urllib.request.Request(
            _IP_API_URL, headers={"User-Agent": "jobagent-preflight"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # offline, DNS, timeout, bad JSON — all fail open
        return IPVerdict(checked=False, reason=f"ip reputation lookup skipped: {exc}")

    if data.get("status") != "success":
        return IPVerdict(
            checked=False,
            reason=f"ip reputation lookup skipped: {data.get('message', 'unknown error')}",
        )

    hosting = bool(data.get("hosting"))
    proxy = bool(data.get("proxy"))
    ip = data.get("query", "") or ""
    org = data.get("org") or data.get("isp") or data.get("as") or ""

    labels = []
    if hosting:
        labels.append("datacenter/hosting")
    if proxy:
        labels.append("proxy/VPN")

    if labels:
        reason = (
            f"egress IP {ip} looks like {' + '.join(labels)} ({org}) — this is "
            f"Greenhouse/Ashby's top fraud signal. Turn off any VPN and run from "
            f"your home network before submitting."
        )
    else:
        reason = f"egress IP {ip} looks residential ({org})"

    return IPVerdict(
        checked=True, ip=ip, is_datacenter=hosting, is_proxy=proxy,
        org=org, reason=reason,
    )


# Process-level single-slot cache: the egress IP does not change mid-run.
_CACHE: dict[str, tuple[float, IPVerdict]] = {}


def egress_ip_reputation_cached(ttl: float = 300.0, timeout: float = 6.0) -> IPVerdict:
    """egress_ip_reputation with a short cache so a batch of submits does not
    hit ip-api once per job (rate limit + ~6s latency each). Only a COMPLETED
    lookup is cached; a failed one retries next call (it fails open, so it
    never blocks). ttl bounds how long a stale verdict survives after you
    connect/disconnect a VPN mid-session."""
    now = time.monotonic()
    cached = _CACHE.get("v")
    if cached is not None and (now - cached[0]) < ttl:
        return cached[1]
    verdict = egress_ip_reputation(timeout=timeout)
    if verdict.checked:
        _CACHE["v"] = (now, verdict)
    return verdict
