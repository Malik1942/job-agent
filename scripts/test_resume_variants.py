"""Unit tests for role-specific resume selection (profile.resume_variants).

Run from the repo root:
    .venv/bin/python scripts/test_resume_variants.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobagent.config import Profile, ResumeVariant, load_config  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"[{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        failures.append(name)


DE, UX, PD = ("design engineer resume.pdf", "ux designer resume.pdf",
              "product designer resume.pdf")
profile = Profile(resume_path=PD, resume_variants=[
    ResumeVariant(path=DE, keywords=["design engineer", "design technologist",
                                     "ux engineer"]),
    ResumeVariant(path=UX, keywords=["ux designer", "user experience designer",
                                     "interaction designer"]),
    ResumeVariant(path=PD, keywords=["product designer", "product design",
                                     "ai designer"]),
])

CASES = [
    # real titles from the live queue
    ("Senior Product Designer", PD),
    ("Product Designer II (Health)", PD),
    ("Staff Product Designer ", PD),
    ("Senior AI-Native Product Designer ", PD),
    ("Senior Product Designer - Mobile", PD),
    ("Manufacturing Design Engineer II (NPI)", DE),
    # class coverage
    ("UX Designer", UX),
    ("AI UX Designer", UX),
    ("Senior Interaction Designer", UX),
    ("UX Engineer", DE),
    ("Design Technologist", DE),
    ("Product Design Engineer", DE),      # order: engineer variant first
    ("Visual Designer", PD),              # no keyword -> fallback
    ("", PD),                             # empty title -> fallback
]
for title, expected in CASES:
    got = profile.resume_for_title(title)
    check(f"{title or '(empty)'!r} -> {Path(expected).stem}", got == expected)

check("any_resume_configured with variants only",
      Profile(resume_variants=[ResumeVariant(path=DE)]).any_resume_configured)
check("any_resume_configured false when nothing set",
      not Profile().any_resume_configured)

# --- config parsing -----------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    cfg_path = Path(td) / "config.yaml"
    cfg_path.write_text("""
profile:
  name: "T"
  resume_path: "fallback.pdf"
  resume_variants:
    - { keywords: ["design engineer"], path: "de.pdf" }
    - { keywords: ["ux designer"], path: "ux.pdf" }
""", encoding="utf-8")
    cfg = load_config(cfg_path)
    check("yaml variants parse into typed ResumeVariant",
          len(cfg.profile.resume_variants) == 2
          and cfg.profile.resume_variants[0].path == "de.pdf")
    check("parsed selection works end to end",
          cfg.profile.resume_for_title("Senior UX Designer") == "ux.pdf"
          and cfg.profile.resume_for_title("PM") == "fallback.pdf")

    bad = Path(td) / "bad.yaml"
    bad.write_text("profile:\n  resume_variants:\n    - { keywords: [x] }\n",
                   encoding="utf-8")
    try:
        load_config(bad)
        check("variant without path rejected", False)
    except ValueError:
        check("variant without path rejected", True)


if failures:
    print(f"\nFAILED: {', '.join(failures)}")
    raise SystemExit(1)
print("\nAll resume-variant tests passed.")
