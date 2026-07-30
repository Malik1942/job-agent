"""Deciding what happens to a scored, deduped job.

The autonomy dial and the live_submit switch both live here, in one small file
you can read top to bottom before trusting it with anything live.

Decision table:
  scan_only source/company      -> scan_only     (never submit, just surface it)
  score < min_score             -> skipped
  mode == review                -> needs_review  (you submit by hand, no browser opened)
  mode == auto:
     manual_submit_kinds (Ashby)-> manual_submit (NO browser; hand off the link,
                                   you submit — automation itself trips their
                                   fraud detection, so filling it is wasted+risky)
     datacenter / VPN egress IP -> needs_review  (held before submit)
     CAPTCHA seen               -> needs_review  (never solved)
     required field unanswered  -> needs_review  (held, not guessed) [hold_on_unknown_required]
     live_submit == false       -> needs_review  (dry run: filled + screenshotted)
     live_submit == true        -> applied       (submitted)

Nothing here solves CAPTCHAs, and nothing submits a form with a required field
the answers file could not cover.
"""

from __future__ import annotations

from .answers import AnswerBook
from .browser import CaptchaEncountered, fill_and_submit
from .config import Config
from .models import ApplicationRecord, Job
from .netcheck import egress_ip_reputation_cached


def submit_mode(source: str, cfg: Config) -> str:
    """How the agent handles this ATS: "auto" = it can drive + submit the form
    itself; "manual" = the platform (Ashby-type) flags automated submits as
    spam, so the agent prepares the application and a human clicks submit.

    The ATS profile is the single source of truth. load_config folds
    manual_submit_kinds into the profiles (back-compat) and validates each
    submit_mode, so here we just read it.
    """
    prof = cfg.autonomy.ats_profiles.get(source)
    return prof.submit_mode if prof else "auto"


def decide(job: Job, cfg: Config, cover_letter_path: str,
           book: AnswerBook | None = None) -> ApplicationRecord:
    base = dict(
        uid=job.uid, company=job.company, title=job.title, url=job.url,
        score=job.score, cover_letter_path=cover_letter_path,
        location=job.location, source=job.source,
    )

    if job.scan_only:
        return ApplicationRecord(
            **base, status="scan_only",
            note="surfaced for manual review; auto-submit disabled for this source/company",
        )

    if job.score < cfg.autonomy.min_score:
        return ApplicationRecord(
            **base, status="skipped",
            note=f"score {job.score} below min {cfg.autonomy.min_score}",
        )

    # Review mode: hold for you, do not even open a browser.
    if cfg.autonomy.mode != "auto":
        return ApplicationRecord(
            **base, status="needs_review",
            note="eligible to apply, held for you (review mode)",
        )

    book = book or AnswerBook({}, [])

    # Ashby-type platforms (manual_submit_kinds) run fraud detection that flags
    # the AUTOMATION ITSELF as spam — it reads the automation control channel
    # (Playwright's navigator.webdriver, or any CDP/inspector attach), not the
    # answers. Confirmed live 2026-07-06: even an EMPTY automated submit is
    # flagged, while a manual submit of the same form from the same person + IP
    # goes through clean. So we open NO browser for these — filling in an
    # automation browser is wasted (the human re-submits in their own browser)
    # and only risks the account. Hand off the apply link for a human submit.
    if submit_mode(job.source, cfg) == "manual":
        return ApplicationRecord(
            **base, status="manual_submit",
            note=(f"{job.source} flags automated submits as spam — prepared for "
                  f"manual submission. Open this in your own browser (no VPN) and "
                  f"submit; your answers are ready in answers.md: "
                  f"{job.apply_url or job.url}"),
        )

    # Auto-submittable ATS: drive the form. live_submit decides the final click.
    effective_live = cfg.autonomy.live_submit

    # Preflight: never file a REAL submit through a datacenter/VPN IP — that is
    # Greenhouse's top fraud signal. Fails open on a network hiccup (see netcheck).
    if effective_live and cfg.autonomy.block_datacenter_ip:
        verdict = egress_ip_reputation_cached()
        if not verdict.safe_to_submit:
            return ApplicationRecord(
                **base, status="needs_review",
                note=f"held before submit — {verdict.reason}",
            )
    try:
        result = fill_and_submit(
            job, cfg, book, live=effective_live,
            cover_letter_path=cover_letter_path,
        )
        note = result.note
        if result.screenshot:
            note = f"{note} | screenshot: {result.screenshot}"
        return ApplicationRecord(
            **base,
            status=result.status,
            note=note,
            screenshot=result.screenshot,
            filled_fields=result.filled,
            unanswered_required=result.unanswered_required,
        )
    except CaptchaEncountered:
        return ApplicationRecord(
            **base, status="needs_review",
            note="stopped at a CAPTCHA / bot challenge; finish this one by hand",
        )
    except Exception as exc:
        return ApplicationRecord(**base, status="error", note=f"submit failed: {exc}")
