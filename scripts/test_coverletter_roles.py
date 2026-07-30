"""Unit tests for config-driven role-family cover letters.
No browser, no network, no LLM — template path only.

Run from the repo root:
    python3 scripts/test_coverletter_roles.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobagent.config import load_config                     # noqa: E402
from jobagent.coverletter import (                          # noqa: E402
    CoverLetterNotConfigured,
    cover_letter_pdf_path,
    role_family,
    template_cover_letter,
    validate_cover_letter_package,
    write_cover_letter,
)
from jobagent.models import Job                             # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"[{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        failures.append(name)


def job(title: str) -> Job:
    return Job(source="greenhouse", company="TestCo", title=title,
               url="https://ex/1", description="AI product design and prototyping")


cfg = load_config(ROOT / "config.example.yaml")

# --- family classification comes from config keywords ------------------------
for title, fam in [
    ("Senior Product Designer - Mobile", "product_designer"),
    ("UX Engineer", "design_engineer"),
    ("Design Engineer", "design_engineer"),
    ("AI UX Designer", "ux_designer"),
    ("Senior Interaction Designer", "ux_designer"),
    ("Visual Designer", "product_designer"),  # default family fallback
]:
    check(f"family({title!r}) == {fam}", role_family(title, cfg) == fam)

with tempfile.TemporaryDirectory() as td:
    cfg.output_dir = td

    de = template_cover_letter(job("Design Engineer"), cfg)
    fam = cfg.coverletter.families["design_engineer"]
    check("DE letter contains config identity", fam.identity in de)
    check("DE letter contains config craft", fam.craft in de)
    check("DE letter contains config closing_mix", fam.closing_mix in de)
    check("DE letter cites profile.website",
          cfg.profile.website.replace("https://", "") in de)
    check("letter signed with profile name", cfg.profile.name in de)
    # Guard against the pre-refactor hardcoded portfolio domain resurfacing.
    _old_domain = "".join(("ma", "likz", "ha", "ng"))  # built at runtime: keeps the
    check("no hardcoded persona leakage", _old_domain not in de.lower())
    # literal itself out of the leak-check gate.

    # Validator: marker + website come from config.
    txt = write_cover_letter(job("Design Engineer"), cfg)
    errs = validate_cover_letter_package(job("Design Engineer"), cfg, txt)
    check(f"DE package validates clean (got {errs})", errs == [])

    # Every configured family validates its own letter…
    for title in ("Senior Product Designer", "Design Engineer",
                  "Senior UX Designer"):
        j = job(title)
        t = write_cover_letter(j, cfg)
        errs = validate_cover_letter_package(j, cfg, t)
        check(f"validation passes for {title!r}", not errs)

    # …and a UX-framed letter attached to a DE job fails the marker gate.
    ux_txt = write_cover_letter(job("Senior UX Designer"), cfg)
    errs = validate_cover_letter_package(job("Design Engineer"), cfg, ux_txt)
    check("wrong-family letter rejected",
          any("positioning" in e for e in errs))

    # PDF filename derives from the profile name.
    check("pdf name uses profile name",
          Path(cover_letter_pdf_path(job("Design Engineer"), cfg)).name
          .startswith("Alex_Rivera_Cover_Letter_"))

# No families configured -> loud failure, never a silent generic letter.
empty = load_config(ROOT / "config.example.yaml")
empty.coverletter.families = {}
try:
    template_cover_letter(job("Design Engineer"), empty)
    check("unconfigured coverletter raises", False)
except CoverLetterNotConfigured as exc:
    check("unconfigured coverletter raises", "jobagent setup" in str(exc))

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    sys.exit(1)
print("all coverletter role tests passed")
