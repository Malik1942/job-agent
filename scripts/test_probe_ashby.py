"""Unit tests for the attended Ashby probe. No browser, no network.

The central guarantee under test: in probe mode the submit click is NEVER
reached. We assert the probe only ever calls fill_and_submit with live=False
(and the real fill_and_submit returns in its `if not live:` branch, before the
only submit-click block — so no automated submit is possible). Plus the verdict
logic, the undetected-custom-widget detection, and report writing.

Run from the repo root:
    .venv/bin/python scripts/test_probe_ashby.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobagent import browser, coverletter, probe          # noqa: E402
from jobagent.browser import SubmitResult                 # noqa: E402
from jobagent.config import load_config                   # noqa: E402
from jobagent.models import Job                            # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"[{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        failures.append(name)


TRUE_FIELDS = [
    {"section": "Details", "label": "Full Name", "atype": "String",
     "required": True, "options": []},
    {"section": "Questions", "label": "How many years of relevant professional "
     "experience do you have?", "atype": "ValueSelect", "required": True,
     "options": ["1-2 years", "3-5 years"]},
]
SCANNED = [{"label": "Full Name", "kind": "text"},
           {"label": "Email", "kind": "text"}]  # NOTE: no years ValueSelect


class FakePage:
    url = "https://jobs.ashbyhq.com/replit/2f1de5f1-48c2-4104-a697-a51fe1620508/x"

    def __init__(self, post_errors):
        self._post_errors = post_errors

    def evaluate(self, js):
        if "navigator.webdriver" in js:
            return True
        if "data-ja-idx" in js:              # _EXTRACT_JS
            return SCANNED
        if "g-recaptcha" in js or "hcaptcha" in js:   # _CAPTCHA_JS
            return False
        if "confirmPhrases" in js:           # _SUBMIT_VERIFY_JS
            return {"confirmed": False, "errors": self._post_errors,
                    "form_present": True}
        return None

    def screenshot(self, **k):
        raise RuntimeError("no real browser in the test")


def run_probe(ask_responses, post_errors=None, submit_click_tripwire=False):
    """Drive probe_ashby with everything network/browser mocked. Returns
    (verdict, report_text, fill_call)."""
    fill_call = {}

    def fake_fill_and_submit(job, cfg, book, live, cover_letter_path="",
                             attended_handoff=None):
        fill_call["live"] = live
        fill_call["has_handoff"] = attended_handoff is not None
        # THE GUARANTEE: probe must never ask for a live submit.
        assert live is False, "probe requested a LIVE submit — must never happen"
        if submit_click_tripwire and live:
            raise AssertionError("submit path reached in probe mode")
        # simulate: fill + validation done, page open, NOT submitted
        page = FakePage(post_errors or [])
        attended_handoff(page, {
            "filled": ["Full Name = Alex Rivera"],
            "presubmit_fail": [], "presubmit_notes": [],
            "hold_notes": ["How many years… (fill yourself)"],
            "should_hold": True, "shot_path": "fill.png",
        })
        return SubmitResult("needs_review", note="[probe] hand-off complete")

    it = iter(ask_responses)
    ask = lambda prompt="": next(it, "aborted")  # noqa: E731

    real_fill = browser.fill_and_submit
    real_build = probe._build_job
    real_true = probe._true_fields
    real_cover = coverletter.write_cover_letter
    browser.fill_and_submit = fake_fill_and_submit
    probe._build_job = lambda url: Job(source="ashby", company="Replit",
        title="Design Engineer", url=url, apply_url=url, external_id="2f1de5f1")
    probe._true_fields = lambda url: TRUE_FIELDS
    coverletter.write_cover_letter = lambda job, cfg, **k: ""
    try:
        cfg = load_config("config.example.yaml")
        with tempfile.TemporaryDirectory() as td:
            cfg.output_dir = td
            cfg.apply.screenshot_dir = str(Path(td) / "shots")
            verdict = probe.probe_ashby(
                cfg, "https://jobs.ashbyhq.com/replit/"
                "2f1de5f1-48c2-4104-a697-a51fe1620508/application", ask=ask)
            report = (Path(td) / "ashby_probe_report.md").read_text()
    finally:
        browser.fill_and_submit = real_fill
        probe._build_job = real_build
        probe._true_fields = real_true
        coverletter.write_cover_letter = real_cover
    return verdict, report, fill_call


# 1. THE GUARANTEE: probe calls fill_and_submit with live=False + a hand-off.
verdict, report, call = run_probe(["", "aborted"])
check("probe calls fill_and_submit with live=False (never a live submit)",
      call.get("live") is False)
check("probe passes an attended hand-off callback", call.get("has_handoff") is True)

# 2. aborted → aborted verdict, zero submission.
check("aborted outcome -> verdict aborted", verdict == "aborted")

# 3. clean human submit, no post-block → clean_manual_submit.
verdict, report, _ = run_probe(["", "submitted_ok"], post_errors=[])
check("submitted_ok + no post-block -> clean_manual_submit",
      verdict == "clean_manual_submit")

# 4. human reports a challenge on submit → blocked_on_submit.
verdict, _, _ = run_probe(["", "challenge_appeared_on_submit"])
check("challenge_appeared_on_submit -> blocked_on_submit",
      verdict == "blocked_on_submit")

# 5. human says ok but a spam marker is detected post-click → blocked_on_submit.
verdict, _, _ = run_probe(["", "submitted_ok"],
                          post_errors=["Your application was flagged as possible spam"])
check("post-click spam marker -> blocked_on_submit (observation overrides)",
      verdict == "blocked_on_submit")

# 6. report content: config logged, undetected custom widget flagged, verdict.
_, report, _ = run_probe(["", "aborted"])
check("report logs the no-stealth config (webdriver observed True)",
      "navigator.webdriver observed:** True" in report
      and "stealth:** NONE" in report)
check("report flags the ValueSelect widget the DOM scan missed",
      "How many years" in report and "MISSED" in report)
check("report ends with a verdict + meaning", "## Verdict:" in report)

# 7. verdict pure-function mapping.
check("_verdict blocked_preload wins",
      probe._verdict({"preload": "blocked_preload"}) == "blocked_preload")
check("_verdict aborted",
      probe._verdict({"preload": "none", "reported_outcome": "aborted"}) == "aborted")

# 8. undetected-widget detection is fuzzy but correct.
und = probe._undetected_widgets(TRUE_FIELDS, SCANNED)
check("undetected widgets = the required ValueSelect only",
      len(und) == 1 and und[0]["atype"] == "ValueSelect")
und2 = probe._undetected_widgets(
    [{"label": "Full Name", "atype": "String", "required": True}],
    [{"label": "Full Name"}])
check("a detected required field is NOT flagged undetected", und2 == [])


if failures:
    print(f"\nFAILED: {', '.join(failures)}")
    raise SystemExit(1)
print("\nAll probe-ashby tests passed (submit click never reached in probe mode).")
