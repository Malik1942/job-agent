"""Round-trip tests for the setup wizard's pure builders.

Run from the repo root:
    python3 scripts/test_setup_wizard.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobagent.answers import AnswerBook                       # noqa: E402
from jobagent.config import load_config, profile_gaps         # noqa: E402
from jobagent.setup_wizard import (                           # noqa: E402
    build_answers_md,
    build_config_yaml,
    write_outputs,
)

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"[{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        failures.append(name)


STATE = {
    "first_name": "Alex", "last_name": "Rivera", "full_name": "Alex Rivera",
    "phonetic_name": "AL-ex ri-VAIR-ah", "pronouns": "they/them",
    "email": "alex.rivera@testmail.local", "phone": "+1 555 010 0100",
    "location": "Portland, OR", "state": "Oregon",
    "website": "https://alexrivera.example.com",
    "linkedin": "https://www.linkedin.com/in/alex-rivera-example",
    "github": "https://github.com/alex-rivera-example",
    "auth_status": "yes", "sponsorship": "no",
    "auth_notes": "Authorized to work in the US.",
    "titles": "Product Designer, Design Engineer",
    "skills": "prototyping, design systems",
    "exclude_keywords": "principal, director",
    "remote_ok": "yes", "locations": "Portland, Remote",
    "sources": ["greenhouse", "lever"],
    "source_tokens": "greenhouse:examplecorp\nlever:samplestartup",
    "resume_default": "resume.pdf",
    "resume_variants": [
        {"keywords": "design engineer, ux engineer", "path": "de-resume.pdf"},
    ],
    "families": {
        "product_designer": {
            "keywords": "",
            "identity": "I am a product designer who prototypes in code.",
            "craft": "I test ideas by building them.",
            "closing_mix": "craft, judgment, and shipping",
            "marker": "product designer",
        },
    },
    "projects": [
        {"keywords": "ai, ios", "text": "Fieldnote is an AI note-capture app I shipped."},
    ],
    "facts_background": "Product designer, 3 years experience.",
    "facts_projects": "Fieldnote: AI note-capture iOS app.",
    "facts_standard": "Q: What are your salary expectations?\nA: Open and flexible.",
}

cfg_text = build_config_yaml(STATE)
answers_text = build_answers_md(STATE)
check("config text mentions generated header", "jobagent setup" in cfg_text)
check("answers text has Fields section", "## Fields" in answers_text)

with tempfile.TemporaryDirectory() as td:
    cfg_path = Path(td) / "config.yaml"
    ans_path = Path(td) / "answers.md"
    written = write_outputs(STATE, cfg_path, ans_path)
    check("both files written", cfg_path.exists() and ans_path.exists())

    cfg = load_config(cfg_path)
    check("round-trip: name", cfg.profile.name == "Alex Rivera")
    check("round-trip: website", cfg.profile.website == "https://alexrivera.example.com")
    check("round-trip: titles", cfg.profile.titles == ["Product Designer", "Design Engineer"])
    check("round-trip: exclude keywords",
          cfg.filters.exclude_keywords == ["principal", "director"])
    check("round-trip: sources", [(s.kind, s.token) for s in cfg.sources]
          == [("greenhouse", "examplecorp"), ("lever", "samplestartup")])
    check("round-trip: resume variant",
          cfg.profile.resume_variants[0].path == "de-resume.pdf")
    check("round-trip: family identity",
          cfg.coverletter.families["product_designer"].identity
          == "I am a product designer who prototypes in code.")
    check("round-trip: project", cfg.coverletter.projects[0].keywords == ["ai", "ios"])
    check("round-trip: answers_path points at written file",
          cfg.profile.answers_path == "answers.md")
    check("written config passes the readiness gate", profile_gaps(cfg) == [])

    book = AnswerBook.from_file(str(ans_path), use_llm=False)
    r = book.resolve("First Name", "text", None)
    check("answers.md resolves First Name", r is not None and r.value == "Alex")
    r = book.resolve("Will you now or in the future require sponsorship?",
                     "select", ["Yes", "No"])
    check("answers.md resolves sponsorship", r is not None and r.value == "No")

    # Overwrite safety: second write backs up the first.
    write_outputs(STATE, cfg_path, ans_path)
    baks = list(Path(td).glob("*.bak-*"))
    check("existing files backed up on rewrite", len(baks) >= 2)

# Defaults still hold: safety switches must come out conservative.
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "c.yaml"
    p.write_text(build_config_yaml(STATE), encoding="utf-8")
    cfg_min = load_config(p)
check("autonomy stays review-mode", cfg_min.autonomy.mode == "review")
check("live_submit stays False", cfg_min.autonomy.live_submit is False)

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    sys.exit(1)
print("all setup wizard builder tests passed")
