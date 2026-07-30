"""Command line interface.

  python -m jobagent.cli setup   --config config.yaml   first-run setup wizard (browser)
  python -m jobagent.cli scan    --config config.yaml   discover + rank, no side effects
  python -m jobagent.cli answers --config config.yaml   preview form answers, offline
  python -m jobagent.cli review-queue --config config.yaml   Slack metadata, no submit
  python -m jobagent.cli slack-panel --config config.yaml   post Slack button panel
  python -m jobagent.cli approval-bot --config config.yaml   listen for Slack buttons
  python -m jobagent.cli approval-web --config config.yaml   phone approval web panel
  python -m jobagent.cli approval-web-link --config config.yaml   send web link
  python -m jobagent.cli run     --config config.yaml   match + generate + apply + track
  python -m jobagent.cli status  --config config.yaml   show the tracker
  python -m jobagent.cli holds   --config config.yaml   which required fields block submissions
  python -m jobagent.cli test-notify --config config.yaml   send a test notification
  python -m jobagent.cli test-llm --config config.yaml      verify LLM provider
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import shlex
import sys

from .answers import AnswerBook
from .apply import submit_mode
from .config import load_config, profile_gaps
from .llm import complete, selected_provider
from .pipeline import run, scan
from .review_queue import send_control_panel, send_review_queue
from .slack_approval import run_approval_bot
from .tracker import Tracker
from .web_approval import (
    approval_token,
    approval_url,
    post_approval_link,
    run_approval_web,
)

# A spread of typical ATS field labels, used by the `answers` preview command.
SAMPLE_LABELS = [
    ("First Name", "text", None),
    ("Last Name", "text", None),
    ("Email", "text", None),
    ("Phone", "text", None),
    ("LinkedIn Profile", "text", None),
    ("Website / Portfolio", "text", None),
    ("Are you legally authorized to work in the US?", "select", ["Yes", "No"]),
    ("Will you now or in the future require sponsorship?", "select", ["Yes", "No"]),
    ("Are you willing to relocate?", "select", ["Yes", "No"]),
    ("Desired salary", "text", None),
    ("Why do you want to work here?", "textarea", None),
    ("Tell us about yourself", "textarea", None),
    ("How did you hear about us?", "text", None),
    ("Years of experience with Kubernetes", "text", None),  # deliberately uncovered
]


def _configure_logging(cfg) -> None:
    """Configure the `jobagent` logger tree once, at process start: INFO (or
    the configured level) to stderr, plus an optional durable file handler for
    the launchd daily run. Idempotent, so repeated CLI calls in one process do
    not stack handlers."""
    root = logging.getLogger("jobagent")
    if root.handlers:
        return
    level = getattr(logging, str(cfg.logging.level).upper(), logging.INFO)
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream = logging.StreamHandler()  # stderr; keeps stdout clean for reports
    stream.setFormatter(fmt)
    root.addHandler(stream)
    if cfg.logging.file:
        try:
            Path(cfg.logging.file).parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(cfg.logging.file, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except Exception as exc:  # a bad log path must never break a run
            root.warning("could not open log file %r: %s", cfg.logging.file, exc)
    root.propagate = False


def _load_local_env() -> None:
    env_path = Path("scripts/.env")
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        try:
            parts = shlex.split(value, comments=False, posix=True)
        except ValueError:
            parts = [value.strip()]
        os.environ[key] = parts[0] if parts else ""


def _print_coverage(cfg) -> None:
    from .coverage import coverage_report, total_gaps
    # LLM off on purpose: a would-be LLM answer must not mask a real gap.
    book = AnswerBook.from_file(cfg.profile.answers_path, use_llm=False)
    reports = coverage_report(cfg, book)
    print("Answer coverage by ATS — can your answers file produce an answer "
          "for each field these forms ask?\n")
    for rep in reports:
        mark = "OK " if not rep.gaps else "GAP"
        submit = "you submit" if rep.submit_mode == "manual" else "auto-submit"
        print(f"  [{mark}] {rep.source}  ({submit})  "
              f"{len(rep.covered)}/{rep.total} covered")
        for g in rep.gaps:
            print(f"          missing: {g}")
    n = total_gaps(reports)
    if n == 0:
        print("\nAll expected fields resolve — no gaps.")
    else:
        print(f"\n{n} gap(s). Add an alias in jobagent/answers.py (FIELD_ALIASES) "
              f"or a value under ## Fields in your answers file, then re-run.")


def _print_scan(cfg, limit: int = 25) -> None:
    result = scan(cfg)
    if result.errors:
        print("Fetch notes:")
        for e in result.errors:
            print(f"  ! {e}")
        print()

    top = [j for j in result.jobs if j.score > 0][:limit]
    if not top:
        print("No scored matches. Check your sources and profile keywords.")
        return

    print(f"Top {len(top)} matches:\n")
    auto_n = manual_n = 0
    for j in top:
        if j.scan_only:
            tag = " [scan-only]"
        elif submit_mode(j.source, cfg) == "manual":
            tag = f" [{j.source} · you submit]"
            manual_n += 1
        else:
            tag = f" [{j.source} · auto-submit]"
            auto_n += 1
        print(f"  {j.score:>4.2f}  {j.company} - {j.title}{tag}")
        print(f"        {j.url}")
        print(f"        {'; '.join(j.reasons)}")
        print()
    print(f"Submit routing: {auto_n} auto-submit, {manual_n} you-submit")
    from .coverage import coverage_report, total_gaps
    gaps = total_gaps(coverage_report(
        cfg, AnswerBook.from_file(cfg.profile.answers_path, use_llm=False)))
    if gaps:
        print(f"⚠  {gaps} answer gap(s) across your ATS forms — run "
              f"`jobagent coverage` to see them before applying.")


def _print_run(cfg, use_llm: bool) -> None:
    summary = run(cfg, use_llm=use_llm)
    print("Run complete.\n")
    print(f"  scanned:      {summary['scanned']}")
    print(f"  processed:    {summary['processed']}")
    print(f"  already seen: {summary['already_seen']}")
    if summary.get("applied_this_run"):
        print(f"  auto attempts:{summary['applied_this_run']}")
    if summary.get("deferred_over_cap"):
        print(f"  deferred (cap): {summary['deferred_over_cap']} "
              f"(raise autonomy.max_applications_per_run to do more per run)")
    if summary["errors"]:
        print("  fetch notes:")
        for e in summary["errors"]:
            print(f"    ! {e}")
    print("  status counts:")
    for status, n in sorted(summary["status_counts"].items()):
        print(f"    {status}: {n}")

    if summary.get("notify"):
        print("  notifications:")
        for line in summary["notify"]:
            print(f"    {line}")

    mode = cfg.autonomy.mode
    live = cfg.autonomy.live_submit
    print(f"\n  autonomy: mode={mode}, live_submit={live}")
    if mode == "review" or not live:
        print("  (nothing was submitted; anything eligible is in needs_review)")


def _print_review_queue(cfg, limit: int | None, interactive: bool) -> None:
    result = send_review_queue(cfg, limit=limit, interactive=interactive)
    print("Review queue sent to Slack.\n")
    print(f"  queue file:   {result.path}")
    print(f"  sent:         {result.sent}")
    print(f"  manual-only:  {result.manual_only}")
    print("  submitted:    0")
    if interactive:
        print("\nStart approval-bot, then use Approve / Skip buttons in Slack.")
    else:
        print("\nConfirm one ID at a time, e.g. APPROVE JA-001.")


def _print_slack_panel(cfg) -> None:
    send_control_panel(cfg)
    print("Slack control panel sent.")


def _print_approval_web_link(cfg, host: str, port: int) -> None:
    url = approval_url(host, port, approval_token())
    post_approval_link(cfg, url)
    print("Approval web panel link sent to Slack.")
    print(f"  url: {url}")


def _print_status(cfg) -> None:
    tracker = Tracker(cfg.db_path)
    records = tracker.all()
    if not records:
        print("Tracker is empty. Run the pipeline first.")
        return
    print(f"{len(records)} tracked applications:\n")
    for r in records:
        print(f"  [{r.status}] {r.score:>4.2f}  {r.company} - {r.title}")
        if r.note:
            print(f"        {r.note}")


def _print_holds(cfg) -> None:
    """Aggregate the required fields the filler could not answer, so you know
    exactly what to add to answers.md to unlock the most applications."""
    import json as _json
    from collections import Counter

    counter: Counter[str] = Counter()
    examples: dict[str, str] = {}

    for r in Tracker(cfg.db_path).all():
        for label in (r.unanswered_required or []):
            label = label.strip()
            if label:
                counter[label] += 1
                examples.setdefault(label, f"{r.company} - {r.title}")

    # Review queues can hold fills that never reached the tracker.
    queue_dir = Path(cfg.output_dir) / "review_queues"
    if queue_dir.is_dir():
        seen_pairs: set[tuple[str, str]] = set()
        for qp in sorted(
            p for p in queue_dir.glob("job-review-queue-*.json")
            if not p.name.endswith(".stop.json")
        ):
            try:
                payload = _json.loads(qp.read_text(encoding="utf-8"))
            except Exception:
                continue
            for item in payload.get("queue", []):
                key = f"{item.get('company')}|{item.get('title')}"
                for label in (item.get("unanswered_required") or []):
                    label = str(label).strip()
                    if label and (label, key) not in seen_pairs:
                        seen_pairs.add((label, key))
                        counter[label] += 1
                        examples.setdefault(
                            label,
                            f"{item.get('company')} - {item.get('title')}")

    if not counter:
        print("No held required fields recorded yet. Either every form "
              "filled clean, or no fills have run since field-level "
              "tracking was added.")
        return

    total = sum(counter.values())
    print(f"Required fields the answers file could not cover "
          f"({total} holds across {len(counter)} distinct fields):\n")
    width = max(len(label) for label in counter)
    for label, n in counter.most_common():
        print(f"  {n:>3}x  {label:<{width}}  e.g. {examples[label]}")
    print("\nEach line above is one answers.md addition away from unlocking "
          "every application it held. Add lines under ## Fields (short "
          "values) or ## Questions (free text), then preview with:\n"
          "  jobagent answers --config config.yaml")


def _print_answers(cfg) -> None:
    book = AnswerBook.from_file(cfg.profile.answers_path,
                               use_llm=cfg.apply.use_llm_for_answers,
                               llm_settings=cfg.llm)
    if not book.fields and not book.qa:
        print(f"No answers loaded. Check profile.answers_path "
              f"(currently: {cfg.profile.answers_path!r}).")
        return
    print("Preview of how sample form fields would be answered:\n")
    for label, kind, options in SAMPLE_LABELS:
        r = book.resolve(label, kind, options)
        if r is None:
            print(f"  [HOLD] {label}")
            print(f"         no confident match; would be left for you\n")
        else:
            val = r.value if len(r.value) <= 90 else r.value[:87] + "..."
            print(f"  [{r.source:^8}] {label}")
            print(f"         -> {val}\n")


def _print_test_notify(cfg) -> None:
    from .models import ApplicationRecord
    from .notify import Notifier

    notifier = Notifier(cfg)
    if not notifier.enabled():
        print("No channels enabled. Set notify.email.enabled or notify.slack.enabled.")
        return
    sample = ApplicationRecord(
        uid="test:1", company="Test Co", title="Senior UX Designer",
        url="https://example.com/job/1", score=0.92, status="needs_review",
        note="this is a jobagent test notification",
    )
    print("Sending a test notification...\n")
    for line in notifier.notify([sample]):
        print(f"  {line}")


def _print_test_llm(cfg) -> None:
    provider = selected_provider(cfg.llm)
    print(f"LLM provider: {provider}")
    if provider == "off":
        print("No LLM credentials found or provider is off.")
        print("Set ANTHROPIC_API_KEY for Claude or OPENAI_API_KEY for OpenAI/Codex.")
        return

    out = complete(
        "Reply with exactly: jobagent llm ok",
        cfg.llm,
        max_tokens=20,
    )
    if out:
        print(f"Response: {out}")
    else:
        print("Provider was selected, but the test request returned no output.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobagent")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in (
        "setup", "scan", "run", "review-queue", "slack-panel", "status",
        "answers", "holds", "coverage", "ashby-checklist", "probe-ashby",
        "approval-bot", "approval-web", "approval-web-link", "test-notify",
        "test-llm",
    ):
        p = sub.add_parser(name)
        p.add_argument("--config", required=True)
        if name == "setup":
            p.add_argument("--host", default="127.0.0.1",
                           help="bind address for the setup wizard "
                                "(localhost only by default)")
            p.add_argument("--port", type=int, default=0,
                           help="wizard port; default 0 picks a free one")
            p.add_argument("--no-browser", action="store_true",
                           help="do not auto-open the wizard in a browser")
        if name == "probe-ashby":
            p.add_argument("--url", required=True,
                           help="a single Ashby application URL to probe "
                                "(attended, never auto-submits)")
        if name == "run":
            p.add_argument("--llm", action="store_true",
                           help="use LLM cover letters for this run")
        if name == "review-queue":
            p.add_argument("--limit", type=int, default=None,
                           help="max candidates to send; default uses config cap")
            p.add_argument("--interactive", action="store_true",
                           help="include Slack Approve/Skip buttons")
        if name == "approval-bot":
            p.add_argument("--queue", default="",
                           help="review queue JSON path; default uses latest")
            p.add_argument("--post-panel", action="store_true",
                           help="post the Slack control panel before listening")
        if name in {"approval-web", "approval-web-link"}:
            p.add_argument("--host", default="",
                           help="host to bind or publish; default uses "
                                "approval_web.host from config "
                                "(127.0.0.1 unless configured)")
            p.add_argument("--port", type=int, default=0,
                           help="approval web port; default uses "
                                "approval_web.port from config")
        if name == "approval-web":
            p.add_argument("--post-link", action="store_true",
                           help="post the phone approval link to Slack on startup")

    args = parser.parse_args(argv)
    _load_local_env()

    # `setup` runs BEFORE config loading: a fresh user has no config.yaml yet
    # (and an incomplete one is exactly why they are here).
    if args.command == "setup":
        from .setup_wizard import run_setup_wizard
        run_setup_wizard(
            config_path=args.config,
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
        )
        return 0

    if not Path(args.config).exists():
        print(f"No config file at {args.config!r}.\n\n"
              "First time here? Create one with the setup wizard:\n"
              f"  jobagent setup --config {args.config}\n\n"
              "Or copy the documented example and edit it:\n"
              f"  cp config.example.yaml {args.config}")
        return 2

    cfg = load_config(args.config)
    _configure_logging(cfg)

    # Personalization gate: these commands generate or send application
    # material, which is meaningless (or dangerous) with a blank profile.
    if args.command in {"scan", "run", "review-queue"}:
        gaps = profile_gaps(cfg)
        if gaps:
            print("Your profile is not set up yet — jobagent cannot "
                  "personalize applications:\n")
            for g in gaps:
                print(f"  - {g}")
            print("\nRun the setup wizard to fix this:\n"
                  f"  jobagent setup --config {args.config}")
            return 2

    if args.command == "scan":
        _print_scan(cfg)
    elif args.command == "run":
        _print_run(cfg, use_llm=getattr(args, "llm", False))
    elif args.command == "review-queue":
        _print_review_queue(
            cfg,
            limit=getattr(args, "limit", None),
            interactive=getattr(args, "interactive", False),
        )
    elif args.command == "slack-panel":
        _print_slack_panel(cfg)
    elif args.command == "approval-bot":
        if getattr(args, "post_panel", False):
            send_control_panel(cfg)
        run_approval_bot(cfg, queue_path=getattr(args, "queue", "") or None)
    elif args.command == "approval-web":
        run_approval_web(
            cfg,
            host=getattr(args, "host", "") or None,
            port=getattr(args, "port", 0) or None,
            post_link=getattr(args, "post_link", False),
        )
    elif args.command == "approval-web-link":
        _print_approval_web_link(
            cfg,
            host=getattr(args, "host", "") or cfg.approval_web.host,
            port=getattr(args, "port", 0) or cfg.approval_web.port,
        )
    elif args.command == "status":
        _print_status(cfg)
    elif args.command == "holds":
        _print_holds(cfg)
    elif args.command == "coverage":
        _print_coverage(cfg)
    elif args.command == "ashby-checklist":
        from .ashby_form import write_ashby_checklist
        path, n, gaps = write_ashby_checklist(cfg)
        print(f"Ashby manual-submit checklist written: {path}")
        print(f"  {n} roles, {gaps} field(s) flagged for you to fill.")
        print("  Open each link in your own browser and submit — Ashby flags "
              "automated submits.")
    elif args.command == "probe-ashby":
        from .probe import probe_ashby
        print("Attended Ashby probe — HEADED, no stealth, NEVER auto-submits. "
              "The bot fills and stops; you inspect and optionally click submit.")
        probe_ashby(cfg, args.url)
    elif args.command == "answers":
        _print_answers(cfg)
    elif args.command == "test-notify":
        _print_test_notify(cfg)
    elif args.command == "test-llm":
        _print_test_llm(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
