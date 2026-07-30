"""Phone-friendly approval web panel.

This is a fallback when Slack interactive buttons are unreliable. Slack only
sends a link; the actual Approve / Skip buttons are normal HTML forms served by
this local process. Use a random token in the URL so casual LAN traffic cannot
trigger submissions.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import socket
import threading
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

from .config import Config
from .pipeline import scan
from .review_queue import build_review_items, write_review_queue
from .slack_approval import (
    approve_all_queue_items,
    approve_queue_item,
    batch_stop_requested,
    confirm_all_queue_items,
    confirm_queue_item,
    latest_queue_path,
    pending_queue_item_ids,
    pending_submit_item_ids,
    post_submission_report,
    prepare_all_queue_items,
    prepare_queue_item,
    queue_status_text,
    request_batch_stop,
    skip_all_queue_items,
    skip_queue_item,
)

TOKEN_FILE = Path("output/approval-web-token")
_TASKS: dict[str, str] = {}
_TASK_LOCK = threading.Lock()


def approval_token(rotate: bool = False) -> str:
    env_token = os.getenv("JOBAGENT_APPROVAL_WEB_TOKEN", "")
    if env_token:
        return env_token
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists() and not rotate:
        return TOKEN_FILE.read_text().strip()
    tok = token_urlsafe(24)
    TOKEN_FILE.write_text(tok)
    TOKEN_FILE.chmod(0o600)
    return tok


def local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def approval_url(host: str, port: int, token: str) -> str:
    public_host = local_ip() if host in {"0.0.0.0", "::"} else host
    return f"http://{public_host}:{port}/?token={quote(token)}"


def create_queue(cfg: Config, limit: int) -> Path:
    result = scan(cfg)
    items = build_review_items(cfg, result.jobs, limit=limit)
    manual_only = [
        j for j in result.jobs
        if j.score >= cfg.autonomy.min_score and j.scan_only
    ]
    return write_review_queue(cfg, items, manual_only)


def _queue_path(cfg: Config, value: str = "") -> Path:
    if value:
        return Path(unquote(value))
    try:
        return latest_queue_path(cfg)
    except FileNotFoundError:
        return create_queue(cfg, 5)


def _load_queue(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _task(name: str, fn) -> None:  # type: ignore[no-untyped-def]
    with _TASK_LOCK:
        _TASKS[name] = "running"
    try:
        fn()
        with _TASK_LOCK:
            _TASKS[name] = "done"
    except Exception as exc:
        with _TASK_LOCK:
            _TASKS[name] = f"error: {exc}"


def _start_task(name: str, fn) -> str:  # type: ignore[no-untyped-def]
    with _TASK_LOCK:
        current = _TASKS.get(name, "")
        if current == "running":
            return f"{name} already running"
        _TASKS[name] = "queued"
    threading.Thread(target=_task, args=(name, fn), daemon=True).start()
    return f"{name} started"


def _approve_one_and_report(cfg: Config, queue: Path, item_id: str) -> None:
    if cfg.autonomy.two_phase_approval:
        prepare_queue_item(cfg, queue, item_id)
        print(post_submission_report(
            cfg, queue, [item_id], f"Prepare {item_id} (filled, NOT submitted)"))
    else:
        approve_queue_item(cfg, queue, item_id)
        print(post_submission_report(cfg, queue, [item_id], f"Approve {item_id}"))


def _confirm_one_and_report(cfg: Config, queue: Path, item_id: str) -> None:
    confirm_queue_item(cfg, queue, item_id)
    print(post_submission_report(cfg, queue, [item_id], f"Confirm {item_id}"))


def _approve_all_and_report(cfg: Config, queue: Path) -> None:
    item_ids = pending_queue_item_ids(queue)
    if cfg.autonomy.two_phase_approval:
        _, errors = prepare_all_queue_items(cfg, queue)
        print(post_submission_report(
            cfg, queue, item_ids, "Prepare All (filled, NOT submitted)", errors))
    else:
        _, errors = approve_all_queue_items(cfg, queue)
        print(post_submission_report(cfg, queue, item_ids, "Approve All", errors))


def _confirm_all_and_report(cfg: Config, queue: Path) -> None:
    item_ids = pending_submit_item_ids(queue)
    _, errors = confirm_all_queue_items(cfg, queue)
    print(post_submission_report(cfg, queue, item_ids, "Confirm All", errors))


def _button(label: str, action: str, token: str, queue: Path,
            extra: dict[str, str] | None = None,
            danger: bool = False) -> str:
    fields = {
        "token": token,
        "queue": str(queue),
        "action": action,
        **(extra or {}),
    }
    hidden = "\n".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in fields.items()
    )
    cls = "danger" if danger else "primary"
    return (
        f'<form method="post" action="/action" class="inline">{hidden}'
        f'<button class="{cls}" type="submit">{html.escape(label)}</button></form>'
    )


def render_page(cfg: Config, token: str, queue: Path, notice: str = "") -> str:
    payload = _load_queue(queue)
    items = payload.get("queue", [])
    live = bool(payload.get("live_submit", cfg.autonomy.live_submit))
    stop_requested = batch_stop_requested(queue)
    counts: dict[str, int] = {}
    for item in items:
        status = item.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1

    two_phase = cfg.autonomy.two_phase_approval
    rows = []
    for item in items:
        status = item.get("status", "pending_confirmation")
        iid = item.get("id", "")
        title = f"{item.get('company', '')} - {item.get('title', '')}"
        note = item.get("result_note", "")
        actions = ""
        if status == "pending_confirmation":
            label = "Approve (fill only)" if two_phase else "Approve"
            actions = (
                _button(label, "approve", token, queue, {"id": iid})
                + _button("Skip", "skip", token, queue, {"id": iid}, danger=True)
            )
        elif status == "pending_submit":
            actions = (
                _button("Submit for real", "confirm", token, queue, {"id": iid})
                + _button("Skip", "skip", token, queue, {"id": iid}, danger=True)
            )
        shot = str(item.get("screenshot") or "")
        shot_html = ""
        if shot:
            name = Path(shot).name
            shot_html = (
                f"<details><summary>Filled form screenshot</summary>"
                f"<img class='shot' src='/shot?token={quote(token)}"
                f"&f={quote(name)}' alt='filled form'></details>"
            )
        unanswered = list(item.get("unanswered_required") or [])
        unanswered_html = (
            "<p class='note'>Unanswered required: "
            + html.escape("; ".join(unanswered)) + "</p>"
        ) if unanswered else ""
        rows.append(
            "<section class='job'>"
            f"<div class='jobtop'><strong>{html.escape(iid)}</strong>"
            f"<span class='status'>{html.escape(status)}</span></div>"
            f"<h2>{html.escape(title)}</h2>"
            f"<p>Score: {float(item.get('score', 0.0)):.2f} | "
            f"{html.escape(item.get('location') or 'not listed')}</p>"
            f"<p><a href='{html.escape(item.get('url', ''))}'>Job link</a></p>"
            f"<p class='note'>{html.escape(note[:600])}</p>"
            f"{unanswered_html}"
            f"{shot_html}"
            f"<div class='actions'>{actions}</div>"
            "</section>"
        )

    task_lines = []
    with _TASK_LOCK:
        for name, status in sorted(_TASKS.items()):
            task_lines.append(f"<li><code>{html.escape(name)}</code>: {html.escape(status)}</li>")

    count_text = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "none"
    return f"""<!doctype html>
<html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<title>jobagent approval</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 18px; color: #111; }}
.banner {{ padding: 12px; border-radius: 8px; background: {'#ffe8e8' if live else '#edf7ee'}; margin-bottom: 12px; }}
.toolbar, .actions {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }}
.inline {{ display: inline; }}
button {{ border: 0; border-radius: 8px; padding: 11px 14px; font-size: 16px; }}
.primary {{ background: #111; color: white; }}
.danger {{ background: #d92d20; color: white; }}
.job {{ border: 1px solid #ddd; border-radius: 8px; padding: 12px; margin: 12px 0; }}
.jobtop {{ display: flex; justify-content: space-between; gap: 10px; }}
.status {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; color: #555; }}
.note {{ color: #555; white-space: pre-wrap; }}
.shot {{ max-width: 100%; border: 1px solid #ddd; border-radius: 8px; margin-top: 8px; }}
a {{ color: #0b5fff; }}
code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
</style></head><body>
<h1>jobagent approval</h1>
<div class="banner"><strong>{'LIVE SUBMIT ON' if live else 'DRY RUN ONLY'}</strong><br>
Queue: <code>{html.escape(str(queue))}</code><br>Counts: {html.escape(count_text)}
{"<br><strong>STOP REQUESTED</strong>: batch will stop after the current item." if stop_requested else ""}</div>
{"<p><strong>" + html.escape(notice) + "</strong></p>" if notice else ""}
<div class="toolbar">
{_button("Refresh Top 5", "refresh", token, queue, {"limit": "5"})}
{_button("Refresh Top 25", "refresh", token, queue, {"limit": "25"})}
{_button("Approve All (fill only)" if two_phase else "Approve All", "approve_all", token, queue)}
{_button(f"Confirm All: submit {counts.get('pending_submit', 0)}", "confirm_all", token, queue) if counts.get('pending_submit') else ""}
{_button("Stop Batch", "stop_batch", token, queue, danger=True)}
{_button("Skip All", "skip_all", token, queue, danger=True)}
</div>
<ul>{''.join(task_lines)}</ul>
{''.join(rows)}
</body></html>"""


class ApprovalHandler(BaseHTTPRequestHandler):
    cfg: Config
    token: str

    def _send_html(self, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, queue: Path, notice: str = "") -> None:
        loc = f"/?token={quote(self.token)}&queue={quote(str(queue))}"
        if notice:
            loc += f"&notice={quote(notice)}"
        self.send_response(303)
        self.send_header("Location", loc)
        self.end_headers()

    def _check_token(self, params: dict[str, list[str]]) -> bool:
        return (params.get("token") or [""])[0] == self.token

    def _send_screenshot(self, params: dict[str, list[str]]) -> None:
        # Serve screenshots by BASENAME only, resolved strictly inside the
        # configured screenshot dir, so no other path can be read.
        name = Path((params.get("f") or [""])[0]).name
        shot_dir = Path(self.cfg.apply.screenshot_dir).resolve()
        target = (shot_dir / name).resolve()
        if (not name or target.parent != shot_dir or not target.exists()
                or target.suffix.lower() != ".png"):
            self.send_error(404, "screenshot not found")
            return
        raw = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if not self._check_token(params):
            self.send_error(403, "bad token")
            return
        if parsed.path == "/shot":
            self._send_screenshot(params)
            return
        queue = _queue_path(self.cfg, (params.get("queue") or [""])[0])
        notice = (params.get("notice") or [""])[0]
        self._send_html(render_page(self.cfg, self.token, queue, notice))

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or 0)
        params = parse_qs(self.rfile.read(length).decode("utf-8"))
        if not self._check_token(params):
            self.send_error(403, "bad token")
            return
        queue = _queue_path(self.cfg, (params.get("queue") or [""])[0])
        action = (params.get("action") or [""])[0]
        item_id = (params.get("id") or [""])[0]

        if action == "refresh":
            limit = int((params.get("limit") or ["5"])[0])
            queue = create_queue(self.cfg, limit)
            self._redirect(queue, f"refreshed top {limit}")
        elif action == "approve" and item_id:
            msg = _start_task(
                f"approve {item_id}",
                lambda: _approve_one_and_report(self.cfg, queue, item_id),
            )
            self._redirect(queue, msg)
        elif action == "confirm" and item_id:
            msg = _start_task(
                f"confirm {item_id}",
                lambda: _confirm_one_and_report(self.cfg, queue, item_id),
            )
            self._redirect(queue, msg)
        elif action == "approve_all":
            msg = _start_task(
                "approve all",
                lambda: _approve_all_and_report(self.cfg, queue),
            )
            self._redirect(queue, msg)
        elif action == "confirm_all":
            msg = _start_task(
                "confirm all",
                lambda: _confirm_all_and_report(self.cfg, queue),
            )
            self._redirect(queue, msg)
        elif action == "stop_batch":
            msg = request_batch_stop(queue, "web Stop Batch button")
            self._redirect(queue, msg)
        elif action == "skip" and item_id:
            skip_queue_item(queue, item_id)
            self._redirect(queue, f"skipped {item_id}")
        elif action == "skip_all":
            skipped = skip_all_queue_items(queue)
            self._redirect(queue, f"skipped {skipped} pending item(s)")
        else:
            self._redirect(queue, "unknown action")

    def log_message(self, fmt: str, *args: Any) -> None:
        print(fmt % args)


def post_approval_link(cfg: Config, url: str) -> None:
    webhook = os.getenv(cfg.notify.slack.webhook_env, "")
    if not webhook:
        return
    text = (
        "*jobagent phone approval panel*\n"
        f"Open this link on your phone: {url}\n"
        "This avoids Slack button routing. Use the page buttons to approve/skip."
    )
    requests.post(webhook, json={"text": text}, timeout=20).raise_for_status()


def run_approval_web(cfg: Config, host: str | None = None,
                     port: int | None = None,
                     post_link: bool = False) -> None:
    host = host or cfg.approval_web.host
    port = port or cfg.approval_web.port
    tok = approval_token(rotate=cfg.approval_web.rotate_token)
    url = approval_url(host, port, tok)
    if post_link:
        post_approval_link(cfg, url)
    handler = type("ConfiguredApprovalHandler", (ApprovalHandler,), {
        "cfg": cfg,
        "token": tok,
    })
    print(f"Approval web panel: {url}")
    if host in {"0.0.0.0", "::"}:
        print("WARNING: bound to all interfaces — anyone on this network "
              "with the link can approve. Prefer 127.0.0.1 or a "
              "Tailscale/VPN IP (approval_web.host in config.yaml).")
    print(f"Safety: live_submit={cfg.autonomy.live_submit} | "
          f"two_phase_approval={cfg.autonomy.two_phase_approval} | "
          f"token {'rotated' if cfg.approval_web.rotate_token else 'persistent'}")
    ThreadingHTTPServer((host, port), handler).serve_forever()
