"""HTTP-level tests for the setup wizard server (no browser needed).

Run from the repo root:
    python3 scripts/test_setup_wizard_http.py
"""
from __future__ import annotations

import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobagent.config import load_config  # noqa: E402
from jobagent.setup_wizard import _make_server  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"[{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        failures.append(name)


def get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def post(url: str, fields: dict) -> tuple[int, str]:
    data = urllib.parse.urlencode(fields, doseq=True).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


with tempfile.TemporaryDirectory() as td:
    cfg_path = Path(td) / "config.yaml"
    server, token, url = _make_server(str(cfg_path), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = url.rsplit("?", 1)[0]  # http://127.0.0.1:PORT/setup

    try:
        # Step page renders.
        code, body = get(f"{base}?token={token}&step=0")
        check("GET step 0 -> 200", code == 200)
        check("step 0 shows Basics", "Basics" in body)
        check("persona appears only as placeholder",
              'value="Alex' not in body and 'placeholder="Alex' in body)

        # Wrong token is rejected.
        code, _ = get(f"{base}?token=WRONG&step=0")
        check("wrong token -> 403", code == 403)

        # Missing required fields re-render with errors, no advance.
        code, body = post(f"{base}?token={token}&step=0",
                          {"first_name": "Sam", "last_name": "", "email": "",
                           "phone": ""})
        check("invalid step re-renders with errors", code == 200
              and "invalid" in body and "Basics" in body)

        # Valid steps advance (303 redirect to the next step).
        steps_payloads = [
            {"first_name": "Sam", "last_name": "Doe",
             "email": "sam.doe@testmail.local", "phone": "+1 555 010 0199"},
            {"auth_status": "yes", "sponsorship": "no", "auth_notes": ""},
            {"titles": "Product Designer", "exclude_keywords": "",
             "remote_ok": "yes", "locations": "",
             "sources": ["greenhouse"], "source_tokens": "greenhouse:demo"},
            {"resume_default": "resume.pdf"},
            {"family_0_name": "product_designer",
             "family_0_keywords": "",
             "family_0_identity": "I am a product designer.",
             "family_0_craft": "I build to think.",
             "family_0_closing_mix": "craft and judgment",
             "family_0_marker": "product designer",
             "project_0_keywords": "ai",
             "project_0_text": "Shipped an AI app."},
            {"facts_background": "Designer.", "facts_projects": "",
             "facts_standard": ""},
        ]
        for i, payload in enumerate(steps_payloads):
            code, body = post(f"{base}?token={token}&step={i}", payload)
            check(f"step {i} accepts valid POST", code == 200
                  and "invalid" not in body)

        # Finish writes both files.
        code, body = post(base.replace("/setup", "/finish") + f"?token={token}", {})
        check("finish -> 200 done page", code == 200 and "config.yaml" in body)
        check("config.yaml written", cfg_path.exists())
        check("answers.md written", (Path(td) / "answers.md").exists())

        cfg = load_config(cfg_path)
        check("server round-trip: name", cfg.profile.name == "Sam Doe")
        check("server round-trip: family",
              cfg.coverletter.families["product_designer"].craft
              == "I build to think.")
        check("server round-trip: source",
              [(s.kind, s.token) for s in cfg.sources] == [("greenhouse", "demo")])
    finally:
        server.shutdown()

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    sys.exit(1)
print("all setup wizard http tests passed")
