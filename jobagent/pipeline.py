"""Orchestration: scan -> score -> (generate + apply + track)."""

from __future__ import annotations

from dataclasses import dataclass

from .answers import AnswerBook
from .apply import decide
from .config import Config
from .coverletter import write_cover_letter
from .models import Job
from .notify import Notifier
from .scoring import score_all
from .sources import fetch_source
from .tracker import Tracker


@dataclass
class ScanResult:
    jobs: list[Job]
    errors: list[str]


def _norm_company(s: str) -> str:
    return " ".join((s or "").casefold().split())


def _is_manual_only_company(job: Job, cfg: Config) -> bool:
    company = _norm_company(job.company)
    if not company:
        return False
    for name in cfg.autonomy.manual_only_companies:
        manual = _norm_company(name)
        if manual and (manual == company or manual in company):
            return True
    return False


def scan(cfg: Config) -> ScanResult:
    """Pull every configured source and score the results. No side effects,
    no applications, safe to run as often as you like."""
    all_jobs: list[Job] = []
    errors: list[str] = []

    for src in cfg.sources:
        jobs, err = fetch_source(src)
        if err:
            errors.append(err)
        # scan_only sources produce no jobs from the API here; mark any that do.
        if src.kind == "scan_only":
            for j in jobs:
                j.scan_only = True
        # Dream-company rule: surface and rank, but never auto-fill/submit.
        for j in jobs:
            if _is_manual_only_company(j, cfg):
                j.scan_only = True
        all_jobs.extend(jobs)

    scored = score_all(all_jobs, cfg)
    for job in scored:
        if _is_manual_only_company(job, cfg):
            note = "manual-only company: apply yourself"
            if note not in job.reasons:
                job.reasons.append(note)
    return ScanResult(jobs=scored, errors=errors)


def run(cfg: Config, use_llm: bool = False) -> dict:
    """Full pipeline. Respects dedupe, the autonomy dial, and live_submit."""
    result = scan(cfg)
    tracker = Tracker(cfg.db_path, cfg.csv_path, cfg.csv_statuses)

    # Load your reference answers once. Empty book is fine (fields just hold).
    book = AnswerBook.from_file(
        cfg.profile.answers_path,
        use_llm=cfg.apply.use_llm_for_answers,
        llm_settings=cfg.llm,
    )

    processed = 0
    skipped_seen = 0
    new_records = []

    # Safety cap: how many auto-submit attempts we will drive this run. Jobs are
    # sorted highest-score first, so we spend the budget on the best matches and
    # leave the rest for the next run (they stay unseen, so they get another
    # chance). 0 means no cap. The cap only bites in auto mode; review mode opens
    # no browser and submits nothing, so it is never capped.
    max_apps = cfg.autonomy.max_applications_per_run
    deferred = 0
    auto_used = 0

    for job in result.jobs:
        if tracker.seen(job.uid):
            skipped_seen += 1
            continue

        is_auto_attempt = (
            cfg.autonomy.mode == "auto"
            and not job.scan_only
            and job.score >= cfg.autonomy.min_score
        )
        # Once the per-run auto budget is spent, stop driving the browser and
        # leave the remaining (lower-scored) jobs for next time.
        if is_auto_attempt and max_apps and auto_used >= max_apps:
            # Everything already acted on this run is now recorded (seen), so the
            # unseen eligible jobs are exactly the ones we're deferring.
            deferred = sum(
                1 for j in result.jobs
                if not tracker.seen(j.uid) and not j.scan_only
                and j.score >= cfg.autonomy.min_score
            )
            break

        # Only spend effort generating a letter for jobs we might act on.
        cover_path = ""
        if job.score >= cfg.autonomy.min_score and not job.scan_only:
            cover_path = write_cover_letter(
                job, cfg, use_llm=(use_llm or cfg.llm.cover_letters)
            )

        rec = decide(job, cfg, cover_path, book=book)
        tracker.record(rec)
        new_records.append(rec)
        processed += 1
        if is_auto_attempt:
            auto_used += 1

    # Notify about genuinely new matches from this run.
    notify_notes = Notifier(cfg).notify(new_records)

    return {
        "scanned": len(result.jobs),
        "processed": processed,
        "already_seen": skipped_seen,
        "applied_this_run": auto_used,
        "deferred_over_cap": max(deferred, 0),
        "errors": result.errors,
        "status_counts": tracker.counts(),
        "notify": notify_notes,
    }
