"""Read a one-time email verification code from the applicant's own inbox —
under three trust gates, because the inbox is UNTRUSTED input.

Some ATS boards (Oura's Greenhouse) email a code after you click submit; the
application is not complete until it is entered. Completing it is legitimate
(the applicant owns the inbox and authorizes each submit) — but anyone can send
an email that LOOKS like a security-code email, so a code is used only when it
clears all three gates. Principle: never trust, verify provenance.

  Gate 1 — sender allowlist: the email's From domain must be allowlisted for
    the ATS that triggered this poll (config verification.senders[ats]). An
    empty allowlist means "not calibrated yet" → the job HOLDS and asks for a
    real sample; we never extrapolate one ATS's sender to another.
  Gate 2 — anchored extraction: the code token must immediately follow a known
    anchor phrase ("security code", "copy and paste this code", ...), be 4-10
    alphanumeric chars (case preserved), and not be an English/email stopword.
    No whole-body token scanning.
  Gate 3 — ATS + time binding: the form-entry step (code_usable_for) uses a
    code only when its source ATS matches the form's ATS and its message
    timestamp falls inside the poll window (submit time → now, ± clock skew).

Why IMAP: the approval-bot is a long-running launchd process with no MCP, so it
reads the inbox itself over IMAP with a Gmail App Password. Stdlib only.
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
import time
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from .verification_data import DEFAULT_ANCHORS, STOPWORDS

logger = logging.getLogger(__name__)

# A code token: 4-10 alphanumeric characters, case preserved.
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_MIN_LEN, _MAX_LEN = 4, 10
# How far past an anchor to look for the code (chars).
_ANCHOR_WINDOW = 120


@dataclass
class VerificationCode:
    """The structured result of a gated extraction — never a bare string, so
    the form-entry gate can bind it to the right ATS and time window."""
    code: str            # extracted code, original case preserved
    ats: str             # source ATS (the poll that produced it)
    sender: str          # From header
    message_epoch: float  # email Date as epoch seconds
    extracted_epoch: float


# ---- gate 1: sender allowlist ---------------------------------------------
def _sender_domain(from_header: str) -> str:
    m = re.search(r"[\w.+-]+@([A-Za-z0-9.-]+)", from_header or "")
    return m.group(1).lower().rstrip(".") if m else ""


def sender_allowed(from_header: str, allowlist: list[str]) -> bool:
    """True iff the From domain equals, or is a subdomain of, an allowlisted
    domain. 'no-reply@us.greenhouse-mail.io' matches 'greenhouse-mail.io'."""
    dom = _sender_domain(from_header)
    if not dom or not allowlist:
        return False
    for raw in allowlist:
        a = (raw or "").strip().lower().lstrip("@").rstrip(".")
        if a and (dom == a or dom.endswith("." + a)):
            return True
    return False


# ---- gate 2: anchored extraction ------------------------------------------
def _has_anchor(text: str, anchors: list[str]) -> bool:
    low = text.lower()
    return any(a and a.lower() in low for a in anchors)


def extract_code_anchored(subject: str, body: str, anchors: list[str],
                          stopwords) -> str | None:
    """The code token that immediately follows the FIRST anchor phrase, within
    a bounded window. Case preserved. Returns None if no anchor matches, or the
    token after it is a stopword / not a valid code.

    Colon rule: if a colon separates the anchor from the code
    ("...on your application: sgBhg"), the connector words before the colon are
    skipped and stopwords AFTER the colon are stepped over to reach the code.
    Without a colon the code must be the first substantial token right after the
    anchor — a stopword there (\"your code is ready...\") yields None, so prose
    is never mined for a code.
    """
    anchors = anchors or DEFAULT_ANCHORS
    text = f"{subject or ''}\n{body or ''}"
    low = text.lower()
    # Every anchor occurrence, in position order. Trying each (not just the
    # earliest) lets a subject-line anchor whose window spills into body prose
    # fall through to the real anchor in the body.
    occ: list[tuple[int, int]] = []
    for a in anchors:
        al = (a or "").lower()
        if not al:
            continue
        start = 0
        while True:
            i = low.find(al, start)
            if i == -1:
                break
            occ.append((i, len(a)))
            start = i + 1
    occ.sort()

    for pos, alen in occ:
        window = text[pos + alen: pos + alen + _ANCHOR_WINDOW]
        after_colon = ":" in window
        region = window.split(":", 1)[1] if after_colon else window
        for tok in _TOKEN_RE.findall(region):
            if len(tok) < _MIN_LEN or len(tok) > _MAX_LEN:
                continue  # too short/long — not a candidate, keep scanning
            if tok.lower() in stopwords:
                if after_colon:
                    continue      # in the code region, step over connector words
                break             # no colon: first real token is a word -> next anchor
            return tok
    return None


# ---- gate 3: ATS + time-window binding ------------------------------------
def code_usable_for(vc: VerificationCode | None, form_ats: str,
                    since_epoch: float, now_epoch: float,
                    skew_seconds: int = 120) -> bool:
    """The form-entry gate: a code is usable only for the ATS that produced it
    and only if its message timestamp falls inside the poll window."""
    if vc is None:
        return False
    if vc.ats != form_ats:
        return False
    return (since_epoch - skew_seconds) <= vc.message_epoch <= (now_epoch + skew_seconds)


# ---- IMAP plumbing ---------------------------------------------------------
def _decoded(raw: str) -> str:
    try:
        return str(make_header(decode_header(raw or "")))
    except Exception:
        return raw or ""


def _plain_body(msg: email.message.Message) -> str:
    parts: list[str] = []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8",
                                  errors="ignore")
        except Exception:
            continue
        if ctype == "text/html":
            text = re.sub(r"<[^>]+>", " ", text)
        parts.append(text)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _connect(cfg):
    import os
    password = os.getenv(cfg.email_verify.password_env, "")
    if not password:
        logger.warning("email verification enabled but ${%s} is not set — "
                       "cannot read the inbox", cfg.email_verify.password_env)
        return None
    user = cfg.email_verify.imap_user or cfg.profile.email
    if not user:
        logger.warning("email verification: no imap_user / profile.email set")
        return None
    try:
        conn = imaplib.IMAP4_SSL(cfg.email_verify.imap_host)
        conn.login(user, password)
        return conn
    except Exception as exc:
        logger.warning("IMAP login failed for %s: %s", user, exc)
        return None


def _mask(code: str) -> str:
    return (code[0] + "*" * (len(code) - 1)) if code else ""


def _search_once(conn, ats: str, allowlist: list[str], anchors: list[str],
                 since_epoch: float, skew: int) -> VerificationCode | None:
    """One inbox pass: the newest email that clears gates 1 & 2 and the time
    window. Emits one audit line per polled email."""
    since_str = time.strftime("%d-%b-%Y", time.gmtime(since_epoch - 86400))
    try:
        conn.select("INBOX", readonly=True)
        typ, data = conn.search(None, "SINCE", since_str)
        if typ != "OK":
            return None
        ids = data[0].split()
    except Exception as exc:
        logger.warning("IMAP search failed: %s", exc)
        return None

    for mid in reversed(ids[-40:]):  # newest first
        try:
            typ, msg_data = conn.fetch(mid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
        except Exception:
            continue
        sender = _decoded(msg.get("From", ""))
        subject = _decoded(msg.get("Subject", ""))
        try:
            epoch = parsedate_to_datetime(msg.get("Date")).timestamp()
        except Exception:
            epoch = 0.0
        subj80 = subject[:80]

        # gate 3 (time) — cheap, first
        if epoch and epoch < since_epoch - skew:
            logger.info("verify poll: from=%r subject=%r -> rejected:stale",
                        sender, subj80)
            continue
        # gate 1 (sender allowlist)
        if not sender_allowed(sender, allowlist):
            logger.info("verify poll: from=%r subject=%r -> "
                        "skipped:sender_not_allowlisted", sender, subj80)
            continue
        # gate 2 (anchored extraction)
        body = _plain_body(msg)
        code = extract_code_anchored(subject, body, anchors, STOPWORDS)
        if code:
            logger.info("verify poll: from=%r subject=%r -> extracted:ok "
                        "ats=%s code=%s", sender, subj80, ats, _mask(code))
            logger.debug("verify poll: full code for %r = %s", subj80, code)
            return VerificationCode(code=code, ats=ats, sender=sender,
                                    message_epoch=epoch, extracted_epoch=time.time())
        outcome = ("rejected:stopword" if _has_anchor(f"{subject}\n{body}", anchors)
                   else "skipped:no_anchor")
        logger.info("verify poll: from=%r subject=%r -> %s",
                    sender, subj80, outcome)
    return None


def fetch_verification_code(
    cfg,
    ats: str,
    since_epoch: float,
    timeout_s: int | None = None,
    poll_s: int = 5,
) -> VerificationCode | None:
    """Poll the inbox for a verification code for `ats` that clears gates 1 & 2
    and arrived within the window. Returns a VerificationCode or None. The
    caller applies gate 3 (code_usable_for) before entering the code."""
    if not getattr(cfg.email_verify, "enabled", False):
        return None
    allowlist = list((cfg.verification.senders or {}).get(ats) or [])
    if not allowlist:
        # No calibrated sender for this ATS — do not guess. Caller holds with a
        # "configure verification.senders" note.
        logger.warning("verify: no sender allowlist for ats=%r — holding", ats)
        return None
    anchors = list(cfg.verification.anchors or []) or DEFAULT_ANCHORS
    skew = int(getattr(cfg.verification, "clock_skew_seconds", 120))
    conn = _connect(cfg)
    if conn is None:
        return None
    deadline = time.time() + (timeout_s or cfg.email_verify.timeout_seconds)
    try:
        while True:
            vc = _search_once(conn, ats, allowlist, anchors, since_epoch, skew)
            if vc is not None:
                return vc
            if time.time() >= deadline:
                logger.warning("no allowlisted verification code for ats=%r "
                               "arrived within the window", ats)
                return None
            time.sleep(poll_s)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
