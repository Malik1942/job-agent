"""Unit tests for the Ashby form fetch + per-job answer sheet. No network —
the GraphQL POST is monkeypatched with a captured-shape response.

Run from the repo root:
    .venv/bin/python scripts/test_ashby_form.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobagent import ashby_form                      # noqa: E402
from jobagent.answers import AnswerBook              # noqa: E402
from jobagent.models import Job                      # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"[{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        failures.append(name)


# Response shaped exactly like the real jobs.ashbyhq.com ApiJobPosting payload
# (Replit Design Engineer), incl. the ValueSelect a DOM scan misses.
FAKE = {"data": {"jobPosting": {"id": "x", "title": "Design Engineer",
    "applicationForm": {"sections": [
        {"title": "Details", "fieldEntries": [
            {"isRequired": True, "field": {"title": "Full Name", "type": "String"}},
            {"isRequired": True, "field": {"title": "Email", "type": "Email"}},
            {"isRequired": True, "field": {"title": "Resume", "type": "File"}},
            {"isRequired": False, "field": {"title": "Portfolio URL", "type": "String"}},
            {"isRequired": True, "field": {"title": "Location", "type": "Location"}},
        ]},
        {"title": "Questions", "fieldEntries": [
            {"isRequired": True, "field": {"title": "What excites you about Replit?",
                                           "type": "LongText"}},
            {"isRequired": True, "field": {"title": "How many years of relevant "
                "professional experience do you have?", "type": "ValueSelect",
                "selectableValues": [{"label": "1-2 years"}, {"label": "3-5 years"},
                                     {"label": "6-8 years"}, {"label": "8+"}]}},
            {"isRequired": True, "field": {"title": "Are you able to work from our "
                "Foster City, CA HQ 3 days per week?", "type": "Boolean"}},
        ]},
        {"title": "Authorization", "fieldEntries": [
            {"isRequired": True, "field": {"title": "Are you at least 18 years of age?",
                                           "type": "Boolean"}},
            {"isRequired": True, "field": {"title": "Are you legally authorized to "
                "work in the United States?", "type": "Boolean"}},
        ]},
    ]}}}}


class _Resp:
    status_code = 200
    def raise_for_status(self): pass
    def json(self): return FAKE


real_post = ashby_form.requests.post
ashby_form.requests.post = lambda *a, **k: _Resp()
try:
    qs = ashby_form.fetch_application_questions("replit", "abc")
finally:
    ashby_form.requests.post = real_post

check("fetches all fields across sections", len(qs) == 10)
yrs = next(q for q in qs if q["label"].startswith("How many years"))
check("ValueSelect options parsed (the field a DOM scan misses)",
      yrs["options"] == ["1-2 years", "3-5 years", "6-8 years", "8+"])
boolq = next(q for q in qs if q["atype"] == "Boolean")
check("Boolean field gets Yes/No options", boolq["options"] == ["Yes", "No"])

BOOK = AnswerBook.from_text("""## Fields
- full_name: Alex Rivera
- email: alex.rivera@example.com
- location: Seattle, WA
- office_area_commit: "Yes"
- office_three_days_per_week: "Yes"
- work_authorization: Yes - authorized to work in the US (F-1 OPT)

## Questions
Q: Are you at least 18 years of age?
A: Yes
Q: What excites you about Replit?
A: Replit is building the place where anyone can go from idea to running software.
""")

ashby_form.requests.post = lambda *a, **k: _Resp()
try:
    rows = ashby_form.answer_sheet("replit", "abc", BOOK, "de_resume.pdf",
                                   cover_pdf_path="cover.pdf")
finally:
    ashby_form.requests.post = real_post

by = {r["label"]: r for r in rows}
check("resume file field points at the resume variant",
      by["Resume"]["source"] == "file" and by["Resume"]["answer"] == "de_resume.pdf")
check("covered text answer resolved",
      by["Full Name"]["answer"] == "Alex Rivera" and not by["Full Name"]["gap"])
check("office-commit Boolean answered Yes",
      by["Are you able to work from our Foster City, CA HQ 3 days per week?"]["answer"]
      == "Yes")
check("age question answered from Q/A", by["Are you at least 18 years of age?"]["answer"] == "Yes")
check("the ValueSelect years question is flagged as a GAP (no saved answer)",
      by["How many years of relevant professional experience do you have?"]["gap"] is True)
check("optional unanswered field is NOT a gap",
      by["Portfolio URL"]["gap"] is False)

# URL parsing
job = Job(source="ashby", company="Replit", title="Design Engineer",
          url="https://jobs.ashbyhq.com/replit/2f1de5f1-48c2-4104-a697-a51fe1620508",
          apply_url="https://jobs.ashbyhq.com/replit/2f1de5f1-48c2-4104-a697-a51fe1620508/application",
          external_id="2f1de5f1-48c2-4104-a697-a51fe1620508")
org, jid = ashby_form._org_and_id(job)
check("org + job id parsed from apply_url", org == "replit" and jid.startswith("2f1de5f1"))


if failures:
    print(f"\nFAILED: {', '.join(failures)}")
    raise SystemExit(1)
print("\nAll Ashby-form tests passed.")
