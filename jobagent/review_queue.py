"""Slack review queue for human-confirmed applications.

This module intentionally does not submit, fill forms, or write the tracker.
It scans and scores jobs, sends one metadata message per eligible application,
and saves a JSON queue so a later confirmed ID can be acted on deliberately.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field as dataclass_field
from datetime import datetime
from pathlib import Path

import requests

from .apply import submit_mode
from .config import Config
from .models import Job
from .pipeline import scan


@dataclass
class ReviewItem:
    id: str
    company: str
    title: str
    score: float
    location: str
    source: str
    url: str
    apply_url: str
    reasons: list[str]
    status: str = "pending_confirmation"
    external_id: str = ""  # preserved so records keep the source's stable uid
    # Per-job answer coverage (greenhouse only for now): the posting's REAL
    # required questions diffed against answers.md at queue time, so gaps are
    # fixed BEFORE Approve instead of surfacing as a mid-fill hold.
    coverage_checked: bool = False
    required_question_count: int = 0
    uncovered_required: list[str] = dataclass_field(default_factory=list)


@dataclass
class ReviewQueueResult:
    path: str
    sent: int
    manual_only: int
    items: list[ReviewItem]


def build_review_items(cfg: Config, jobs: list[Job],
                       limit: int | None = None) -> list[ReviewItem]:
    cap = limit if limit is not None else cfg.autonomy.max_applications_per_run
    if not cap:
        cap = len(jobs)
    # Never re-queue a job that already reached a terminal outcome — before
    # this filter, freshly applied jobs reappeared at the top of every
    # morning queue and made "did I already apply to this?" a real question.
    # Match by uid AND url: older records lack external_id so only their
    # url lines up with a fresh scan.
    from .tracker import Tracker
    tracker = Tracker(cfg.db_path, cfg.csv_path, cfg.csv_statuses)
    decided = tracker.decided_uids()
    decided_urls = tracker.decided_urls()
    eligible = [
        j for j in jobs
        if j.score >= cfg.autonomy.min_score and not j.scan_only
        # Ashby-type (manual_submit) platforms flag automation — keep them OUT of
        # the interactive Approve/Confirm flow (the bot can't submit them); they
        # go in the "submit yourself" section instead.
        and submit_mode(j.source, cfg) == "auto"
        and j.uid not in decided
        and (j.url or "").rstrip("/").lower() not in decided_urls
    ][:cap]
    items = [
        ReviewItem(
            id=f"JA-{i:03d}",
            company=j.company,
            title=j.title,
            score=round(j.score, 3),
            location=j.location,
            source=j.source,
            url=j.url,
            apply_url=j.apply_url or j.url,
            reasons=j.reasons,
            external_id=j.external_id,
        )
        for i, j in enumerate(eligible, start=1)
    ]
    # Pre-flight answer coverage against each posting's REAL questions
    # (greenhouse). Best-effort by design: never blocks queue building.
    try:
        from .gh_questions import annotate_review_items
        annotate_review_items(cfg, items)
    except Exception:
        pass
    return items


def _queue_path(cfg: Config) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(cfg.output_dir) / "review_queues"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"job-review-queue-{stamp}.json"


def write_review_queue(cfg: Config, items: list[ReviewItem],
                       manual_only: list[Job]) -> Path:
    path = _queue_path(cfg)
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "config": "config.yaml",
        "mode": cfg.autonomy.mode,
        "live_submit": cfg.autonomy.live_submit,
        "min_score": cfg.autonomy.min_score,
        "manual_only_companies": cfg.autonomy.manual_only_companies,
        "queue": [asdict(i) for i in items],
        "manual_only_visible": [
            {
                "company": j.company,
                "title": j.title,
                "score": round(j.score, 3),
                "url": j.url,
                "reasons": j.reasons,
            }
            for j in manual_only[:25]
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _slack_bot_token() -> str:
    return os.getenv("JOBAGENT_SLACK_BOT_TOKEN") or os.getenv("SLACK_BOT_TOKEN", "")


def _slack_channel() -> str:
    return os.getenv("JOBAGENT_SLACK_CHANNEL", "")


def _post_slack(
    webhook: str,
    text: str,
    blocks: list[dict] | None = None,
    prefer_bot: bool = False,
) -> None:
    bot_token = _slack_bot_token()
    channel = _slack_channel()
    if prefer_bot and bot_token and channel:
        payload: dict = {"channel": channel, "text": text}
        if blocks:
            payload["blocks"] = blocks
        resp = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {bot_token}"},
            json=payload,
            timeout=20,
        )
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"chat.postMessage failed: {data.get('error')}")
        return

    payload: dict = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    resp = requests.post(webhook, json=payload, timeout=20)
    resp.raise_for_status()


def _header_text(path: Path, items: list[ReviewItem], manual_only_count: int,
                 cfg: Config, interactive: bool = False) -> str:
    confirm_line = (
        "- Use the Approve / Skip / Approve All buttons from Slack on your phone. Keep "
        "`jobagent approval-bot` running on this computer."
        if interactive else
        "- Confirm one at a time with the ID, e.g. `APPROVE JA-001`. "
        "This webhook cannot read replies, so send the same ID back in Codex "
        "when you want me to act."
    )
    return (
        "*jobagent review queue started*\n"
        f"- Queue file: `{path}`\n"
        f"- Auto-eligible candidates: `{len(items)}`\n"
        f"- Manual-only visible matches: `{manual_only_count}`\n"
        f"- Current safety: `mode={cfg.autonomy.mode}`, "
        f"`live_submit={cfg.autonomy.live_submit}`\n"
        "- No applications were submitted.\n"
        f"{confirm_line}"
    )


def _header_blocks(path: Path, items: list[ReviewItem], manual_only_count: int,
                   cfg: Config) -> list[dict]:
    body = _header_text(path, items, manual_only_count, cfg, interactive=True)
    safety = (
        "This fills forms, creates screenshots, and prepares a review summary. "
        "It does not submit; use Confirm after review."
        if cfg.autonomy.two_phase_approval else
        (
            "This will dry-run all pending items because `live_submit=false`."
            if not cfg.autonomy.live_submit else
            "This may submit every pending item because `live_submit=true`."
        )
    )
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": body},
        },
        {
            "type": "actions",
            "block_id": "jobagent_review_queue_actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text",
                             "text": "Approve All (Fill Only)"},
                    "style": "primary",
                    "value": json.dumps({"queue": str(path)}),
                    "action_id": "jobagent_approve_all",
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Approve all?"},
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"Approve all `{len(items)}` pending jobs in this "
                                f"queue. {safety}"
                            ),
                        },
                        "confirm": {"type": "plain_text",
                                    "text": "Fill Only"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Stop Batch"},
                    "style": "danger",
                    "value": json.dumps({"queue": str(path)}),
                    "action_id": "jobagent_stop_batch",
                },
            ],
        },
    ]


def _control_panel_text(cfg: Config) -> str:
    live = "live submit ON" if cfg.autonomy.live_submit else "dry-run only"
    return (
        "*jobagent control panel*\n"
        f"- Safety: `{live}`\n"
        f"- Mode: `mode={cfg.autonomy.mode}`, `live_submit={cfg.autonomy.live_submit}`\n"
        "- Use these buttons from Slack on your phone. Keep "
        "`jobagent approval-bot` running on your computer."
    )


def _control_panel_blocks(cfg: Config) -> list[dict]:
    safety = (
        "Approve All fills forms, creates screenshots, and posts a final review "
        "summary. Confirm All is the submit step."
        if cfg.autonomy.two_phase_approval else
        (
            "Approve All may submit applications because `live_submit=true`."
            if cfg.autonomy.live_submit else
            "Approve All will dry-run fills/screenshots because `live_submit=false`."
        )
    )
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": _control_panel_text(cfg)},
        },
        {
            "type": "actions",
            "block_id": "jobagent_control_panel_queue",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Refresh Top 5"},
                    "style": "primary",
                    "value": "5",
                    "action_id": "jobagent_queue_top_5",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Refresh Top 25"},
                    "value": "25",
                    "action_id": "jobagent_queue_top_25",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Queue Status"},
                    "value": json.dumps({"queue": "latest"}),
                    "action_id": "jobagent_queue_status",
                },
            ],
        },
        {
            "type": "actions",
            "block_id": "jobagent_control_panel_bulk",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text",
                             "text": "Approve All (Fill Only)"},
                    "style": "primary",
                    "value": json.dumps({"queue": "latest"}),
                    "action_id": "jobagent_approve_all",
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Approve all pending?"},
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                "Approve all pending items in the latest queue. "
                                f"{safety}"
                            ),
                        },
                        "confirm": {"type": "plain_text",
                                    "text": "Fill Only"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Stop Batch"},
                    "style": "danger",
                    "value": json.dumps({"queue": "latest"}),
                    "action_id": "jobagent_stop_batch",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Skip All"},
                    "style": "danger",
                    "value": json.dumps({"queue": "latest"}),
                    "action_id": "jobagent_skip_all",
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Skip all pending?"},
                        "text": {
                            "type": "mrkdwn",
                            "text": "Mark all pending items in the latest queue as skipped.",
                        },
                        "confirm": {"type": "plain_text", "text": "Skip All"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
            ],
        },
        {
            "type": "actions",
            "block_id": "jobagent_control_panel_reports",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Coverage"},
                    "value": "coverage",
                    "action_id": "jobagent_coverage",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Scan Top 10"},
                    "value": "10",
                    "action_id": "jobagent_scan",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Ashby Checklist"},
                    "value": "checklist",
                    "action_id": "jobagent_ashby_checklist",
                },
            ],
        },
    ]


def send_control_panel(cfg: Config) -> None:
    webhook = os.getenv(cfg.notify.slack.webhook_env, "")
    if not webhook:
        raise RuntimeError(f"set ${cfg.notify.slack.webhook_env} first")
    _post_slack(
        webhook,
        _control_panel_text(cfg),
        blocks=_control_panel_blocks(cfg),
        prefer_bot=True,
    )


def _coverage_line(item: ReviewItem) -> str:
    """One line of pre-flight answer coverage for the Slack card. Empty when
    the item was not checked (non-greenhouse source or API hiccup)."""
    if not item.coverage_checked:
        return ""
    n = item.required_question_count
    gaps = item.uncovered_required
    if not gaps:
        return f"- Answer coverage: all `{n}` required questions covered ✓\n"
    listed = "; ".join(gaps[:6])
    if len(gaps) > 6:
        listed += f"; …and {len(gaps) - 6} more"
    return (f"- ⚠ Answer gaps (`{len(gaps)}` of `{n}` required — fix "
            f"answers.md BEFORE Approve or this will hold): {listed}\n")


def _item_text(item: ReviewItem, total: int) -> str:
    reasons = "; ".join(item.reasons) or "no reasons recorded"
    if len(reasons) > 700:
        reasons = reasons[:697] + "..."
    location = item.location or "not listed"
    return (
        f"*{item.id} / {total:03d} - review before apply*\n"
        f"- Company: `{item.company}`\n"
        f"- Role: `{item.title}`\n"
        f"- Score: `{item.score:.2f}`\n"
        f"- Location: `{location}`\n"
        f"- Source: `{item.source}`\n"
        f"- Match notes: {reasons}\n"
        f"- Job link: {item.url}\n"
        f"{_coverage_line(item)}"
        "- Status: `pending_confirmation`; not submitted.\n"
        f"- Confirm: `APPROVE {item.id}` or reject: `SKIP {item.id}`"
    )


def _item_blocks(item: ReviewItem, total: int, path: Path) -> list[dict]:
    reasons = "; ".join(item.reasons) or "no reasons recorded"
    if len(reasons) > 700:
        reasons = reasons[:697] + "..."
    location = item.location or "not listed"
    coverage = _coverage_line(item)
    body = (
        f"*{item.id} / {total:03d} - review before apply*\n"
        f"*Company:* `{item.company}`\n"
        f"*Role:* `{item.title}`\n"
        f"*Score:* `{item.score:.2f}`\n"
        f"*Location:* `{location}`\n"
        f"*Source:* `{item.source}`\n"
        f"*Match notes:* {reasons}\n"
        f"*Job link:* {item.url}\n"
        f"{coverage}"
        "*Status:* `pending_confirmation`; not submitted."
    )
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": body},
        },
        {
            "type": "actions",
            "block_id": f"jobagent_review_{item.id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "value": json.dumps({"id": item.id, "queue": str(path)}),
                    "action_id": "jobagent_approve",
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Approve?"},
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"Approve `{item.id}` for jobagent to run. "
                                "It will still obey `live_submit`."
                            ),
                        },
                        "confirm": {"type": "plain_text", "text": "Approve"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Skip"},
                    "style": "danger",
                    "value": json.dumps({"id": item.id, "queue": str(path)}),
                    "action_id": "jobagent_skip",
                },
            ],
        },
    ]


def _manual_text(manual_only: list[Job]) -> str:
    lines = ["*Submit these yourself — kept out of the Approve flow*",
             "_Ashby flags automation; dream companies are scan-only. "
             "Open each in your own browser and submit._"]
    for j in manual_only[:12]:
        lines.append(f"- `{j.score:.2f}` [{j.source}] {j.company} - {j.title}: {j.url}")
    if len(manual_only) > 12:
        lines.append(f"- ...and {len(manual_only) - 12} more")
    return "\n".join(lines)


def send_review_queue(
    cfg: Config,
    limit: int | None = None,
    interactive: bool = False,
) -> ReviewQueueResult:
    result = scan(cfg)
    # "Submit yourself" bucket: scan-only dream companies AND Ashby-type
    # platforms that flag automation. Both are kept out of the Approve flow.
    manual_only = [
        j for j in result.jobs
        if j.score >= cfg.autonomy.min_score
        and (j.scan_only or submit_mode(j.source, cfg) == "manual")
    ]
    items = build_review_items(cfg, result.jobs, limit=limit)
    path = write_review_queue(cfg, items, manual_only)

    webhook = os.getenv(cfg.notify.slack.webhook_env, "")
    if not webhook:
        raise RuntimeError(f"set ${cfg.notify.slack.webhook_env} first")

    _post_slack(
        webhook,
        _header_text(path, items, len(manual_only), cfg, interactive),
        blocks=_header_blocks(path, items, len(manual_only), cfg)
        if interactive else None,
        prefer_bot=interactive,
    )
    for item in items:
        _post_slack(
            webhook,
            _item_text(item, len(items)),
            blocks=_item_blocks(item, len(items), path) if interactive else None,
            prefer_bot=interactive,
        )
        time.sleep(0.35)
    if manual_only:
        _post_slack(webhook, _manual_text(manual_only))

    return ReviewQueueResult(
        path=str(path),
        sent=len(items),
        manual_only=len(manual_only),
        items=items,
    )
