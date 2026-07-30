"""Fetch an Ashby posting's REAL application questions and resolve each against
the answer book — so the manual-submit checklist becomes a per-job crib sheet.

Ashby must be submitted by hand (its fraud detection flags any automated submit
— see the ashby-submit memory). So "bot does everything up to submit" means:
for each Ashby job, list its exact questions with the answer already filled in,
gaps flagged, resume + cover letter ready — the human opens the link in their
own browser and just transcribes + clicks submit.

The form comes from Ashby's public hosted-jobs GraphQL API (the same one the
embedded form uses), unauthenticated and read-only:
    POST https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting
This catches custom widgets (e.g. the ValueSelect "years of experience") that a
DOM scan misses.
"""

from __future__ import annotations

import logging

import requests

from .answers import AnswerBook

logger = logging.getLogger(__name__)

_API = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"
# Minimal query: just the application form. `field` is a JSON scalar (request
# it whole; its title/type/selectableValues live inside).
_QUERY = (
    "query ApiJobPosting($org: String!, $jid: String!) {"
    " jobPosting(organizationHostedJobsPageName: $org, jobPostingId: $jid) {"
    " id title applicationForm { sections { title fieldEntries {"
    " isRequired field } } } } }"
)


def _resolve_kind(atype: str) -> str:
    """Map an Ashby field type onto the AnswerBook.resolve field_type."""
    a = (atype or "").lower()
    if a == "boolean":
        return "select"          # Yes / No
    if a == "valueselect":
        return "select"          # a fixed option list
    if a == "file":
        return "file"
    if a == "longtext":
        return "textarea"
    return "text"                # String, Email, Phone, Location, Number, ...


def fetch_application_questions(org: str, job_id: str,
                               timeout: int = 20) -> list[dict]:
    """Every field on this posting's application form, in order:
    {section, label, atype, required, options}. Raises on network/HTTP error."""
    resp = requests.post(
        _API,
        json={"operationName": "ApiJobPosting", "query": _QUERY,
              "variables": {"org": org, "jid": job_id}},
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(f"Ashby GraphQL error: {data['errors'][:1]}")
    jp = (data.get("data") or {}).get("jobPosting") or {}
    out: list[dict] = []
    for sec in ((jp.get("applicationForm") or {}).get("sections") or []):
        for fe in (sec.get("fieldEntries") or []):
            f = fe.get("field") or {}
            atype = str(f.get("type") or "")
            if atype.lower() == "boolean":
                options = ["Yes", "No"]
            else:
                options = [str(o.get("label")) for o in (f.get("selectableValues") or [])
                           if o.get("label")]
            out.append({
                "section": str(sec.get("title") or ""),
                "label": str(f.get("title") or "").strip(),
                "atype": atype,
                "required": bool(fe.get("isRequired")),
                "options": options,
            })
    return out


def answer_sheet(org: str, job_id: str, book: AnswerBook,
                 resume_path: str, cover_pdf_path: str = "") -> list[dict]:
    """Each question resolved against the answer book, for the crib sheet.
    Rows: {section, label, atype, required, options, answer, source, gap}.
    `gap` is True when a REQUIRED field has no answer (fill it yourself)."""
    rows: list[dict] = []
    for q in fetch_application_questions(org, job_id):
        kind = _resolve_kind(q["atype"])
        answer, source = "", ""
        if kind == "file":
            nl = q["label"].lower()
            if "cover" in nl:
                answer, source = (cover_pdf_path or "(generate cover letter)"), "file"
            elif "resume" in nl or "cv" in nl or q["required"]:
                answer, source = resume_path, "file"
        else:
            r = book.resolve(q["label"], kind, q["options"] or None)
            if r is not None:
                answer, source = r.value, r.source
        gap = q["required"] and not answer
        rows.append({**q, "answer": answer, "source": source, "gap": gap})
    return rows


import re as _re


def _org_and_id(job) -> tuple[str, str]:
    """Pull the Ashby org slug + job id out of a jobs.ashbyhq.com URL."""
    m = _re.search(r"ashbyhq\.com/([^/?#]+)/([0-9a-fA-F-]{36})",
                   job.apply_url or job.url or "")
    org = m.group(1) if m else ""
    jid = job.external_id or (m.group(2) if m else "")
    return org, jid


def _sheet_lines(rows: list[dict]) -> list[str]:
    lines: list[str] = []
    section = None
    for r in rows:
        if r["section"] != section:
            section = r["section"]
            if section:
                lines.append(f"\n  **{section}**")
        star = " *" if r["required"] else ""
        label = r["label"]
        if r["source"] == "file":
            lines.append(f"  - {label}{star} → attach `{r['answer']}`")
        elif r["gap"]:
            hint = (f"pick one: {', '.join(r['options'])}" if r["options"]
                    else "fill this in yourself")
            lines.append(f"  - ⚠ {label}{star} → **{hint}**")
        elif r["answer"]:
            val = r["answer"] if len(r["answer"]) <= 220 else r["answer"][:217] + "…"
            lines.append(f"  - {label}{star} → {val}")
        else:
            lines.append(f"  - {label} (optional) → leave blank")
    return lines


def build_ashby_checklist(cfg) -> tuple[list[str], int, int]:
    """Scan Ashby jobs, fetch each real application form, and resolve every
    question into per-job crib-sheet markdown. Returns (lines, roles, gaps).
    Pure rendering — writes nothing; write_ashby_checklist puts it on disk."""
    from datetime import datetime

    from .apply import submit_mode
    from .coverletter import cover_letter_pdf_path, write_cover_letter
    from .pipeline import scan

    book = AnswerBook.from_file(cfg.profile.answers_path,
                               use_llm=cfg.apply.use_llm_for_answers,
                               llm_settings=cfg.llm)
    result = scan(cfg)
    # Same dedup as the review queue: a role you already submitted (or that
    # closed) must not reappear on the crib sheet as if it were still open.
    from .tracker import Tracker
    tracker = Tracker(cfg.db_path, cfg.csv_path, cfg.csv_statuses)
    decided = tracker.decided_uids()
    decided_urls = tracker.decided_urls()
    ashby = [j for j in result.jobs
             if j.source == "ashby" and not j.scan_only
             and j.score >= cfg.autonomy.min_score
             and submit_mode(j.source, cfg) == "manual"
             and j.uid not in decided
             and (j.url or "").rstrip("/").lower() not in decided_urls]
    ashby.sort(key=lambda j: -j.score)

    out = [f"# Ashby — submit these yourself ({len(ashby)} roles)\n",
           f"_Generated {datetime.now():%Y-%m-%d %H:%M}. Ashby flags automation as "
           "spam (re-confirmed 2026-07-09), so the bot does everything up to submit "
           "and you click submit. Open each link in a **normal browser tab you drive "
           "yourself** (or use Ashby's 'Autofill from resume' / a Simplify-style "
           "extension), transcribe the answers below, and submit._\n",
           "⚠ = a required field with no saved answer — you fill it in.\n"]

    total_gaps = 0
    for i, job in enumerate(ashby, 1):
        org, jid = _org_and_id(job)
        resume = cfg.profile.resume_for_title(job.title)
        try:
            write_cover_letter(job, cfg)
            cover_pdf = cover_letter_pdf_path(job, cfg)
        except Exception:
            cover_pdf = ""
        out.append(f"\n## {i}. {job.company} — {job.title}  ·  score {job.score:.2f}")
        out.append(f"- **Apply:** {job.apply_url or job.url}")
        out.append(f"- **Resume:** `{resume}`")
        if cover_pdf:
            out.append(f"- **Cover letter (if asked):** `{cover_pdf}`")
        try:
            rows = answer_sheet(org, jid, book, resume, cover_pdf)
        except Exception as exc:
            logger.warning("Ashby form fetch failed for %s (%s): %s",
                           job.company, job.title, exc)
            out.append("  - _(could not fetch this form's questions — open the "
                       "link and fill from answers.md)_")
            continue
        gaps = sum(1 for r in rows if r["gap"])
        total_gaps += gaps
        out.append(f"- **Answers** ({len(rows)} fields"
                   + (f", ⚠ {gaps} to fill yourself" if gaps else ", all covered ✓")
                   + "):")
        out.extend(_sheet_lines(rows))

    return out, len(ashby), total_gaps


def write_ashby_checklist(cfg) -> tuple[str, int, int]:
    """Build the per-job manual-submit crib sheet and write it to
    output/manual_submit_checklist.md. Returns (path, jobs, gaps)."""
    from pathlib import Path

    out, roles, gaps = build_ashby_checklist(cfg)
    path = Path(cfg.output_dir) / "manual_submit_checklist.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return str(path), roles, gaps

