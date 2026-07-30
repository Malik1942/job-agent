"""Per-job Greenhouse application questions, checked BEFORE any browser run.

Greenhouse's public board API exposes each posting's exact application form:
    GET boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true
returns `questions` and `location_questions` — each with a label, a required
flag, a field type, and (for selects) the exact option labels.

This is the systemic fix for "every company asks different questions": instead
of discovering an unanswerable required field mid-fill (the JA-004 State /
Bay-Area hold), the review queue diffs each job's REAL questions against the
AnswerBook and puts the gaps on the Slack card, so answers.md gets fixed
before Approve, not after a failed fill.

Static check only (LLM off, same policy as coverage.py): a would-be LLM answer
must never mask a real gap. `demographic_questions` / EEO compliance sections
are not checked — they are voluntary and the fill-time flow already handles
them. Everything here is best-effort: a fetch failure marks the item
unchecked, never blocks the queue.
"""

from __future__ import annotations

import logging

from .answers import AnswerBook
from .config import Config
from .sources.base import get_json

logger = logging.getLogger(__name__)

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}?questions=true"


def fetch_job_questions(token: str, job_id: str) -> list[dict]:
    """The posting's application questions, normalized to
    {label, required, type, options}. Includes location_questions (City/State
    live there when the board enables them)."""
    data = get_json(API.format(token=token, job_id=job_id))
    out: list[dict] = []
    for q in (data.get("questions") or []) + (data.get("location_questions") or []):
        fields = q.get("fields") or []
        ftype = str(fields[0].get("type") or "") if fields else ""
        options: list[str] = []
        for f in fields:
            for v in f.get("values") or []:
                lab = str(v.get("label") or "").strip()
                if lab:
                    options.append(lab)
        label = str(q.get("label") or "").strip()
        if not label:
            continue
        out.append({
            "label": label,
            "required": bool(q.get("required")),
            "type": ftype,
            "options": options,
        })
    return out


def uncovered_required(book: AnswerBook, questions: list[dict],
                       has_resume: bool = True,
                       has_cover_letter: bool = True) -> list[str]:
    """Which REQUIRED questions can the answers file not answer? File uploads
    are covered by the resume / generated cover letter; any other required
    upload (portfolio PDF, transcript) is always a gap — the filler never
    guesses which document to attach."""
    gaps: list[str] = []
    for q in questions:
        if not q.get("required"):
            continue
        label = str(q.get("label") or "")
        ftype = str(q.get("type") or "")
        # Hidden inputs (Longitude/Latitude on location-enabled boards) are
        # populated by the page's own JS when the visible location field is
        # picked — never human questions, never gaps.
        if ftype == "input_hidden":
            continue
        if ftype == "input_file":
            nl = label.lower()
            if "resume" in nl or "cv" in nl:
                if not has_resume:
                    gaps.append(f"{label} (no resume_path configured)")
            elif "cover" in nl:
                if not has_cover_letter:
                    gaps.append(label)
            else:
                gaps.append(f"{label} (file upload — attach manually)")
            continue
        kind = "select" if "select" in ftype else "text"
        if book.resolve(label, kind, q.get("options") or None) is None:
            gaps.append(label)
    return gaps


def _greenhouse_tokens(cfg: Config) -> dict[str, str]:
    """company label (casefolded) -> board token, for greenhouse sources."""
    return {
        (s.label or s.token).casefold(): s.token
        for s in cfg.sources if s.kind == "greenhouse" and s.token
    }


def annotate_review_items(cfg: Config, items: list) -> None:
    """Best-effort, in-place: for each greenhouse ReviewItem, fetch the real
    question list and record answer-coverage. Never raises — an API hiccup
    leaves the item unchecked rather than blocking the queue."""
    gh_tokens = _greenhouse_tokens(cfg)
    if not gh_tokens:
        return
    book = AnswerBook.from_file(cfg.profile.answers_path, use_llm=False)
    has_resume = cfg.profile.any_resume_configured
    for item in items:
        if item.source != "greenhouse" or not item.external_id:
            continue
        token = gh_tokens.get((item.company or "").casefold())
        if not token:
            continue
        try:
            questions = fetch_job_questions(token, item.external_id)
        except Exception as exc:
            logger.warning("question prefetch failed for %s (%s): %s",
                           item.id, item.company, exc)
            continue
        required = [q for q in questions if q.get("required")]
        item.required_question_count = len(required)
        item.uncovered_required = uncovered_required(
            book, questions, has_resume=has_resume)
        item.coverage_checked = True
        if item.uncovered_required:
            logger.info("coverage %s (%s): %d/%d required covered; gaps: %s",
                        item.id, item.company,
                        len(required) - len(item.uncovered_required),
                        len(required), "; ".join(item.uncovered_required))
