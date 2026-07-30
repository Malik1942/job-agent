"""Slack button approvals for the review queue.

Run this on the same machine as jobagent. It uses Slack Socket Mode, so it does
not need a public HTTPS callback URL. Slack sends button clicks over an
outbound WebSocket connection initiated by this process.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from .answers import AnswerBook
from .apply import decide
from .config import Config
from .coverletter import (
    ensure_cover_letter_pdf,
    validate_cover_letter_package,
    write_cover_letter,
)
from .models import ApplicationRecord, Job
from .tracker import Tracker


@dataclass
class QueueLookup:
    path: Path
    payload: dict[str, Any]
    item: dict[str, Any]


def latest_queue_path(cfg: Config) -> Path:
    root = Path(cfg.output_dir) / "review_queues"
    matches = sorted(
        p for p in root.glob("job-review-queue-*.json")
        if not p.name.endswith(".stop.json")
    )
    if not matches:
        raise FileNotFoundError(f"no review queue files in {root}")
    return matches[-1]


def _queue_arg(value: str, cfg: Config, fallback: str | Path | None = None) -> Path:
    try:
        parsed = json.loads(value) if value else {}
    except json.JSONDecodeError:
        parsed = {"id": value}
    queue = parsed.get("queue") if isinstance(parsed, dict) else ""
    if queue and queue != "latest":
        return Path(queue)
    if fallback:
        return Path(fallback)
    return latest_queue_path(cfg)


def _item_arg(value: str) -> str:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    if isinstance(parsed, dict) and parsed.get("id"):
        return str(parsed["id"])
    return value


def load_queue_item(queue_path: str | Path, item_id: str) -> QueueLookup:
    path = Path(queue_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    for item in payload.get("queue", []):
        if item.get("id") == item_id:
            return QueueLookup(path=path, payload=payload, item=item)
    raise KeyError(f"{item_id} not found in {path}")


def save_queue_item(lookup: QueueLookup) -> None:
    lookup.path.write_text(
        json.dumps(lookup.payload, indent=2),
        encoding="utf-8",
    )


def pending_queue_item_ids(queue_path: str | Path) -> list[str]:
    path = Path(queue_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        item["id"] for item in payload.get("queue", [])
        if item.get("id") and item.get("status") == "pending_confirmation"
    ]


def _job_from_item(item: dict[str, Any]) -> Job:
    return Job(
        source=item.get("source", "slack_review"),
        company=item.get("company", ""),
        title=item.get("title", ""),
        url=item.get("url", ""),
        location=item.get("location", ""),
        apply_url=item.get("apply_url", "") or item.get("url", ""),
        scan_only=False,
        score=float(item.get("score", 0.0)),
        reasons=list(item.get("reasons") or []),
        external_id=item.get("external_id", ""),
    )


# Text fragments that reliably indicate a posting has closed. Deliberately
# conservative: unknown pages are treated as live so we never skip a real one.
_CLOSED_MARKERS = (
    "no longer accepting applications",
    "this job is no longer open",
    "job you're looking for is no longer open",
    "job you are looking for is no longer open",
    "this position is no longer open",
    "position has been filled",
    "sorry, we couldn't find anything here",
    "job not found",
    "posting not found",
    "this job posting has closed",
)


def check_posting_live(url: str, timeout: int = 15) -> tuple[bool, str]:
    """Cheap pre-fill liveness check. Returns (is_live, note).

    Only strong signals mark a posting closed: an HTTP error status or a
    well-known 'closed' phrase in the page body. Network trouble counts as
    live — the browser step will surface a real error if there is one.
    """
    if not url:
        return True, "no url to check"
    try:
        resp = requests.get(
            url, timeout=timeout, allow_redirects=True,
            headers={"User-Agent": "jobagent/0.1 (posting liveness check)"},
        )
    except requests.RequestException as exc:
        return True, f"liveness check inconclusive ({exc})"
    if resp.status_code >= 400:
        return False, f"posting returned HTTP {resp.status_code}"
    body = resp.text[:200000].lower()
    for marker in _CLOSED_MARKERS:
        if marker in body:
            return False, f"posting page says: {marker!r}"
    return True, "posting looks live"


def _batch_stop_path(queue_path: str | Path) -> Path:
    path = Path(queue_path)
    return path.with_name(f"{path.stem}.stop.json")


def clear_batch_stop(queue_path: str | Path) -> None:
    try:
        _batch_stop_path(queue_path).unlink()
    except FileNotFoundError:
        pass


def request_batch_stop(queue_path: str | Path, reason: str = "user") -> str:
    path = _batch_stop_path(queue_path)
    path.write_text(json.dumps({
        "requested_at": datetime.now().isoformat(timespec="seconds"),
        "reason": reason,
    }, indent=2), encoding="utf-8")
    return f"Stop requested for `{Path(queue_path)}`."


def batch_stop_requested(queue_path: str | Path) -> bool:
    return _batch_stop_path(queue_path).exists()


def _batch_pause(cfg: Config, position: int, total: int,
                 queue_path: str | Path | None = None) -> bool:
    """Human-ish jittered pause between batch browser runs. Returns false if
    a stop request arrives while waiting."""
    import random
    import time
    if position >= total - 1:
        return True
    lo = max(0, int(cfg.autonomy.batch_pause_min_seconds))
    hi = max(lo, int(cfg.autonomy.batch_pause_max_seconds))
    if hi <= 0:
        return True
    pause = random.uniform(lo, hi)
    print(f"pacing: sleeping {pause:.0f}s before next submission "
          f"({position + 1}/{total} done)")
    deadline = time.monotonic() + pause
    while True:
        if queue_path is not None and batch_stop_requested(queue_path):
            print("batch stop requested during pacing pause")
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        time.sleep(min(1.0, remaining))


def _run_queue_item(cfg: Config, queue_path: str | Path, item_id: str,
                    allowed_statuses: set[str],
                    live_override: bool | None,
                    next_status_on_dry: str | None = None,
                    precheck: bool = False) -> ApplicationRecord:
    """Shared engine for phase-1 (prepare) and phase-2/single-phase (submit).

    live_override=False forces a dry-run fill (phase 1); None honors the
    queue's creation-time live_submit stamp. next_status_on_dry lets phase 1
    park the item as pending_submit instead of the record's needs_review.
    """
    lookup = load_queue_item(queue_path, item_id)
    status = lookup.item.get("status", "pending_confirmation")
    if status not in allowed_statuses:
        raise RuntimeError(f"{item_id} is already {status}")

    job = _job_from_item(lookup.item)

    if precheck:
        live_posting, note = check_posting_live(job.apply_url or job.url)
        if not live_posting:
            lookup.item["status"] = "closed_before_submit"
            lookup.item["decided_at"] = datetime.now().isoformat(
                timespec="seconds")
            lookup.item["result_note"] = note
            save_queue_item(lookup)
            rec = ApplicationRecord(
                uid=job.uid, company=job.company, title=job.title,
                url=job.url, score=job.score, status="closed_before_submit",
                note=note, location=job.location, source=job.source,
            )
            Tracker(cfg.db_path, cfg.csv_path, cfg.csv_statuses).record(rec)
            return rec

    original_mode = cfg.autonomy.mode
    original_live_submit = cfg.autonomy.live_submit
    cfg.autonomy.mode = "auto"  # the approval button is the human gate.
    if live_override is not None:
        cfg.autonomy.live_submit = live_override
    else:
        # Honor the queue's creation-time safety switch. Old dry-run queues
        # stay dry-run even if config.yaml is later changed to live submit.
        cfg.autonomy.live_submit = bool(
            lookup.payload.get("live_submit", cfg.autonomy.live_submit)
        )

    book = AnswerBook.from_file(
        cfg.profile.answers_path,
        use_llm=cfg.apply.use_llm_for_answers,
        llm_settings=cfg.llm,
    )
    if status == "pending_submit" and not lookup.item.get("cover_letter_path"):
        raise RuntimeError(
            "cover letter was not prepared/reviewed; run approve/fill first"
        )
    if live_override is False or not lookup.item.get("cover_letter_path"):
        # Every prepare pass rebuilds the role-specific package so stale
        # generic letters cannot be reused after prompt/workflow updates.
        cover_path = write_cover_letter(
            job,
            cfg,
            use_llm=cfg.llm.cover_letters,
        )
    else:
        cover_path = lookup.item.get("cover_letter_path") or write_cover_letter(
            job,
            cfg,
            use_llm=cfg.llm.cover_letters,
        )
    cover_pdf_path = ensure_cover_letter_pdf(job, cfg, cover_path)
    cover_errors = validate_cover_letter_package(
        job, cfg, cover_path, cover_pdf_path
    )
    if cover_errors:
        raise RuntimeError(
            "cover letter package mismatch: " + "; ".join(cover_errors)
        )
    try:
        rec = decide(job, cfg, cover_path, book=book)
    finally:
        cfg.autonomy.mode = original_mode
        cfg.autonomy.live_submit = original_live_submit
    Tracker(cfg.db_path, cfg.csv_path, cfg.csv_statuses).record(rec)

    item_status = rec.status
    if (next_status_on_dry and rec.status == "needs_review"
            and not rec.unanswered_required):
        # Phase-1 fill went clean: park it awaiting explicit confirmation.
        item_status = next_status_on_dry
    lookup.item["status"] = item_status
    lookup.item["decided_at"] = datetime.now().isoformat(timespec="seconds")
    lookup.item["cover_letter_path"] = cover_path
    lookup.item["cover_letter_pdf_path"] = cover_pdf_path
    lookup.item["result_note"] = rec.note
    lookup.item["screenshot"] = rec.screenshot
    lookup.item["filled_fields"] = rec.filled_fields
    lookup.item["unanswered_required"] = rec.unanswered_required
    save_queue_item(lookup)
    return rec


def prepare_queue_item(cfg: Config, queue_path: str | Path,
                       item_id: str) -> ApplicationRecord:
    """Phase 1: fill the form and screenshot it, never submit. A clean fill
    parks the item as pending_submit; a fill with unanswered required fields
    stays needs_review (finish those by hand or fix answers.md)."""
    return _run_queue_item(
        cfg, queue_path, item_id,
        allowed_statuses={"pending_confirmation", "error"},
        live_override=False,
        next_status_on_dry="pending_submit",
        precheck=True,
    )


def confirm_queue_item(cfg: Config, queue_path: str | Path,
                       item_id: str) -> ApplicationRecord:
    """Phase 2: actually submit an item that passed phase 1. Honors the
    queue's creation-time live_submit stamp."""
    return _run_queue_item(
        cfg, queue_path, item_id,
        allowed_statuses={"pending_submit", "error"},
        live_override=None,
        precheck=True,
    )


def approve_queue_item(cfg: Config, queue_path: str | Path,
                       item_id: str) -> ApplicationRecord:
    """Single-phase approve (two_phase_approval: false): fill and submit in
    one step, honoring the queue's live_submit stamp."""
    return _run_queue_item(
        cfg, queue_path, item_id,
        allowed_statuses={"pending_confirmation", "error"},
        live_override=None,
        precheck=True,
    )


def _batch(cfg: Config, queue_path: str | Path, item_ids: list[str],
           fn) -> tuple[list[ApplicationRecord], list[str]]:
    clear_batch_stop(queue_path)
    records: list[ApplicationRecord] = []
    errors: list[str] = []
    for pos, item_id in enumerate(item_ids):
        if batch_stop_requested(queue_path):
            left = len(item_ids) - pos
            errors.append(
                f"batch stopped by user before {item_id}; "
                f"{left} item(s) left pending"
            )
            break
        try:
            records.append(fn(cfg, queue_path, item_id))
        except Exception as exc:
            errors.append(f"{item_id}: {exc}")
            try:
                lookup = load_queue_item(queue_path, item_id)
                lookup.item["status"] = "error"
                lookup.item["decided_at"] = datetime.now().isoformat(
                    timespec="seconds")
                lookup.item["result_note"] = str(exc)
                save_queue_item(lookup)
            except Exception:
                pass
        if batch_stop_requested(queue_path):
            left = len(item_ids) - pos - 1
            if left:
                errors.append(
                    f"batch stopped by user after {item_id}; "
                    f"{left} item(s) left pending"
                )
            break
        if not _batch_pause(cfg, pos, len(item_ids), queue_path):
            left = len(item_ids) - pos - 1
            if left:
                errors.append(
                    f"batch stopped by user after {item_id}; "
                    f"{left} item(s) left pending"
                )
            break
    return records, errors


def approve_all_queue_items(cfg: Config, queue_path: str | Path) -> tuple[
    list[ApplicationRecord], list[str]
]:
    """Single-phase batch approve, with pacing between submissions."""
    path = Path(queue_path)
    return _batch(cfg, path, pending_queue_item_ids(path), approve_queue_item)


def prepare_all_queue_items(cfg: Config, queue_path: str | Path) -> tuple[
    list[ApplicationRecord], list[str]
]:
    """Phase-1 batch: fill + screenshot every pending item, submit nothing."""
    path = Path(queue_path)
    return _batch(cfg, path, pending_queue_item_ids(path), prepare_queue_item)


def pending_submit_item_ids(queue_path: str | Path) -> list[str]:
    path = Path(queue_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        item["id"] for item in payload.get("queue", [])
        if item.get("id") and item.get("status") == "pending_submit"
    ]


def confirm_all_queue_items(cfg: Config, queue_path: str | Path) -> tuple[
    list[ApplicationRecord], list[str]
]:
    """Phase-2 batch: submit everything parked as pending_submit, with
    pacing between submissions."""
    path = Path(queue_path)
    return _batch(cfg, path, pending_submit_item_ids(path), confirm_queue_item)


def _line_chunks(text: str, max_chars: int = 35000) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        while len(line) > max_chars:
            head, line = line[:max_chars], line[max_chars:]
            if current:
                chunks.append("\n".join(current))
                current, size = [], 0
            chunks.append(head)
        add = len(line) + 1
        if current and size + add > max_chars:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += add
    if current:
        chunks.append("\n".join(current))
    return chunks


def _reply_chunks(reply: str, max_chars: int = 3500) -> list[str]:
    """Split a slash reply for Slack. A reply that is one fenced code block
    (holds/coverage/scan) is re-fenced per chunk so a long block never turns
    into broken ``` fragments; anything else chunks as plain lines."""
    fence = "```"
    if reply.startswith(fence + "\n") and reply.endswith("\n" + fence):
        inner = reply[len(fence) + 1:-(len(fence) + 1)]
        pad = 2 * (len(fence) + 1)  # opening ```\n + closing \n```
        return [f"{fence}\n{c}\n{fence}"
                for c in _line_chunks(inner, max_chars - pad)]
    return _line_chunks(reply, max_chars)


def _slash_ack_text(verb: str) -> str:
    """Immediate ack for a slash verb. Slow verbs get an honest time hint so
    silence during the work does not read as a hang."""
    hints = {
        "scan": "jobagent: scanning 14 boards, ~1-2 min...",
        "checklist": "jobagent: building the manual-submit checklist...",
    }
    hints["ashby"] = hints["checklist"]  # alias shares the checklist ack
    if verb in hints:
        return hints[verb]
    if verb in {"help", ""}:
        return "jobagent help:"
    return f"jobagent: running `{verb}`..."


def _screenshot_from_item(item: dict[str, Any]) -> str:
    if item.get("screenshot"):
        return str(item["screenshot"])
    note = str(item.get("result_note") or "")
    match = re.search(r"screenshot:\s*([^|]+)", note)
    return match.group(1).strip() if match else ""


def _cover_letter_text(item: dict[str, Any]) -> str:
    path = str(item.get("cover_letter_path") or "")
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _cover_letter_pdf(item: dict[str, Any]) -> str:
    path = str(item.get("cover_letter_pdf_path") or "")
    return path if path and Path(path).exists() else path


def _filled_value(filled: list[str], needles: tuple[str, ...]) -> str:
    for line in filled:
        low = line.lower()
        if any(n in low for n in needles):
            return line
    return ""


def _open_ended_lines(filled: list[str]) -> list[str]:
    markers = (
        "[question]", "[cover_letter]", "why do you", "additional information",
        "tell us", "describe", "projects/work", "portfolio", "anything else",
    )
    out: list[str] = []
    for line in filled:
        low = line.lower()
        if any(m in low for m in markers):
            out.append(line)
    return out[:5]


def _outcome_banner(item: dict[str, Any]) -> str:
    """One unambiguous line stating whether this item actually submitted.
    This is the answer to 'did it go through or not?' — everything else in
    the record is supporting detail."""
    status = str(item.get("status") or "unknown")
    note = str(item.get("result_note") or "")
    if status == "applied":
        return ":white_check_mark: *SUBMITTED* — the page confirmed it."
    if status == "submitted_unverified":
        return (":warning: *LIKELY SUBMITTED, NOT CONFIRMED* — the form closed "
                "with no error but no confirmation text either. Verify by email.")
    if status == "error":
        return ":octagonal_sign: *ERROR* — nothing was submitted."
    if status in {"closed_before_submit"}:
        return ":no_entry: *POSTING CLOSED* — not submitted."
    if status in {"skipped_by_user", "skipped"}:
        return ":fast_forward: *SKIPPED* — not submitted."
    if status in {"pending_submit"}:
        return (":hourglass_flowing_sand: *FILLED, AWAITING CONFIRM* — reviewed "
                "but not submitted yet. Tap *Confirm submit* to send it.")
    if status == "pending_confirmation":
        return (":inbox_tray: *NOT SUBMITTED YET* — waiting for your Approve "
                "to fill the form.")
    if status == "manual_submit":
        return (":writing_hand: *FILLED — SUBMIT YOURSELF* — this ATS flags "
                "automated submits as spam, so the form is filled + "
                "screenshotted; open the apply link and click submit in your "
                "own browser.")
    if status == "needs_review":
        if "REJECTED" in note or "submit blocked" in note or item.get(
                "unanswered_required"):
            return (":x: *NOT SUBMITTED* — the page rejected it or a required "
                    "field is unanswered (see below).")
        return ":pause_button: *HELD* — needs your input before it can go."
    return f":grey_question: *{status}*"


def _record_lines(item: dict[str, Any]) -> list[str]:
    filled = list(item.get("filled_fields") or [])
    unanswered = list(item.get("unanswered_required") or [])
    reasons = "; ".join(item.get("reasons") or [])
    screenshot = _screenshot_from_item(item)
    cover_text = _cover_letter_text(item)
    cover_pdf = _cover_letter_pdf(item)
    resume = _filled_value(filled, ("resume", "cv"))
    cover_upload = _filled_value(filled, ("cover letter (file:", "cover letter*", "cover letter"))
    pronunciation = _filled_value(filled, ("phonetic", "pronounce"))
    linkedin = _filled_value(filled, ("linkedin",))
    portfolio = _filled_value(filled, ("portfolio", "website", "other website"))
    open_ended = _open_ended_lines(filled)
    lines = [
        f"*{item.get('id', 'JA-???')} - {item.get('company', '')} - "
        f"{item.get('title', '')}*",
        _outcome_banner(item),
        f"- Status: `{item.get('status', 'unknown')}`",
        f"- Score: `{float(item.get('score', 0.0)):.2f}`",
        f"- Location: `{item.get('location') or 'not listed'}`",
        f"- Source: `{item.get('source') or 'unknown'}`",
        f"- Job link: {item.get('url') or ''}",
        f"- Apply link: {item.get('apply_url') or item.get('url') or ''}",
        f"- Cover letter: `{item.get('cover_letter_path') or 'not generated'}`",
        f"- Cover letter PDF: `{cover_pdf or 'not generated'}`",
        f"- Screenshot: `{screenshot or 'not captured'}`",
        f"- Decided at: `{item.get('decided_at') or 'not recorded'}`",
    ]
    lines.extend([
        "- Final review summary:",
        f"  - Company: {item.get('company', '')}",
        f"  - Role: {item.get('title', '')}",
        f"  - Resume attached: {resume or 'not detected'}",
        (
            f"  - Cover letter PDF attached: {cover_upload}"
            if cover_upload and ".pdf" in cover_upload.lower()
            else (
                f"  - Cover letter PDF generated: `{cover_pdf}` "
                "(no PDF upload field detected)"
                if cover_pdf else
                "  - Cover letter PDF attached: not detected"
            )
        ),
        f"  - Pronunciation field: {pronunciation or 'no pronunciation field detected'}",
        f"  - LinkedIn: {linkedin or 'not detected'}",
        f"  - Portfolio: {portfolio or 'not detected'}",
        (
            "  - Manual confirmation needed: "
            + ("; ".join(str(x) for x in unanswered) if unanswered else "none recorded")
        ),
        "  - Tone check: confident, specific, not desperate",
    ])
    if open_ended:
        lines.append("- Suggested final answers for open-ended questions:")
        for value in open_ended:
            clipped = value if len(value) <= 900 else value[:897] + "..."
            lines.append(f"  - {clipped}")
    if reasons:
        lines.append(f"- Match notes: {reasons}")
    if item.get("result_note"):
        lines.append(f"- Result note: {item['result_note']}")
    if filled:
        lines.append(f"- Filled fields ({len(filled)}):")
        lines.extend(f"  - {value}" for value in filled)
    else:
        lines.append(
            "- Filled fields: field-level details were not recorded for this "
            "run; future approvals will include the full field list."
        )
    if unanswered:
        lines.append(f"- Unanswered required fields ({len(unanswered)}):")
        lines.extend(f"  - {value}" for value in unanswered)
    if cover_text:
        lines.append("- Cover letter content:")
        lines.extend(f"  {line}" for line in cover_text.splitlines())
    lines.append("")
    return lines


def submission_report_text(
    queue_path: str | Path,
    item_ids: list[str],
    action_label: str,
    errors: list[str] | None = None,
) -> str:
    path = Path(queue_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    wanted = set(item_ids)
    selected = [
        item for item in payload.get("queue", [])
        if item.get("id") in wanted
    ]
    counts: dict[str, int] = {}
    for item in selected:
        status = item.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1

    count_text = ", ".join(
        f"{status}: {count}" for status, count in sorted(counts.items())
    ) or "none"
    lines = [
        "*jobagent submission report*",
        f"- Action: `{action_label}`",
        f"- Queue: `{path}`",
        f"- Queue live submit: `{payload.get('live_submit')}`",
        f"- Processed items: `{len(selected)}`",
        f"- Status counts: {count_text}",
        "",
    ]
    for item in selected:
        lines.extend(_record_lines(item))
    if errors:
        lines.append(f"*Errors ({len(errors)})*")
        lines.extend(f"- {err}" for err in errors)
    return "\n".join(lines).strip()


def post_submission_report(
    cfg: Config,
    queue_path: str | Path,
    item_ids: list[str],
    action_label: str,
    errors: list[str] | None = None,
) -> str:
    webhook = os.getenv(cfg.notify.slack.webhook_env, "")
    if not webhook:
        return f"submission report skipped (set ${cfg.notify.slack.webhook_env})"
    if not item_ids and not errors:
        return "submission report skipped (no processed items)"
    try:
        text = submission_report_text(queue_path, item_ids, action_label, errors)
        for chunk in _line_chunks(text):
            requests.post(webhook, json={"text": chunk}, timeout=20).raise_for_status()
        return "submission report sent to Slack"
    except Exception as exc:
        return f"submission report failed ({exc})"


_SKIPPABLE = {"pending_confirmation", "pending_submit"}


def skip_queue_item(queue_path: str | Path, item_id: str) -> dict[str, Any]:
    lookup = load_queue_item(queue_path, item_id)
    if lookup.item.get("status") not in _SKIPPABLE:
        return lookup.item
    lookup.item["status"] = "skipped_by_user"
    lookup.item["decided_at"] = datetime.now().isoformat(timespec="seconds")
    save_queue_item(lookup)
    return lookup.item


def skip_all_queue_items(queue_path: str | Path) -> int:
    path = Path(queue_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    skipped = 0
    for item in payload.get("queue", []):
        if item.get("status") in _SKIPPABLE:
            item["status"] = "skipped_by_user"
            item["decided_at"] = datetime.now().isoformat(timespec="seconds")
            skipped += 1
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return skipped


def queue_status_text(queue_path: str | Path) -> str:
    path = Path(queue_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    counts: dict[str, int] = {}
    for item in payload.get("queue", []):
        status = item.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    lines = [f"*Queue status* `{path}`"]
    for status, count in sorted(counts.items()):
        lines.append(f"- `{status}`: `{count}`")
    if len(lines) == 1:
        lines.append("- no queue items")
    if batch_stop_requested(path):
        lines.append("- `batch_stop_requested`: `true`")
    return "\n".join(lines)


def _approval_tokens() -> tuple[str, str]:
    bot_token = (
        os.getenv("JOBAGENT_SLACK_BOT_TOKEN")
        or os.getenv("SLACK_BOT_TOKEN")
        or ""
    )
    app_token = (
        os.getenv("JOBAGENT_SLACK_APP_TOKEN")
        or os.getenv("SLACK_APP_TOKEN")
        or ""
    )
    if not bot_token or not app_token:
        raise RuntimeError(
            "set JOBAGENT_SLACK_BOT_TOKEN=xoxb-... and "
            "JOBAGENT_SLACK_APP_TOKEN=xapp-..."
        )
    return bot_token, app_token


def _md_to_mrkdwn(lines: list[str]) -> str:
    """Render manual-checklist markdown lines as Slack mrkdwn: `## x` headers
    become a `*x*` bold line and `**x**` becomes `*x*`. Backticks and the ⚠
    gap marker are valid mrkdwn already and pass through untouched."""
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            out.append("*" + stripped.lstrip("#").strip() + "*")
        else:
            out.append(re.sub(r"\*\*(.+?)\*\*", r"*\1*", line))
    return "\n".join(out)


def handle_slash_text(cfg: Config, fixed_path: Path | None,
                      text: str) -> str:
    """Dispatch a slash-command line to jobagent actions and return the
    reply text. Pure enough to test without Slack.

    Grammar:
      refresh [N]        scan the boards fresh, queue + post top N (default 5)
      scan [N]           read-only ranked view of the boards, top N (default
                         25); nothing is queued or posted
      status             latest queue status counts
      approve <ID|all>   two-phase: fill + screenshot (single-phase if disabled)
      confirm <ID|all>   submit item(s) parked as pending_submit
      stop               stop the current approve-all / confirm-all batch
      skip <ID|all>      skip pending item(s)
      holds              required fields that keep blocking submissions
      coverage           answer coverage per ATS (static, offline)
      checklist [N]      regenerate the Ashby manual-submit crib sheet and
                         post the top N roles (default 3, cap 5); alias: ashby
      help               this text
    """
    parts = (text or "").strip().split()
    cmd = (parts[0].lower() if parts else "help")
    arg = " ".join(parts[1:]).strip()

    def path() -> Path:
        return fixed_path or latest_queue_path(cfg)

    try:
        if cmd == "refresh":
            from .review_queue import send_review_queue
            limit = int(arg) if arg.isdigit() else 5
            result = send_review_queue(cfg, limit=limit, interactive=True)
            return (f"Refreshed from the boards: `{result.sent}` item(s) "
                    f"queued and posted"
                    + (f", `{result.manual_only}` manual-only surfaced"
                       if result.manual_only else "")
                    + f".\nQueue: `{result.path}`")

        if cmd == "scan":
            import io
            from contextlib import redirect_stdout
            from .cli import _print_scan
            limit = int(arg) if arg.isdigit() else 25
            buf = io.StringIO()
            with redirect_stdout(buf):
                _print_scan(cfg, limit=limit)
            # No truncation: the caller re-fences long code blocks per
            # chunk via _reply_chunks.
            return "```\n" + buf.getvalue().strip() + "\n```"

        if cmd == "status":
            return queue_status_text(path())

        if cmd == "holds":
            import io
            from contextlib import redirect_stdout
            from .cli import _print_holds
            buf = io.StringIO()
            with redirect_stdout(buf):
                _print_holds(cfg)
            # No truncation: the caller re-fences long code blocks per
            # chunk via _reply_chunks.
            return "```\n" + buf.getvalue().strip() + "\n```"

        if cmd == "coverage":
            import io
            from contextlib import redirect_stdout
            from .cli import _print_coverage
            buf = io.StringIO()
            with redirect_stdout(buf):
                _print_coverage(cfg)
            # No truncation: the caller re-fences long code blocks per
            # chunk via _reply_chunks.
            return "```\n" + buf.getvalue().strip() + "\n```"

        if cmd == "stop":
            return request_batch_stop(path(), "slash command")

        if cmd in {"checklist", "ashby"}:
            # Reporting only: regenerates the crib sheet (template cover
            # letters, no LLM) and posts a preview. Never submits anything.
            from .ashby_form import write_ashby_checklist
            limit = max(1, min(int(arg), 5)) if arg.isdigit() else 3
            sheet_path, roles, gaps = write_ashby_checklist(cfg)
            if not roles:
                return ("No Ashby manual-submit roles right now.\n"
                        f"Full sheet: `{sheet_path}`")
            sections: list[list[str]] = []
            sheet_text = Path(sheet_path).read_text(encoding="utf-8")
            for line in sheet_text.splitlines():
                if line.startswith("## "):
                    sections.append([line])
                elif sections:
                    sections[-1].append(line)
            reply = [f"*Ashby manual-submit checklist* — {roles} role(s), "
                     f"{gaps} gap(s)" + (" ⚠" if gaps else "")]
            for sec in sections[:limit]:
                reply.append("")
                reply.append(_md_to_mrkdwn(sec))
            if roles > limit:
                reply.append(f"\n_+{roles - limit} more role(s) in the "
                             "full sheet._")
            reply.append(f"Full sheet: `{sheet_path}`")
            return "\n".join(reply)

        if cmd in {"approve", "confirm", "skip"}:
            if not arg:
                return f"Usage: `{cmd} JA-001` or `{cmd} all`"
            p = path()
            if cmd == "skip":
                if arg.lower() == "all":
                    return f"Skipped `{skip_all_queue_items(p)}` item(s)."
                item = skip_queue_item(p, arg.upper())
                return f"`{arg.upper()}` is now `{item.get('status')}`."
            two_phase = cfg.autonomy.two_phase_approval
            if cmd == "approve":
                if arg.lower() == "all":
                    ids = pending_queue_item_ids(p)
                    if not ids:
                        return "Nothing is pending_confirmation."
                    if two_phase:
                        _, errors = prepare_all_queue_items(cfg, p)
                        label = "Prepare all (filled, NOT submitted)"
                        ready = pending_submit_item_ids(p)
                        tail = (f"\n`{len(ready)}` ready — `confirm all` "
                                "to submit.") if ready else ""
                    else:
                        _, errors = approve_all_queue_items(cfg, p)
                        label, tail = "Approve all", ""
                    return submission_report_text(p, ids, label, errors) + tail
                item_id = arg.upper()
                if two_phase:
                    prepare_queue_item(cfg, p, item_id)
                    rep = submission_report_text(
                        p, [item_id],
                        f"Prepare {item_id} (filled, NOT submitted)")
                    return (rep + f"\nReview the screenshot, then "
                            f"`confirm {item_id}` to submit.")
                approve_queue_item(cfg, p, item_id)
                return submission_report_text(p, [item_id],
                                              f"Approve {item_id}")
            # confirm
            if arg.lower() == "all":
                ids = pending_submit_item_ids(p)
                if not ids:
                    return ("Nothing is pending_submit. `approve all` "
                            "fills forms first.")
                _, errors = confirm_all_queue_items(cfg, p)
                return submission_report_text(p, ids, "Confirm all", errors)
            item_id = arg.upper()
            confirm_queue_item(cfg, p, item_id)
            return submission_report_text(p, [item_id], f"Confirm {item_id}")

        return (
            "*jobagent commands*\n"
            "`/jobagent refresh [N]` — scan the boards fresh, queue + post "
            "top N approval cards (default 5)\n"
            "`/jobagent scan [N]` — read-only ranked view, top N (default 25);"
            " includes scan-only/manual-only roles that never enter the queue;"
            " nothing is queued or posted\n"
            "`/jobagent status` — queue status counts\n"
            "`/jobagent approve JA-001 | all` — fill + screenshot (no submit)\n"
            "`/jobagent confirm JA-001 | all` — submit what you reviewed\n"
            "`/jobagent stop` — stop the current Approve All / Confirm All batch\n"
            "`/jobagent skip JA-001 | all` — skip pending item(s)\n"
            "`/jobagent holds` — which required fields block submissions\n"
            "`/jobagent coverage` — answer coverage per ATS (offline, no scan)\n"
            "`/jobagent checklist [N]` — Ashby manual-submit crib sheet, "
            "top N roles (default 3, cap 5; alias `ashby`; takes ~2 min: "
            "scan + per-job form fetches)\n"
            "`/refresh [N]` works as a shortcut if you registered it."
        )
    except Exception as exc:
        return f"`{cmd} {arg}`".strip() + f" failed: `{exc}`"


def run_approval_bot(cfg: Config, queue_path: str | None = None) -> None:
    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler
    except ImportError as exc:
        raise RuntimeError(
            "Install Slack approval dependencies first: "
            "python -m pip install -e '.[slack]'"
        ) from exc

    bot_token, app_token = _approval_tokens()
    fixed_path = Path(queue_path) if queue_path else None
    app = App(token=bot_token)

    def _bot_say(client, channel, thread_ts, text,
                 blocks=None) -> None:  # type: ignore[no-untyped-def]
        """Post as the bot, falling back to the incoming webhook.

        The workspace app may lack im:write (only incoming-webhook +
        chat:write + commands), in which case chat.postMessage into the
        webhook's DM fails with channel_not_found. Before this fallback that
        exception killed the handler MID-ACTION — the user tapped a button
        and heard nothing back. Never let a reply failure break the action.
        """
        try:
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=text,
                blocks=blocks,
            )
            return
        except Exception:
            pass
        webhook = os.getenv(cfg.notify.slack.webhook_env, "")
        if not webhook:
            print(f"slack post failed and no webhook fallback: {text[:120]}")
            return
        payload: dict[str, Any] = {"text": text}
        if blocks:
            payload["blocks"] = blocks
        try:
            requests.post(webhook, json=payload, timeout=20).raise_for_status()
        except Exception as exc:
            print(f"slack webhook fallback failed ({exc}): {text[:120]}")

    def _post_screenshot(client, channel, thread_ts, path, item_id) -> None:
        try:
            lookup = load_queue_item(path, item_id)
            shot = _screenshot_from_item(lookup.item)
            if not (channel and shot and Path(shot).exists()):
                return
            client.files_upload_v2(
                channel=channel, thread_ts=thread_ts, file=shot,
                title=f"{lookup.item.get('company', '')} - "
                      f"{lookup.item.get('title', '')}",
            )
        except Exception as exc:
            note = (
                f"(screenshot for {item_id} could not upload: `{exc}`. "
                f"It is on disk; add the files:write scope to the Slack app "
                f"to get screenshots inline.)"
            )
            _bot_say(client, channel, thread_ts, note)

    def _confirm_button_blocks(path, item_id: str) -> list[dict]:
        value = json.dumps({"id": item_id, "queue": str(path)})
        return [{
            "type": "actions",
            "elements": [
                {"type": "button", "style": "primary",
                 "text": {"type": "plain_text",
                          "text": f"Confirm submit {item_id}"},
                 "action_id": "jobagent_confirm", "value": value},
                {"type": "button", "style": "danger",
                 "text": {"type": "plain_text", "text": f"Skip {item_id}"},
                 "action_id": "jobagent_skip", "value": value},
            ],
        }]

    def _stop_batch_blocks(path) -> list[dict]:  # type: ignore[no-untyped-def]
        return [{
            "type": "actions",
            "elements": [{
                "type": "button", "style": "danger",
                "text": {"type": "plain_text", "text": "Stop Batch"},
                "action_id": "jobagent_stop_batch",
                "value": json.dumps({"queue": str(path)}),
            }],
        }]

    @app.action("jobagent_approve")
    def _approve(ack, body, client, respond):  # type: ignore[no-untyped-def]
        ack()
        action_value = body["actions"][0]["value"]
        item_id = _item_arg(action_value)
        path = _queue_arg(action_value, cfg, fixed_path)
        channel = body.get("channel", {}).get("id")
        thread_ts = body.get("message", {}).get("ts")
        two_phase = cfg.autonomy.two_phase_approval
        respond(
            text=(f"Received `APPROVE {item_id}`. "
                  + ("Filling the form for your review (no submit yet)..."
                     if two_phase else "Running jobagent now...")),
            response_type="in_channel",
        )
        blocks: list[dict] = []
        try:
            if two_phase:
                rec = prepare_queue_item(cfg, path, item_id)
                text = submission_report_text(
                    path, [item_id], f"Prepare {item_id} (filled, NOT submitted)")
                if rec.status != "error":
                    lookup = load_queue_item(path, item_id)
                    if lookup.item.get("status") == "pending_submit":
                        blocks = _confirm_button_blocks(path, item_id)
                        text += ("\nReview the screenshot, then tap "
                                 "*Confirm submit* to actually send it.")
                    elif lookup.item.get("unanswered_required"):
                        text += ("\nHeld: required fields above are "
                                 "unanswered. Fix answers.md or finish "
                                 "this one by hand.")
            else:
                rec = approve_queue_item(cfg, path, item_id)
                text = submission_report_text(
                    path, [item_id], f"Approve {item_id}")
        except Exception as exc:
            text = f"`{item_id}` failed: `{exc}`"
        if channel:
            _bot_say(
                client, channel, thread_ts, text,
                blocks=(
                    [{"type": "section",
                      "text": {"type": "mrkdwn", "text": chunk}}
                     for chunk in _line_chunks(text, 2900)] + blocks
                ) if blocks else None,
            )
            _post_screenshot(client, channel, thread_ts, path, item_id)

    @app.action("jobagent_confirm")
    def _confirm(ack, body, client, respond):  # type: ignore[no-untyped-def]
        ack()
        action_value = body["actions"][0]["value"]
        item_id = _item_arg(action_value)
        path = _queue_arg(action_value, cfg, fixed_path)
        channel = body.get("channel", {}).get("id")
        thread_ts = body.get("message", {}).get("ts")
        respond(
            text=f"Received `CONFIRM {item_id}`. Submitting now...",
            response_type="in_channel",
        )
        try:
            confirm_queue_item(cfg, path, item_id)
            text = submission_report_text(path, [item_id], f"Confirm {item_id}")
        except Exception as exc:
            text = f"`{item_id}` confirm failed: `{exc}`"
        if channel:
            for chunk in _line_chunks(text, 2900):
                _bot_say(client, channel, thread_ts, chunk)
            _post_screenshot(client, channel, thread_ts, path, item_id)

    @app.action("jobagent_skip")
    def _skip(ack, body, respond):  # type: ignore[no-untyped-def]
        ack()
        action_value = body["actions"][0]["value"]
        item_id = _item_arg(action_value)
        path = _queue_arg(action_value, cfg, fixed_path)
        try:
            item = skip_queue_item(path, item_id)
            text = (
                f"`{item_id}` marked `{item.get('status')}`: "
                f"{item.get('company')} - {item.get('title')}"
            )
        except Exception as exc:
            text = f"`{item_id}` skip failed: `{exc}`"
        respond(text=text, response_type="in_channel")

    @app.action("jobagent_approve_all")
    def _approve_all(ack, body, client, respond):  # type: ignore[no-untyped-def]
        ack()
        action_value = body["actions"][0].get("value", "")
        path = _queue_arg(action_value, cfg, fixed_path)
        channel = body.get("channel", {}).get("id")
        thread_ts = body.get("message", {}).get("ts")
        two_phase = cfg.autonomy.two_phase_approval
        if two_phase:
            respond(
                text=("Received `Approve all`. Filling every pending form "
                      "for review (nothing submits yet)..."),
                response_type="in_channel",
            )
            item_ids = pending_queue_item_ids(path)
            if channel:
                _bot_say(
                    client, channel, thread_ts,
                    "Approve All is running. Tap Stop Batch to stop after "
                    "the current item.",
                    blocks=_stop_batch_blocks(path),
                )
            records, errors = prepare_all_queue_items(cfg, path)
            text = submission_report_text(
                path, item_ids, "Prepare all (filled, NOT submitted)", errors)
            ready = pending_submit_item_ids(path)
            blocks: list[dict] = []
            if ready:
                blocks = [{
                    "type": "actions",
                    "elements": [{
                        "type": "button", "style": "primary",
                        "text": {"type": "plain_text",
                                 "text": f"Confirm all: submit {len(ready)} "
                                         "application(s)"},
                        "action_id": "jobagent_confirm_all",
                        "value": json.dumps({"queue": str(path)}),
                    }],
                }]
                text += (f"\n{len(ready)} item(s) filled clean and are "
                         "waiting on *Confirm all*.")
            if channel:
                chunks = _line_chunks(text, 2900)
                for chunk in chunks[:-1]:
                    _bot_say(client, channel, thread_ts, chunk)
                _bot_say(
                    client, channel, thread_ts, chunks[-1],
                    blocks=[{"type": "section",
                             "text": {"type": "mrkdwn", "text": chunks[-1]}}]
                    + blocks if blocks else None,
                )
                for item_id in item_ids:
                    _post_screenshot(client, channel, thread_ts, path, item_id)
            return
        respond(
            text="Received `Approve all`. Running pending queue items now...",
            response_type="in_channel",
        )
        item_ids = pending_queue_item_ids(path)
        if channel:
            _bot_say(
                client, channel, thread_ts,
                "Approve All is running. Tap Stop Batch to stop after "
                "the current item.",
                blocks=_stop_batch_blocks(path),
            )
        records, errors = approve_all_queue_items(cfg, path)
        text = submission_report_text(path, item_ids, "Approve all", errors)
        if channel:
            for chunk in _line_chunks(text, 2900):
                _bot_say(client, channel, thread_ts, chunk)

    @app.action("jobagent_confirm_all")
    def _confirm_all(ack, body, client, respond):  # type: ignore[no-untyped-def]
        ack()
        action_value = body["actions"][0].get("value", "")
        path = _queue_arg(action_value, cfg, fixed_path)
        channel = body.get("channel", {}).get("id")
        thread_ts = body.get("message", {}).get("ts")
        item_ids = pending_submit_item_ids(path)
        respond(
            text=(f"Received `Confirm all`. Submitting {len(item_ids)} "
                  "application(s) with pacing..."),
            response_type="in_channel",
        )
        if channel:
            _bot_say(
                client, channel, thread_ts,
                "Confirm All is running. Tap Stop Batch to stop after "
                "the current item.",
                blocks=_stop_batch_blocks(path),
            )
        records, errors = confirm_all_queue_items(cfg, path)
        text = submission_report_text(path, item_ids, "Confirm all", errors)
        if channel:
            for chunk in _line_chunks(text, 2900):
                _bot_say(client, channel, thread_ts, chunk)
            for item_id in item_ids:
                _post_screenshot(client, channel, thread_ts, path, item_id)

    @app.action("jobagent_stop_batch")
    def _stop_batch(ack, body, respond):  # type: ignore[no-untyped-def]
        ack()
        action_value = body["actions"][0].get("value", "")
        path = _queue_arg(action_value, cfg, fixed_path)
        respond(
            text=request_batch_stop(path, "Slack Stop Batch button"),
            response_type="in_channel",
        )

    @app.action("jobagent_skip_all")
    def _skip_all(ack, body, respond):  # type: ignore[no-untyped-def]
        ack()
        action_value = body["actions"][0].get("value", "")
        path = _queue_arg(action_value, cfg, fixed_path)
        try:
            skipped = skip_all_queue_items(path)
            text = f"`Skip all` marked `{skipped}` pending item(s) skipped."
        except Exception as exc:
            text = f"`Skip all` failed: `{exc}`"
        respond(text=text, response_type="in_channel")

    @app.action("jobagent_queue_status")
    def _queue_status(ack, body, respond):  # type: ignore[no-untyped-def]
        ack()
        action_value = body["actions"][0].get("value", "")
        try:
            path = _queue_arg(action_value, cfg, fixed_path)
            text = queue_status_text(path)
        except Exception as exc:
            text = f"Queue status failed: `{exc}`"
        respond(text=text, response_type="in_channel")

    def _refresh_queue(limit: int, respond) -> None:  # type: ignore[no-untyped-def]
        from .review_queue import send_review_queue

        try:
            result = send_review_queue(cfg, limit=limit, interactive=True)
            text = (
                f"Refreshed Slack review queue: `{result.sent}` item(s). "
                f"Queue file: `{result.path}`"
            )
        except Exception as exc:
            text = f"Refresh failed: `{exc}`"
        respond(text=text, response_type="in_channel")

    @app.action("jobagent_queue_top_5")
    def _queue_top_5(ack, respond):  # type: ignore[no-untyped-def]
        ack()
        respond(text="Refreshing Top 5 queue...", response_type="in_channel")
        _refresh_queue(5, respond)

    @app.action("jobagent_queue_top_25")
    def _queue_top_25(ack, respond):  # type: ignore[no-untyped-def]
        ack()
        respond(text="Refreshing Top 25 queue...", response_type="in_channel")
        _refresh_queue(25, respond)

    @app.action("jobagent_coverage")
    def _btn_coverage(ack, respond):  # type: ignore[no-untyped-def]
        ack()
        respond(text=_slash_ack_text("coverage"), response_type="in_channel")
        _run_slash("coverage", respond)

    @app.action("jobagent_scan")
    def _btn_scan(ack, respond):  # type: ignore[no-untyped-def]
        ack()
        respond(text=_slash_ack_text("scan"), response_type="in_channel")
        _run_slash("scan 10", respond)

    @app.action("jobagent_ashby_checklist")
    def _btn_checklist(ack, respond):  # type: ignore[no-untyped-def]
        ack()
        respond(text=_slash_ack_text("checklist"), response_type="in_channel")
        _run_slash("checklist", respond)

    @app.message(re.compile(r"^\s*-?\s*approve all\s*$", re.IGNORECASE))
    def _approve_all_text(message, say):  # type: ignore[no-untyped-def]
        path = fixed_path or latest_queue_path(cfg)
        item_ids = pending_queue_item_ids(path)
        if cfg.autonomy.two_phase_approval:
            records, errors = prepare_all_queue_items(cfg, path)
            label = "Prepare all (filled, NOT submitted)"
            ready = len(pending_submit_item_ids(path))
            trailer = (f"\n{ready} item(s) ready. Reply `confirm all` "
                       "to submit them.") if ready else ""
        else:
            records, errors = approve_all_queue_items(cfg, path)
            label, trailer = "Approve all", ""
        for chunk in _line_chunks(
            submission_report_text(path, item_ids, label, errors) + trailer
        ):
            say(chunk)

    @app.message(re.compile(r"^\s*-?\s*confirm all\s*$", re.IGNORECASE))
    def _confirm_all_text(message, say):  # type: ignore[no-untyped-def]
        path = fixed_path or latest_queue_path(cfg)
        item_ids = pending_submit_item_ids(path)
        if not item_ids:
            say("Nothing is pending_submit. `approve all` fills forms first.")
            return
        records, errors = confirm_all_queue_items(cfg, path)
        for chunk in _line_chunks(
            submission_report_text(path, item_ids, "Confirm all", errors)
        ):
            say(chunk)

    def _run_slash(text: str, respond) -> None:  # type: ignore[no-untyped-def]
        import threading

        def work() -> None:
            reply = handle_slash_text(cfg, fixed_path, text)
            for chunk in _reply_chunks(reply, 3500):
                try:
                    respond(text=chunk, response_type="in_channel")
                except Exception:
                    pass

        threading.Thread(target=work, daemon=True).start()

    @app.command("/jobagent")
    def _cmd_jobagent(ack, command, respond):  # type: ignore[no-untyped-def]
        text = (command.get("text") or "").strip()
        verb = text.split()[0].lower() if text else "help"
        ack(_slash_ack_text(verb))
        _run_slash(text, respond)

    @app.command("/refresh")
    def _cmd_refresh(ack, command, respond):  # type: ignore[no-untyped-def]
        ack("jobagent: scanning the boards fresh...")
        _run_slash(f"refresh {(command.get('text') or '').strip()}".strip(),
                   respond)

    path_label = str(fixed_path) if fixed_path else "latest queue"
    print(f"Listening for Slack approvals on: {path_label}")
    print(f"Safety: live_submit={cfg.autonomy.live_submit} | "
          f"two_phase_approval={cfg.autonomy.two_phase_approval}")
    print("Slash commands: /jobagent (refresh/scan/status/approve/confirm/"
          "skip/holds/coverage/checklist), /refresh — register them once in "
          "your Slack app config.")
    SocketModeHandler(app, app_token).start()
