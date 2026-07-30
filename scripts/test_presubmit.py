"""Unit tests for the pre-submit validation verdict — the pure Python core of
the safety invariant. No browser, no network: the in-page JS
(_REQUIRED_FIELDS_JS) does the gathering, and presubmit_verdict() makes the
pass/fail decision on the raw records, so the decision is testable on hand-built
field state.

Run from the repo root:
    .venv/bin/python scripts/test_presubmit.py

Covers (see requirement mapping in the task):
  - req 1/3: a required native <select> stuck on a placeholder option fails,
    even though its option carries a non-empty value (the State-field class).
  - req 1/3: a required file input with no attached file fails.
  - req 3:   an all-filled form passes (empty verdict == safe to submit).
  - req 2:   required-detection evidence is preserved through to the verdict.
  - extras:  combobox placeholder, checkbox/radio unchecked, aria-invalid,
             and non-required placeholder is ignored (no false hold).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobagent.browser import presubmit_verdict  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"[{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        failures.append(name)


# 1. Required native select with a placeholder-labelled option that still
#    carries a non-empty value ("none"). The old sweep (value === "" only)
#    would have missed this; the new rule must fail it.
placeholder_select = {
    "label": "State", "kind": "select", "required": True,
    "evidence": "el.required", "aria_invalid": False,
    "value": "none", "selected_label": "Select a state...",
    "combo_text": "", "checked": False, "file_count": 0,
}
v = presubmit_verdict([placeholder_select])
check("placeholder select fails", len(v) == 1 and v[0]["label"] == "State")
check("placeholder select reason mentions placeholder",
      bool(v) and "placeholder" in v[0]["reason"].lower())
check("placeholder select preserves evidence",
      bool(v) and v[0]["evidence"] == "el.required")

# 2. Required file input with no attached file.
empty_file = {
    "label": "Resume", "kind": "file", "required": True,
    "evidence": "el.required", "aria_invalid": False,
    "value": "", "selected_label": "", "combo_text": "",
    "checked": False, "file_count": 0,
}
v = presubmit_verdict([empty_file])
check("empty required file fails",
      len(v) == 1 and v[0]["label"] == "Resume" and "file" in v[0]["reason"])

attached_file = {**empty_file, "file_count": 1}
check("attached required file passes", presubmit_verdict([attached_file]) == [])

# 3. An all-filled form passes: every required field satisfied.
all_filled = [
    {"label": "First Name", "kind": "text", "required": True,
     "evidence": "el.required", "aria_invalid": False, "value": "Alex",
     "selected_label": "", "combo_text": "", "checked": False, "file_count": 0},
    {"label": "State", "kind": "select", "required": True,
     "evidence": "el.required", "aria_invalid": False, "value": "WA",
     "selected_label": "Washington", "combo_text": "", "checked": False,
     "file_count": 0},
    {"label": "Location", "kind": "combobox", "required": True,
     "evidence": "aria-required", "aria_invalid": False, "value": "",
     "selected_label": "", "combo_text": "Seattle, Washington, United States",
     "checked": False, "file_count": 0},
    {"label": "Consent", "kind": "checkbox", "required": True,
     "evidence": "consent-heuristic", "aria_invalid": False, "value": "",
     "selected_label": "", "combo_text": "", "checked": True, "file_count": 0},
    {"label": "Resume", "kind": "file", "required": True,
     "evidence": "el.required", "aria_invalid": False, "value": "",
     "selected_label": "", "combo_text": "", "checked": False, "file_count": 1},
]
check("all-filled form passes", presubmit_verdict(all_filled) == [])

# 4. Evidence variety is preserved through to the verdict output.
mixed = [
    {"label": "Gender", "kind": "radio", "required": True,
     "evidence": "asterisk", "aria_invalid": False, "value": "",
     "selected_label": "", "combo_text": "", "checked": False, "file_count": 0},
    {"label": "Work auth", "kind": "combobox", "required": True,
     "evidence": "aria-required", "aria_invalid": False, "value": "",
     "selected_label": "", "combo_text": "Choose one", "checked": False,
     "file_count": 0},
]
v = presubmit_verdict(mixed)
by_label = {r["label"]: r for r in v}
check("radio unchecked fails with asterisk evidence",
      "Gender" in by_label and by_label["Gender"]["evidence"] == "asterisk")
check("combobox placeholder text fails",
      "Work auth" in by_label
      and "placeholder" in by_label["Work auth"]["reason"].lower())

# 5. aria-invalid alone fails even if not marked required.
invalid_only = {
    "label": "Email", "kind": "text", "required": False,
    "evidence": "none", "aria_invalid": True, "value": "not-an-email",
    "selected_label": "", "combo_text": "", "checked": False, "file_count": 0,
}
v = presubmit_verdict([invalid_only])
check("aria-invalid field fails", len(v) == 1 and v[0]["reason"] == "aria-invalid")

# 6. A NON-required placeholder select must NOT produce a false hold.
optional_placeholder = {
    "label": "How did you hear?", "kind": "select", "required": False,
    "evidence": "none", "aria_invalid": False, "value": "none",
    "selected_label": "Select one", "combo_text": "", "checked": False,
    "file_count": 0,
}
check("optional placeholder is ignored",
      presubmit_verdict([optional_placeholder]) == [])

# 7. A filled native select (real option, non-placeholder label) passes.
filled_select = {
    "label": "State", "kind": "select", "required": True,
    "evidence": "el.required", "aria_invalid": False, "value": "WA",
    "selected_label": "Washington", "combo_text": "", "checked": False,
    "file_count": 0,
}
check("filled select passes", presubmit_verdict([filled_select]) == [])


def sel(label, value, index=0, disabled=False):
    return {"label": "State", "kind": "select", "required": True,
            "evidence": "el.required", "aria_invalid": False, "value": value,
            "selected_label": label, "selected_index": index,
            "selected_disabled": disabled, "combo_text": "", "checked": False,
            "file_count": 0}


# 8. Structural placeholder signals catch odd/non-English placeholders the
#    label regex alone would miss (finding #3/#5).
check("disabled selected option fails",
      len(presubmit_verdict([sel("Anything at all", "x", index=3,
                                 disabled=True)])) == 1)
check("sentinel value at index 0 fails ('Pick one', value none)",
      len(presubmit_verdict([sel("Pick one", "none", index=0)])) == 1)
check("sentinel value 'null' at index 0 fails",
      len(presubmit_verdict([sel("Choisir…", "null", index=0)])) == 1)

# 9. Legit first option whose value is '0' must NOT false-hold (0 excluded
#    from the sentinel set — '0-1 years' is a real answer).
check("legit index-0 value '0' passes",
      presubmit_verdict([sel("0-1 years", "0", index=0)]) == [])

# 10. Tightened regex no longer over-holds real names starting with a
#     placeholder word (finding #6). These are genuinely-selected answers.
check("real employer 'Select Portfolio Servicing' passes",
      presubmit_verdict([sel("Select Portfolio Servicing", "sps", index=5)]) == [])
check("real answer 'Choose Chicago' passes",
      presubmit_verdict([sel("Choose Chicago", "chi", index=4)]) == [])
check("combobox real name 'Please Touch Museum' passes",
      presubmit_verdict([{
          "label": "Employer", "kind": "combobox", "required": True,
          "evidence": "aria-required", "aria_invalid": False, "value": "",
          "selected_label": "", "combo_text": "Please Touch Museum",
          "checked": False, "file_count": 0}]) == [])

# 11. But genuine placeholder wordings still fail (regression guard).
for ph in ("Select a state…", "Choose one", "Please make a selection",
           "-- Select --"):
    check(f"placeholder {ph!r} still fails",
          len(presubmit_verdict([sel(ph, "none" if ph.startswith("-") else "x",
                                     index=0)])) == 1)

# 12. An EEO decline option that merely starts with '--' is a real answer,
#     not a placeholder — must pass (finding #6, safe-direction but real cost).
check("'-- (I prefer not to say)' real answer passes",
      presubmit_verdict([sel("-- (I prefer not to say)", "decline", index=2)]) == [])


def combo(text, placeholder=""):
    return {"label": "Field", "kind": "combobox", "required": True,
            "evidence": "aria-required", "aria_invalid": False, "value": "",
            "selected_label": "", "combo_text": text,
            "placeholder": placeholder, "checked": False, "file_count": 0}


# 13. Round-2 regression class: bare 'Select <noun>' / 'Choose <noun>'
#     placeholders (no article, noun outside any fixed allowlist) must fail —
#     BOTH the select and the unbackstopped combobox branch.
for ph in ("Select gender", "Choose file", "Select department", "Select year",
           "Select response", "Select ethnicity", "Select disability status",
           "Select team", "Select office", "Select role", "Select Gender",
           "Choose An Option", "select all that apply", "Select region"):
    check(f"select placeholder {ph!r} fails",
          len(presubmit_verdict([sel(ph, ph, index=0)])) == 1)
    check(f"combobox placeholder {ph!r} fails",
          len(presubmit_verdict([combo(ph)])) == 1)

# 14. Decline-style answers that START with the verb are real answers.
for real in ("Choose not to disclose", "I choose not to answer",
             "Select Portfolio Servicing", "Choose Chicago"):
    check(f"real answer {real!r} passes (select)",
          presubmit_verdict([sel(real, "v", index=3)]) == [])
    check(f"real answer {real!r} passes (combobox)",
          presubmit_verdict([combo(real)]) == [])

# 15. Structural combobox backstop: readback equal to the input's own
#     placeholder attribute is unfilled, whatever the wording.
check("combobox readback == placeholder attr fails",
      len(presubmit_verdict([combo("Start typing…", placeholder="Start typing…")])) == 1)
check("combobox readback != placeholder attr passes",
      presubmit_verdict([combo("Seattle, Washington", placeholder="Start typing…")]) == [])


if failures:
    print(f"\nFAILED: {', '.join(failures)}")
    raise SystemExit(1)
print("\nAll presubmit verdict unit tests passed.")
