"""Unit tests for the State/office aliases (word-boundary matching) and the
per-job Greenhouse question prefetch. No browser, no network — the Greenhouse
API is mocked the same way the source connectors are in smoke_test.py.

Run from the repo root:
    .venv/bin/python scripts/test_question_prefetch.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobagent import gh_questions                      # noqa: E402
from jobagent.answers import AnswerBook                # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"[{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        failures.append(name)


BOOK = AnswerBook.from_text("""## Fields
- first_name: Alex
- location: Seattle, WA
- state: Washington
- office_area_commit: "Yes"
- work_authorization: Yes - authorized to work in the US (F-1 OPT)
""")

# --- word-boundary alias matching -------------------------------------------
r = BOOK.resolve("State", "text")
check("bare 'State' label resolves", r is not None and r.value == "Washington")
r = BOOK.resolve("State *", "select",
                 ["Alabama", "Washington", "Wyoming"])
check("'State *' select maps onto 'Washington' option",
      r is not None and r.value == "Washington")
check("'Personal statement' does NOT get the state answer",
      BOOK.resolve("Personal statement", "textarea") is None)
check("'United States' auth question does NOT get the state answer",
      BOOK.resolve(
          "Are you authorized to work in the United States of America?",
          "select", ["Yes", "No"]).value == "Yes")
r = BOOK.resolve(
    "Do you live in the SF bay area and can commit to being in the office "
    "a few times a week?", "select", ["Yes", "No"])
check("bay-area office-commit question answers Yes (standing policy)",
      r is not None and r.value == "Yes")

# --- greenhouse question prefetch (mocked API) --------------------------------
PAYLOAD = {
    "questions": [
        {"label": "First Name", "required": True,
         "fields": [{"type": "input_text", "values": []}]},
        {"label": "Resume/CV", "required": True,
         "fields": [{"type": "input_file", "values": []}]},
        {"label": "Portfolio deck (PDF)", "required": True,
         "fields": [{"type": "input_file", "values": []}]},
        {"label": "Why do you want to join Oura?", "required": True,
         "fields": [{"type": "textarea", "values": []}]},
        {"label": "Do you live in the SF bay area and can commit to being "
                  "in the office a few times a week?", "required": True,
         "fields": [{"type": "multi_value_single_select",
                     "values": [{"label": "Yes", "value": 1},
                                {"label": "No", "value": 0}]}]},
        {"label": "Optional nickname", "required": False,
         "fields": [{"type": "input_text", "values": []}]},
    ],
    "location_questions": [
        {"label": "City", "required": True,
         "fields": [{"type": "input_text", "values": []}]},
        # Real Greenhouse boards (Oura) list State as ABBREVIATIONS only.
        {"label": "State", "required": True,
         "fields": [{"type": "multi_value_single_select",
                     "values": [{"label": "AL", "value": 1},
                                {"label": "WA", "value": 47},
                                {"label": "WY", "value": 50}]}]},
        # Auto-populated by the page's own JS — must never count as gaps.
        {"label": "Longitude", "required": True,
         "fields": [{"type": "input_hidden", "values": []}]},
        {"label": "Latitude", "required": True,
         "fields": [{"type": "input_hidden", "values": []}]},
    ],
}

real_get_json = gh_questions.get_json
gh_questions.get_json = lambda url, retries=3: PAYLOAD
try:
    qs = gh_questions.fetch_job_questions("oura", "4207833009")
finally:
    gh_questions.get_json = real_get_json

check("fetch normalizes questions + location_questions", len(qs) == 10)
state_q = next(q for q in qs if q["label"] == "State")
check("state options extracted", state_q["options"] == ["AL", "WA", "WY"])

gaps = gh_questions.uncovered_required(BOOK, qs, has_resume=True)
check("State covered: 'Washington' maps onto abbreviation 'WA'",
      "State" not in gaps)
check("hidden Longitude/Latitude are not gaps",
      not any(g in ("Longitude", "Latitude") for g in gaps))
check("bay-area question covered via office_area_commit",
      not any("bay area" in g.lower() for g in gaps))
check("resume upload covered", not any("Resume" in g for g in gaps))
check("unknown required upload flagged",
      any(g.startswith("Portfolio deck") and "attach manually" in g
          for g in gaps))
check("uncovered free-text question flagged",
      any(g.startswith("Why do you want to join Oura?") for g in gaps))
check("optional question ignored", not any("nickname" in g.lower() for g in gaps))
check("City covered via location alias", not any(g == "City" for g in gaps))
check("exactly the 2 real gaps", len(gaps) == 2)

# fetch failure must not raise out of annotate (best-effort by design)
class _Item:
    def __init__(self):
        self.id, self.company, self.source = "JA-001", "Oura", "greenhouse"
        self.external_id = "123"
        self.coverage_checked = False
        self.required_question_count = 0
        self.uncovered_required = []


class _Src:
    kind, token, label = "greenhouse", "oura", "Oura"


from jobagent.config import Profile as _Profile  # noqa: E402


class _Cfg:
    sources = [_Src()]
    profile = _Profile(resume_path="resume.pdf")


def _boom(url, retries=3):
    raise RuntimeError("api down")


gh_questions.get_json = _boom
try:
    item = _Item()
    gh_questions.annotate_review_items(_Cfg(), [item])
    check("fetch failure leaves item unchecked, no raise",
          item.coverage_checked is False)
finally:
    gh_questions.get_json = real_get_json


# --- real per-company shapes found live on Vercel / Oura-Mobile boards -------
VBOOK = AnswerBook.from_text("""## Fields
- country: United States
- consent: "Yes"
- office_area_commit: "Yes"
- how_did_you_hear: Company careers page
- work_authorization: Yes - authorized to work in the US (F-1 OPT)

## Questions
Q: Your authorization to work in the country where you live.
A: I am authorized to work in the country based on a valid work permit and do not need a company to sponsor my visa

Q: Do you live in one of the following states? Alabama, Alaska, Delaware, Kansas, Maine, Mississippi, Montana, Nebraska, New Mexico, North Dakota, South Dakota, West Virginia, or Wyoming.
A: No
""")

r = VBOOK.resolve(
    "Are you currently based in any of these countries? Please note these "
    "are the only countries where we are accepting applications",
    "select", ["United States", "Germany", "United Kingdom", "Other"])
check("country-list question picks 'United States'",
      r is not None and r.value == "United States")

AUTH_OPTS = [
    "I am authorized to work in the country due to my nationality",
    "I am authorized to work in the country based on a valid work permit "
    "and do not need a company to sponsor my visa",
    "I am not authorized to work in the country and need visa support",
    "Other",
]
r = VBOOK.resolve(
    "Your authorization to work in the country where you live. Please "
    "choose the option that describes your work authorization.",
    "select", AUTH_OPTS)
check("descriptive work-auth question picks the work-permit option exactly",
      r is not None and r.value == AUTH_OPTS[1])

r = VBOOK.resolve(
    "Do you live in one of the following states?\nAlabama, Alaska, Delaware, "
    "Kansas, Maine, Mississippi, Montana, Nebraska, New Mexico, North "
    "Dakota, South Dakota, West Virginia, or Wyoming.",
    "select", ["Yes", "No"])
check("state-exclusion list answers No", r is not None and r.value == "No")

r = VBOOK.resolve(
    "By submitting my application, I acknowledge that I have read and "
    "understand Vercel's Job Applicant Privacy Notice",
    "select", ["Acknowledge/Confirm"])
check("consent 'Yes' maps onto 'Acknowledge/Confirm'",
      r is not None and r.value == "Acknowledge/Confirm")

r = VBOOK.resolve(
    "Please double-check all the information provided above. Ensuring "
    "accuracy is crucial, as any errors or omissions may impact the review "
    "of your application.",
    "select", ["I have reviewed and confirmed that all the information "
               "provided is accurate and complete."])
check("accuracy confirmation maps onto the 'I have reviewed' option",
      r is not None and r.value.startswith("I have reviewed"))

r = VBOOK.resolve(
    "Are you comfortable working in a hybrid environment and coming into "
    "our San Francisco office approximately 50% of the time?",
    "select", ["Yes", "No"])
check("hybrid-50% office question answers Yes (policy)",
      r is not None and r.value == "Yes")

check("'Where did you first hear about this role?' stays a human choice "
      "when no careers-page option exists",
      VBOOK.resolve("Where did you first hear about this role?", "select",
                    ["Events", "LinkedIn", "Google Search", "Other job boards"])
      is None)


if failures:
    print(f"\nFAILED: {', '.join(failures)}")
    raise SystemExit(1)
print("\nAll question-prefetch tests passed.")
