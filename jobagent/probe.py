"""Attended, single-shot Ashby diagnostic probe.

Runs the REAL fill + Phase-1 validation pipeline on one Ashby posting up to (but
NEVER through) the submit click, in HEADED mode under the CURRENT honest config
— no stealth: navigator.webdriver stays true, plain launch, honest locale/tz
only. Then it hands the open window to the human, who inspects and optionally
clicks submit themselves. The probe records whether a block appears before or
after that human click and writes a diagnostic report.

Why this is safe:
  - It calls browser.fill_and_submit(..., live=False, attended_handoff=...).
    With live=False the function returns in its `if not live:` branch, BEFORE
    the submit-click block — so the bot never auto-submits. The only submit
    click in the codebase is guarded by `live` being True.
  - No stealth is added (this tests the current config, logged truthfully as
    such — it is NOT "Phase 1.6", which does not exist).
  - The bot never touches a CAPTCHA/challenge; it observes and classifies only.
    Only the human interacts with the form.

Reuses verbatim: the normal DOM-scan filler + Phase-1 pre-submit validation +
per-field logging (all inside fill_and_submit), the _EXTRACT_JS field scan, the
_CAPTCHA_JS block detector, and _SUBMIT_VERIFY_JS for post-click block markers.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from .answers import AnswerBook
from .config import Config
from .models import Job

logger = logging.getLogger(__name__)

_ASHBY_URL_RE = re.compile(
    r"ashbyhq\.com/([^/?#]+)/([0-9a-fA-F-]{36})", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).strip()


def _build_job(url: str) -> Job:
    """A Job for the probe from a single Ashby application URL. Title is
    fetched from Ashby's public API when reachable (best-effort)."""
    m = _ASHBY_URL_RE.search(url or "")
    org = m.group(1) if m else ""
    jid = m.group(2) if m else ""
    title, company = "Ashby posting", org or "Ashby"
    try:
        from .ashby_form import fetch_application_questions  # noqa: F401
        import requests
        q = ("query P($org:String!,$jid:String!){jobPosting("
             "organizationHostedJobsPageName:$org,jobPostingId:$jid){title}}")
        r = requests.post(
            "https://jobs.ashbyhq.com/api/non-user-graphql?op=P",
            json={"query": q, "variables": {"org": org, "jid": jid}},
            headers={"Content-Type": "application/json"}, timeout=15)
        title = (((r.json().get("data") or {}).get("jobPosting") or {})
                 .get("title") or title)
    except Exception as exc:
        logger.warning("could not fetch Ashby title: %s", exc)
    return Job(source="ashby", company=company.title(), title=title,
               url=url, apply_url=url, external_id=jid)


def _true_fields(url: str) -> list[dict]:
    m = _ASHBY_URL_RE.search(url or "")
    if not m:
        return []
    try:
        from .ashby_form import fetch_application_questions
        return fetch_application_questions(m.group(1), m.group(2))
    except Exception as exc:
        logger.warning("could not fetch Ashby form fields: %s", exc)
        return []


def _undetected_widgets(true_fields: list[dict], scanned: list[dict]) -> list[dict]:
    """Required fields Ashby's API lists that the DOM scan did NOT surface —
    the known custom-widget (ValueSelect/radio) gap. Matched by fuzzy label."""
    scanned_labels = [_norm(f.get("label") or "") for f in scanned]

    def seen(label: str) -> bool:
        n = _norm(label)
        return any(n and (n == s or n in s or s in n
                          or SequenceMatcher(None, n, s).ratio() >= 0.8)
                   for s in scanned_labels if s)

    return [q for q in true_fields
            if q.get("required") and not seen(q.get("label") or "")]


def probe_ashby(cfg: Config, url: str, ask=input) -> str:
    """Run the attended probe. Returns the one-line verdict and writes a
    markdown report to output/. `ask` is injected for testing."""
    from . import browser  # late import: keeps playwright optional

    if not _ASHBY_URL_RE.search(url or ""):
        raise ValueError(f"not an Ashby application URL: {url!r}")

    job = _build_job(url)
    book = AnswerBook.from_file(cfg.profile.answers_path,
                               use_llm=cfg.apply.use_llm_for_answers,
                               llm_settings=cfg.llm)

    # Force HEADED so the human can see + click. This is the current config,
    # no stealth — logged truthfully below.
    cfg.apply.headless = False
    from .browser import _context_kwargs
    ctx = _context_kwargs(cfg)
    config_log = {
        "headed": True,
        "launch": "pw.chromium.launch(headless=False) — plain, no persistent "
                  "profile, no stealth args",
        "context": ctx or "(defaults)",
        "stealth": "NONE — navigator.webdriver stays true (this is the current "
                   "config, NOT the non-existent 'Phase 1.6')",
        "batch_pacing_s": (cfg.autonomy.batch_pause_min_seconds,
                           cfg.autonomy.batch_pause_max_seconds),
    }
    logger.info("probe config in effect: %s", config_log)

    true_fields = _true_fields(url)
    report: dict = {"url": url, "job": job, "config": config_log,
                    "true_fields": true_fields, "preload": "none",
                    "captcha_preload": False}

    shot_dir = Path(cfg.apply.screenshot_dir)
    shot_dir.mkdir(parents=True, exist_ok=True)
    post_shot = str(shot_dir / "_probe_post_click.png")

    def handoff(page, data: dict) -> None:
        """Runs with the OPEN, filled page. Bot does NOT submit here."""
        report["webdriver"] = page.evaluate("() => navigator.webdriver")
        try:
            scanned = page.evaluate(browser._EXTRACT_JS)
        except Exception:
            scanned = []
        report["scanned_count"] = len(scanned)
        report["undetected"] = _undetected_widgets(true_fields, scanned)
        report["filled"] = data["filled"]
        report["hold_notes"] = data["hold_notes"]
        report["validation"] = ("WOULD HOLD" if data["should_hold"]
                                else "WOULD PROCEED to submit")
        report["fill_shot"] = data["shot_path"]

        print("\n" + "=" * 68)
        print("ATTENDED ASHBY PROBE — bot has FILLED the form and STOPPED.")
        print("The bot will NOT click submit. You decide.")
        print("=" * 68)
        print(f"Config: headed, NO stealth (navigator.webdriver = "
              f"{report['webdriver']}).")
        print(f"Filled {len(data['filled'])} field(s). Pre-submit validation: "
              f"{report['validation']}.")
        if data["hold_notes"]:
            print("Validation flagged (would hold in live mode):")
            for h in data["hold_notes"][:8]:
                print(f"  - {h}")
        if report["undetected"]:
            print(f"\nDOM scan MISSED {len(report['undetected'])} required "
                  f"field(s) Ashby's API lists (custom widgets):")
            for q in report["undetected"]:
                print(f"  - {q['label']}  <{q['atype']}>")
        print("\nThe headed browser window is open. Inspect it. If YOU choose "
              "to, complete any gaps and click Submit YOURSELF.")
        ask("Press Enter here AFTER you've inspected / optionally submitted... ")

        outcome = ""
        while outcome not in ("submitted_ok", "submitted_then_blocked",
                              "challenge_appeared_on_submit", "aborted"):
            outcome = (ask("Type the outcome [submitted_ok / "
                           "submitted_then_blocked / challenge_appeared_on_submit"
                           " / aborted]: ") or "").strip()
        report["reported_outcome"] = outcome

        # Post-click observation (classify only — never interact with a block).
        report["post_url"] = page.url
        try:
            captcha = bool(page.evaluate(browser._CAPTCHA_JS))
        except Exception:
            captcha = False
        try:
            verify = page.evaluate(browser._SUBMIT_VERIFY_JS)
            errors = verify.get("errors") or []
        except Exception:
            errors = []
        spam = any(re.search(r"spam|flagged|couldn.?t submit|something went wrong",
                             e, re.I) for e in errors)
        report["post_errors"] = errors[:6]
        report["post_block"] = ("blocked_on_submit"
                                if (captcha or spam) else "none")
        browser._safe_screenshot(page, post_shot)
        report["post_shot"] = post_shot

    # Run the real pipeline in DRY-RUN (never submits) with the hand-off hook.
    cover_path = ""
    try:
        from .coverletter import write_cover_letter
        cover_path = write_cover_letter(job, cfg)
    except Exception:
        cover_path = ""

    try:
        browser.fill_and_submit(job, cfg, book, live=False,
                                cover_letter_path=cover_path,
                                attended_handoff=handoff)
    except browser.CaptchaEncountered:
        report["preload"] = "blocked_preload"
        report["captcha_preload"] = True
        report["reported_outcome"] = "n/a (blocked before fill)"

    verdict = _verdict(report)
    report["verdict"] = verdict
    path = _write_report(cfg, report)
    report["report_path"] = path
    print(f"\nVERDICT: {verdict}")
    print(f"Report: {path}")
    return verdict


def _verdict(report: dict) -> str:
    if report.get("preload") == "blocked_preload":
        return "blocked_preload"
    outcome = report.get("reported_outcome", "aborted")
    if outcome == "aborted":
        return "aborted"
    if report.get("post_block") == "blocked_on_submit":
        return "blocked_on_submit"
    if outcome in ("submitted_then_blocked", "challenge_appeared_on_submit"):
        return "blocked_on_submit"
    if outcome == "submitted_ok":
        return "clean_manual_submit"
    return "aborted"


_VERDICT_MEANING = {
    "blocked_preload": "A challenge appeared before the form even loaded. Ashby "
        "blocks this browser identity at the door — manual-only (own browser) "
        "stays; the bot's browser can't even be used for a human hand-off here.",
    "blocked_on_submit": "The form filled fine, but the human's own click hit a "
        "block/challenge/spam flag. The browser IDENTITY (webdriver=true, no "
        "stealth) is the problem, not who clicks — so a headed bot-fills / "
        "human-clicks flow does NOT help. Keep the crib-sheet (own browser).",
    "clean_manual_submit": "The human's click in the bot's headed (non-stealth) "
        "browser sailed through, no block. Ashby does NOT flag a human-completed "
        "submit even in the automation browser — so we COULD move Ashby to "
        "'bot fills headed, human clicks in the same window' instead of the "
        "separate crib-sheet. Worth confirming across 2-3 postings before "
        "changing the routing.",
    "aborted": "You chose not to submit. Zero submission occurred. No conclusion "
        "about the block — rerun when you want to test the click.",
}


def _write_report(cfg: Config, r: dict) -> str:
    job = r["job"]
    lines = [f"# Ashby attended probe — {job.company} / {job.title}",
             f"\n_Generated {datetime.now():%Y-%m-%d %H:%M}. Attended, single-shot,"
             " NO auto-submit. Tests the CURRENT config (no stealth) — not the "
             "non-existent 'Phase 1.6'._\n",
             f"- **URL:** {r['url']}",
             "\n## Config tested (logged verbatim)"]
    for k, v in r["config"].items():
        lines.append(f"- **{k}:** {v}")
    lines.append(f"- **navigator.webdriver observed:** {r.get('webdriver', 'n/a')}")

    lines.append("\n## Preload block detection (_CAPTCHA_JS)")
    lines.append(f"- Result: **{r['preload']}** "
                 + ("(a visible challenge appeared before fill)"
                    if r["captcha_preload"] else
                    "(no VISIBLE challenge — note: reCAPTCHA v3 is invisible/"
                    "score-based, so 'none' here does not mean 'no anti-bot')"))

    if not r["captcha_preload"]:
        lines.append("\n## Fill summary")
        lines.append(f"- Filled **{len(r.get('filled', []))}** field(s); DOM scan "
                     f"saw {r.get('scanned_count', 0)} control(s).")
        und = r.get("undetected") or []
        if und:
            lines.append(f"- ⚠ **{len(und)} required field(s) the DOM scan MISSED** "
                         "(custom widgets Ashby's API lists):")
            for q in und:
                lines.append(f"  - {q['label']}  `<{q['atype']}>`")
        else:
            lines.append("- DOM scan detected every required field Ashby's API lists.")
        lines.append(f"\n## Pre-submit validation (Phase 1)")
        lines.append(f"- Verdict: **{r.get('validation')}**")
        for h in (r.get("hold_notes") or [])[:8]:
            lines.append(f"  - {h}")

        lines.append("\n## Your reported outcome + post-click observation")
        lines.append(f"- You reported: **{r.get('reported_outcome')}**")
        lines.append(f"- Post-click URL: {r.get('post_url', 'n/a')}")
        lines.append(f"- Post-click block classification: "
                     f"**{r.get('post_block', 'n/a')}**")
        for e in (r.get("post_errors") or []):
            lines.append(f"  - marker: {e}")

    lines.append("\n## Screenshots")
    lines.append(f"- Filled form: `{r.get('fill_shot', 'n/a')}`")
    lines.append(f"- Post-click: `{r.get('post_shot', 'n/a')}`")

    lines.append(f"\n## Verdict: **{r['verdict']}**")
    lines.append(_VERDICT_MEANING.get(r["verdict"], ""))

    out = Path(cfg.output_dir) / "ashby_probe_report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(out)
