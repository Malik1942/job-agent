"""Unit tests for the three verification-code security gates:

  Gate 1 (sender_allowed):     the code email must come from an allowlisted
                                domain for the ATS being polled.
  Gate 2 (extract_code_anchored): the code token must sit immediately after
                                a known instructional anchor phrase, be
                                4-10 alphanumeric chars, and not be an
                                English stopword.
  Gate 3 (code_usable_for):    a found code may only be used for the ATS
                                that produced it, and only within a tight
                                time window around the poll.

No network — IMAP is monkeypatched with a fake server, same as
test_email_verify.py.

Run from the repo root:
    .venv/bin/python scripts/test_verification_gates.py
"""

from __future__ import annotations

import os
import sys
import time
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jobagent import email_verify                                  # noqa: E402
from jobagent.config import EmailVerify, Profile, Verification     # noqa: E402
from jobagent.email_verify import (                                # noqa: E402
    VerificationCode,
    code_usable_for,
    extract_code_anchored,
    fetch_verification_code,
    sender_allowed,
)
from jobagent.verification_data import DEFAULT_ANCHORS, STOPWORDS  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"[{'ok' if cond else 'FAIL'}] {name}")
    if not cond:
        failures.append(name)


# --- fake IMAP server (same shape as test_email_verify.py) -------------------
def _mk(sender, subject, body, epoch):
    m = EmailMessage()
    m["From"] = sender
    m["Subject"] = subject
    m["Date"] = time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(epoch))
    m.set_content(body)
    return m.as_bytes()


class FakeIMAP:
    store: list[tuple[bytes, bytes]] = []  # (id, rfc822)

    def __init__(self, host):
        self.host = host

    def login(self, user, pw):
        return ("OK", [b""])

    def select(self, mailbox, readonly=False):
        return ("OK", [b"1"])

    def search(self, charset, *criteria):
        return ("OK", [b" ".join(mid for mid, _ in FakeIMAP.store)])

    def fetch(self, mid, spec):
        for i, raw in FakeIMAP.store:
            if i == mid:
                return ("OK", [(b"1 (RFC822)", raw)])
        return ("NO", [None])

    def logout(self):
        return ("BYE", [b""])


def make_cfg(store, senders=None):
    FakeIMAP.store = store
    email_verify.imaplib.IMAP4_SSL = lambda host: FakeIMAP(host)

    class _Cfg:
        email_verify = EmailVerify(enabled=True, imap_user="alex.rivera@example.com")
        profile = Profile(email="alex.rivera@example.com")
        verification = Verification(
            senders=senders if senders is not None else {},
            anchors=[],
            clock_skew_seconds=120,
        )
    return _Cfg()


os.environ["JOBAGENT_IMAP_PASSWORD"] = "x"

now = time.time()

GREENHOUSE_SENDERS = {"greenhouse": ["greenhouse-mail.io", "greenhouse.io"]}


# ==============================================================================
# GATE 2: extract_code_anchored — POSITIVE cases
# ==============================================================================

check(
    "gate2 positive: Greenhouse alnum lower/mixed code exact case",
    extract_code_anchored(
        "Security code for your application",
        "Hi Alex, Copy and paste this code into the security code field on "
        "your application: sgBhg. This code expires in 30 minutes.",
        DEFAULT_ANCHORS, STOPWORDS,
    ) == "sgBhg",
)

check(
    "gate2 positive: Greenhouse alnum code with digit",
    extract_code_anchored(
        "Security code for your application",
        "Copy and paste this code into the security code field on your "
        "application: u08Fl",
        DEFAULT_ANCHORS, STOPWORDS,
    ) == "u08Fl",
)

check(
    "gate2 positive: numeric OTP",
    extract_code_anchored(
        "Your verification code",
        "Your verification code is 971868",
        DEFAULT_ANCHORS, STOPWORDS,
    ) == "971868",
)

check(
    "gate2 positive: case preservation on mixed-case code (not lowercased)",
    extract_code_anchored(
        "Your code",
        "Your verification code: aB3xY to continue.",
        DEFAULT_ANCHORS, STOPWORDS,
    ) == "aB3xY",
)


# ==============================================================================
# GATE 2: extract_code_anchored — NEGATIVE cases
# ==============================================================================

check(
    "gate2 negative (4a): no anchor phrase present -> None",
    extract_code_anchored(
        "Thanks",
        "Thank you so much, hello there, we received it",
        DEFAULT_ANCHORS, STOPWORDS,
    ) is None,
)

check(
    "gate2 negative (4c): stopword 'ready' immediately after anchor -> None",
    extract_code_anchored(
        "Your code",
        "Your code is ready to use shortly",
        DEFAULT_ANCHORS, STOPWORDS,
    ) is None,
)

check(
    "gate2 negative (4c): stopword 'please' immediately after anchor -> None",
    extract_code_anchored(
        "Security code",
        "security code: please",
        DEFAULT_ANCHORS, STOPWORDS,
    ) is None,
)

check(
    "gate2 negative: a year in prose with no anchor -> None",
    extract_code_anchored("Newsletter", "Copyright 2026 Company. All rights.",
                          DEFAULT_ANCHORS, STOPWORDS) is None,
)

check(
    "gate2 negative: an order number with no anchor -> None",
    extract_code_anchored("Your order", "Order #1216937381493907 confirmed.",
                          DEFAULT_ANCHORS, STOPWORDS) is None,
)


# ==============================================================================
# GATE 1: sender_allowed
# ==============================================================================

check(
    "gate1: allowlisted Greenhouse sender domain -> True",
    sender_allowed(
        "Greenhouse <no-reply@us.greenhouse-mail.io>",
        ["greenhouse-mail.io", "greenhouse.io"],
    ) is True,
)

check(
    "gate1: non-allowlisted sender domain -> False",
    sender_allowed(
        "Scammer <no-reply@evil.com>",
        ["greenhouse-mail.io"],
    ) is False,
)


# --- (4b) gate 1 enforced inside fetch_verification_code --------------------
# A store with ONE non-allowlisted-but-otherwise-valid email -> blocked at
# gate 1, fetch returns None even though extraction would have succeeded.
cfg = make_cfg(
    [
        (b"1", _mk(
            "Scammer <no-reply@evil.com>",
            "Security code for your application",
            "Copy and paste this code into the security code field on your "
            "application: sgBhg",
            now,
        )),
    ],
    senders=GREENHOUSE_SENDERS,
)
result = fetch_verification_code(cfg, "greenhouse", since_epoch=now - 30,
                                 timeout_s=1, poll_s=0)
check(
    "gate1 (4b): non-allowlisted sender is blocked before extraction, "
    "fetch returns None",
    result is None,
)

# Prove it's really gate 1 doing the blocking: same scenario but with a SECOND,
# allowlisted email carrying a DIFFERENT code -> fetch must return the
# allowlisted one, proving the non-allowlisted email was skipped rather than
# accidentally read.
cfg = make_cfg(
    [
        (b"1", _mk(
            "Scammer <no-reply@evil.com>",
            "Security code for your application",
            "Copy and paste this code into the security code field on your "
            "application: sgBhg",
            now,
        )),
        (b"2", _mk(
            "Greenhouse <no-reply@us.greenhouse-mail.io>",
            "Security code for your application",
            "Copy and paste this code into the security code field on your "
            "application: u08Fl",
            now,
        )),
    ],
    senders=GREENHOUSE_SENDERS,
)
result = fetch_verification_code(cfg, "greenhouse", since_epoch=now - 30,
                                 timeout_s=1, poll_s=0)
check(
    "gate1 (4b): allowlisted email wins over non-allowlisted email with a "
    "different code",
    result is not None and result.code == "u08Fl",
)


# ==============================================================================
# GATE 3: code_usable_for — ATS binding + time window
# ==============================================================================

valid_vc = VerificationCode(
    code="sgBhg",
    ats="greenhouse",
    sender="Greenhouse <no-reply@us.greenhouse-mail.io>",
    message_epoch=now,
    extracted_epoch=now,
)

check(
    "gate3: ATS mismatch (form_ats='lever') refuses a Greenhouse code",
    code_usable_for(valid_vc, "lever", since_epoch=now - 30, now_epoch=now,
                    skew_seconds=120) is False,
)

check(
    "gate3: matching ATS + message_epoch inside window -> True",
    code_usable_for(valid_vc, "greenhouse", since_epoch=now - 30, now_epoch=now,
                    skew_seconds=120) is True,
)

stale_vc = VerificationCode(
    code="sgBhg",
    ats="greenhouse",
    sender="Greenhouse <no-reply@us.greenhouse-mail.io>",
    message_epoch=now - 3600,  # well before since_epoch - skew
    extracted_epoch=now,
)

check(
    "gate3 (4d): stale message_epoch (well before since - skew) -> False",
    code_usable_for(stale_vc, "greenhouse", since_epoch=now - 30, now_epoch=now,
                    skew_seconds=120) is False,
)


# --- (4d) staleness enforced inside fetch_verification_code ------------------
# The only matching email has a Date well BEFORE the poll window opened ->
# fetch must return None.
cfg = make_cfg(
    [
        (b"1", _mk(
            "Greenhouse <no-reply@us.greenhouse-mail.io>",
            "Security code for your application",
            "Copy and paste this code into the security code field on your "
            "application: sgBhg",
            now - 3600,
        )),
    ],
    senders=GREENHOUSE_SENDERS,
)
result = fetch_verification_code(cfg, "greenhouse", since_epoch=now - 30,
                                 timeout_s=1, poll_s=0)
check(
    "gate3 (4d): via fetch_verification_code, a stale pre-window email is "
    "NOT used",
    result is None,
)


# ==============================================================================
# Connection guards: the feature must degrade to a hold, never crash.
# ==============================================================================
good = [(b"1", _mk("Greenhouse <no-reply@us.greenhouse-mail.io>",
                   "Security code for your application",
                   "Copy and paste this code into the security code field on "
                   "your application: sgBhg", now))]

cfg = make_cfg(good, senders=GREENHOUSE_SENDERS)
cfg.email_verify.enabled = False
check("guard: email_verify disabled -> None",
      fetch_verification_code(cfg, "greenhouse", since_epoch=now - 30,
                              timeout_s=1, poll_s=0) is None)

cfg = make_cfg(good, senders=GREENHOUSE_SENDERS)
os.environ.pop("JOBAGENT_IMAP_PASSWORD", None)
check("guard: missing IMAP app password -> None (no crash)",
      fetch_verification_code(cfg, "greenhouse", since_epoch=now - 30,
                              timeout_s=1, poll_s=0) is None)
os.environ["JOBAGENT_IMAP_PASSWORD"] = "x"

# Gate 1, uncalibrated ATS: an empty allowlist must never poll/guess.
cfg = make_cfg(good, senders={"greenhouse": ["greenhouse-mail.io"], "lever": []})
check("gate1: empty allowlist for the polled ATS -> None (hold, no guess)",
      fetch_verification_code(cfg, "lever", since_epoch=now - 30,
                              timeout_s=1, poll_s=0) is None)


if failures:
    print(f"\nFAILED: {', '.join(failures)}")
    raise SystemExit(1)
print("\nAll verification-gate tests passed.")
