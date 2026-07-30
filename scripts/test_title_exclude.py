"""Calibration tests for the hardware/facilities title exclusion.

Guards the exact risk: hardware "… Design Engineer" roles drop, but a bare
"Design Engineer" (incl. software/hardware-hybrid AI-team roles) and all
product/UX/design-engineer titles survive. Calibrated against real
OpenAI/Whoop/Anthropic postings.

Run from the repo root:
    .venv/bin/python scripts/test_title_exclude.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobagent.config import load_config     # noqa: E402
from jobagent.models import Job             # noqa: E402
from jobagent.scoring import score_job      # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"[{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        failures.append(name)


cfg = load_config("config.example.yaml")
DESC = "AI product design and prototyping, design systems, Figma, user research"


def score(title: str) -> float:
    j = Job(source="ashby", company="Co", title=title, url="x", description=DESC)
    return score_job(j, cfg).score


# EXCLUDED — real hardware/facilities titles that were mis-caught.
for title in [
    "Mechanical Design Engineer", "Physical Design Engineer",
    "Data Center Design Engineer, Electrical - Industrial Compute",
    "Audiovisual Design Engineer", "Manufacturing Design Engineer II (NPI)",
    "Senior Mechanical Engineer (NPI)", "ASIC Firmware Engineer, Modeling",
    "Civil Engineer", "Data Center Electrical Engineer",
]:
    check(f"EXCLUDED: {title!r}", score(title) == 0.0)

# KEPT — real design fits that must survive (the over-kill guard).
for title in [
    "Design Engineer",                       # bare — Runway/Replit
    "Design Engineer, Web",                   # Anthropic software design-eng
    "Design Engineer, Education Labs",
    "Product Designer, ChatGPT",
    "Senior Product Designer",
    "AI Product Designer",
    "UX Designer", "UX Engineer",             # the profile's own titles
    "Interaction Designer", "Design Technologist",
]:
    check(f"KEPT (score>0): {title!r}", score(title) > 0.0)


if failures:
    print(f"\nFAILED: {', '.join(failures)}")
    raise SystemExit(1)
print("\nAll title-exclusion calibration tests passed.")
