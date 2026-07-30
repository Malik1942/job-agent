"""End-to-end test of the browser form filler against a local fixture that
replicates the ATS behaviors which broke the 2026-07-01 Greenhouse batch.
Needs playwright + chromium installed (same as auto-submit), no network.

Run from the repo root:
    .venv/bin/python scripts/test_fill_fixture.py

Scenarios:
  1. consent NOT authorized in answers  -> held, consent surfaced, no submit
  2. full answers                       -> autocomplete picked, submitted,
                                           verifier sees real confirmation
  3. full answers, fixture rejects      -> clicked submit, no confirmation,
                                           verifier reports NOT submitted
  4. full answers + extra required      -> pre-submit invariant holds even with
     placeholder <select> and empty        hold_on_unknown_required off; the
     required file (?extra=1)              [presubmit] note names State + Resume
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobagent.answers import AnswerBook          # noqa: E402
from jobagent.browser import fill_and_submit     # noqa: E402
from jobagent.config import load_config          # noqa: E402
from jobagent.models import Job                  # noqa: E402

FIXTURE = (ROOT / "scripts" / "fill_fixture.html").resolve().as_uri()

BASE_ANSWERS = """\
## Fields
- first_name: Alex
- last_name: Rivera
- email: alex.rivera@example.com
- location: Seattle, WA
- work_authorization: Yes - authorized to work in the US (F-1 OPT)
"""

CONSENT_LINE = "- consent: Yes\n"


def make_cfg(tmp: Path, answers_text: str):
    answers = tmp / "answers.md"
    answers.write_text(answers_text, encoding="utf-8")
    cfg_yaml = tmp / "config.yaml"
    cfg_yaml.write_text(
        f"""
profile:
  name: "Alex Rivera"
  answers_path: "{answers}"
  resume_path: ""
autonomy: {{ mode: "auto", live_submit: true }}
apply:
  headless: true
  hold_on_unknown_required: true
  screenshot_dir: "{tmp / 'shots'}"
""", encoding="utf-8")
    return load_config(cfg_yaml)


def job(url: str) -> Job:
    return Job(source="fixture", company="FixtureCo",
               title="Product Designer", url=url, apply_url=url)


def main() -> int:
    failures = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        # 1. Consent not authorized -> held before submit, consent named.
        cfg = make_cfg(tmp, BASE_ANSWERS)
        book = AnswerBook.from_file(cfg.profile.answers_path)
        r1 = fill_and_submit(job(FIXTURE), cfg, book, live=True)
        ok1 = (r1.status == "needs_review"
               and any("agree" in u.lower() or "consent" in u.lower()
                       for u in r1.unanswered_required))
        print(f"[{'ok' if ok1 else 'FAIL'}] consent gate: status={r1.status} "
              f"unanswered={r1.unanswered_required}")
        if not ok1:
            failures.append("consent gate")

        # 2. Full answers -> real submission, verifier sees confirmation.
        cfg = make_cfg(tmp, BASE_ANSWERS + CONSENT_LINE)
        book = AnswerBook.from_file(cfg.profile.answers_path)
        r2 = fill_and_submit(job(FIXTURE), cfg, book, live=True)
        picked = [f for f in r2.filled if "selected" in f]
        ok2 = (r2.status == "applied"
               and "confirmation" in r2.note
               and any("Seattle, Washington" in f for f in picked))
        print(f"[{'ok' if ok2 else 'FAIL'}] happy path: status={r2.status}")
        print(f"        note: {r2.note}")
        for f in picked:
            print(f"        {f}")
        if not ok2:
            failures.append("happy path")

        # 3. Fixture rejects the submit -> verifier must NOT report applied.
        r3 = fill_and_submit(job(FIXTURE + "?mode=reject"), cfg, book,
                             live=True)
        ok3 = r3.status != "applied" and "NOT submitted" in r3.note
        print(f"[{'ok' if ok3 else 'FAIL'}] reject path: status={r3.status}")
        print(f"        note: {r3.note[:160]}")
        if not ok3:
            failures.append("reject path")

        # 4. Extra required fields left unfilled -> the pre-submit invariant
        #    holds even with hold_on_unknown_required OFF. Three classes:
        #      - State: placeholder <select> whose option carries a NON-empty
        #        value (the class the old value==="" sweep missed)
        #      - Preferred work region: a "weird" placeholder (non-sentinel
        #        value, non-placeholder label, not disabled) that the structural
        #        verdict ALONE cannot flag — held only because the filler
        #        resolved no answer for it (the no-answer fold, the residual)
        #      - Resume: an empty required file input
        #    Everything else fills, so exactly these three must fail.
        cfg = make_cfg(tmp, BASE_ANSWERS + CONSENT_LINE)
        cfg.apply.hold_on_unknown_required = False
        book = AnswerBook.from_file(cfg.profile.answers_path)
        r4 = fill_and_submit(job(FIXTURE + "?extra=1"), cfg, book, live=True)
        named_state = any("state" in u.lower() for u in r4.unanswered_required)
        named_region = any("region" in u.lower() for u in r4.unanswered_required)
        named_resume = any("resume" in u.lower() for u in r4.unanswered_required)
        ok4 = (r4.status == "needs_review"
               and r4.note.startswith("[presubmit]")
               and "not submitted" in r4.note.lower()
               and named_state and named_region and named_resume)
        print(f"[{'ok' if ok4 else 'FAIL'}] presubmit invariant: "
              f"status={r4.status}")
        print(f"        note: {r4.note[:240]}")
        print(f"        unanswered: {r4.unanswered_required}")
        if not ok4:
            failures.append("presubmit invariant")

    if failures:
        print(f"\nFAILED: {', '.join(failures)}")
        return 1
    print("\nAll fixture scenarios passed — the filler selects autocomplete "
          "options, consent boxes gate on your answers file, and 'applied' "
          "now requires a real confirmation page.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
