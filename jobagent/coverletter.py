"""Generate a role-specific cover letter package for a job.

Each application gets a text version for form textareas and a polished PDF for
cover-letter upload fields. All voice/positioning content comes from the
`coverletter:` section of config.yaml (see config.example.yaml or run
`jobagent setup`); the deterministic generator supplies only structure, so it
works even when no LLM provider is configured.
"""

from __future__ import annotations

import re
import textwrap
from datetime import datetime
from pathlib import Path

from .config import Config
from .llm import complete
from .models import Job


def _slug(text: str, sep: str = "-") -> str:
    chars = [c.lower() if c.isalnum() else sep for c in text]
    return re.sub(f"{re.escape(sep)}+", sep, "".join(chars)).strip(sep)[:80] or "job"


def _pdf_part(text: str) -> str:
    part = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return part[:80] or "Role"


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower())).strip()


def _role_matches(text: str, title: str) -> bool:
    ntext = _norm_text(text)
    ntitle = _norm_text(title)
    if not ntitle:
        return True
    if ntitle in ntext:
        return True
    stop = {
        "the", "and", "for", "with", "role", "remote", "new", "grad",
        "senior", "staff", "lead",
    }
    tokens = [
        t for t in ntitle.split()
        if len(t) >= 3 and t not in stop and not t.isdigit()
    ]
    if not tokens:
        return True
    needed = min(2, len(tokens))
    return sum(1 for t in tokens[:4] if t in ntext) >= needed


def _date_text() -> str:
    now = datetime.now()
    return f"{now.strftime('%B')} {now.day}, {now.year}"


class CoverLetterNotConfigured(RuntimeError):
    """No coverletter families in config — nothing to generate letters from."""


def _family(cfg: Config, title: str):
    """(name, CoverLetterFamily) for this job title. Keyword match in config
    order wins; otherwise the configured default family."""
    cl = cfg.coverletter
    if not cl.families:
        raise CoverLetterNotConfigured(
            "coverletter.families is not configured — run `jobagent setup` "
            "or copy the coverletter section from config.example.yaml")
    t = " ".join((title or "").casefold().split())
    for name, fam in cl.families.items():
        if name == cl.default_family:
            continue
        if any(k.casefold() in t for k in fam.keywords if k):
            return name, fam
    name = cl.default_family or next(iter(cl.families))
    return name, cl.families[name]


def role_family(title: str, cfg: Config) -> str:
    """Which letter family a job title belongs to (mirrors resume_variants
    selection: keyword match first, configured default otherwise)."""
    return _family(cfg, title)[0]


def _job_terms(job: Job, cfg: Config) -> str:
    """Up to three of the user's own skills that appear in the job text —
    the letter connects THEIR keywords to THIS job, no built-in taxonomy."""
    text = f"{job.title} {job.description}".lower()
    hits = [s for s in cfg.profile.skills if s and s.lower() in text]
    return ", ".join(hits[:3]) or "the problems this role owns"


def _project_paragraph(job: Job, cfg: Config) -> str:
    """Config project stories whose keywords appear in the job text (up to 3).
    No configured projects -> empty string; the template collapses cleanly."""
    projects = cfg.coverletter.projects
    if not projects:
        return ""
    text = f"{job.company} {job.title} {job.description}".lower()
    hits = [p.text for p in projects
            if any(k.lower() in text for k in p.keywords if k)]
    if not hits:
        hits = [projects[0].text]
    return " ".join(hits[:3])


def _company_angle(job: Job) -> str:
    return (
        f"What draws me to {job.company} is the chance to shape a product "
        "where strong design can make complex technology easier to use."
    )


def template_cover_letter(job: Job, cfg: Config) -> str:
    p = cfg.profile
    _, fam = _family(cfg, job.title)
    role_terms = _job_terms(job, cfg)
    project_text = _project_paragraph(job, cfg)
    site = p.website.replace("https://", "").replace("http://", "").strip("/")
    portfolio_line = (
        f" My portfolio at {site} shows the work in more detail, and I"
        if site else " I"
    )

    middle = f"{project_text} {fam.craft}".strip()
    body = f"""\
Dear {job.company} Hiring Team,

I am excited to apply for the {job.title} role. {_company_angle(job)}

{fam.identity} For this role, I see a strong connection to {role_terms}.

{middle}

I would bring that mix of {fam.closing_mix} to {job.company}.{portfolio_line} would be glad to discuss how this background could support the {job.title} team.

Warmly,
{p.name}
"""
    return textwrap.dedent(body).strip() + "\n"


def llm_cover_letter(job: Job, cfg: Config) -> str:
    """Optional LLM path. Fails soft to the template if no provider is ready."""
    _, fam = _family(cfg, job.title)
    projects = "; ".join(p.text for p in cfg.coverletter.projects) or "none provided"
    site = cfg.profile.website or "not provided"
    prompt = (
        f"Write a natural, confident, specific cover letter for {cfg.profile.name} "
        f"applying to {job.company} for the role {job.title}.\n\n"
        "Length: 250-400 words. Tone: warm, direct, not corporate, not desperate, "
        "not AI-generated sounding.\n\n"
        f"Positioning (match this framing for this role family): {fam.identity}\n"
        f"The letter MUST include the phrase \"{fam.marker}\" naturally.\n\n"
        f"Project stories to use only when they fit the role: {projects}. "
        f"Portfolio: {site}.\n\n"
        f"Candidate summary: {cfg.profile.summary}\n"
        f"Skills: {', '.join(cfg.profile.skills)}\n"
        f"Job description excerpt: {job.description[:1800]}\n\n"
        "Do not invent facts, metrics, or company details. Do not mention every "
        "project; choose the strongest fit. Return only the letter body with a "
        "salutation and signature."
    )
    text = complete(
        prompt,
        cfg.llm,
        max_tokens=getattr(cfg.llm, "cover_letter_max_tokens", 600),
    )
    return (text.strip() + "\n") if text else template_cover_letter(job, cfg)


def _name_part(cfg: Config) -> str:
    return _pdf_part(cfg.profile.name or "Applicant")


def _pdf_path(job: Job, cfg: Config) -> Path:
    out_dir = Path(cfg.output_dir) / "cover_letters"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / (
        f"{_name_part(cfg)}_Cover_Letter_{_pdf_part(job.company)}_"
        f"{_pdf_part(job.title)}.pdf"
    )


def _text_path(job: Job, cfg: Config) -> Path:
    out_dir = Path(cfg.output_dir) / "cover_letters"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{_slug(job.company)}-{_slug(job.title)}.txt"


def write_cover_letter_pdf(job: Job, cfg: Config, body: str) -> str:
    try:
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    except ImportError as exc:
        raise RuntimeError(
            "reportlab is required for cover letter PDF export. "
            "Run: python -m pip install reportlab"
        ) from exc

    p = cfg.profile
    path = _pdf_path(job, cfg)
    doc = SimpleDocTemplate(
        str(path),
        pagesize=LETTER,
        rightMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        topMargin=0.65 * inch,
        bottomMargin=0.65 * inch,
    )
    styles = getSampleStyleSheet()
    normal = ParagraphStyle(
        "LetterBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=10.5,
        leading=15,
        alignment=TA_LEFT,
        spaceAfter=8,
    )
    header = ParagraphStyle(
        "LetterHeader",
        parent=normal,
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=18,
        spaceAfter=4,
    )
    small = ParagraphStyle(
        "LetterSmall",
        parent=normal,
        fontSize=9.5,
        leading=13,
        spaceAfter=3,
    )

    contact = " | ".join(
        x for x in (
            p.email,
            p.phone,
            f"LinkedIn: {p.linkedin}" if p.linkedin else "",
            f"Portfolio: {p.website}" if p.website else "",
        ) if x
    )
    story = [
        Paragraph(p.name or "Applicant", header),
        Paragraph(contact, small),
        Spacer(1, 0.16 * inch),
        Paragraph(_date_text(), normal),
        Paragraph(job.company, normal),
        Paragraph(f"Role: {job.title}", normal),
        Spacer(1, 0.1 * inch),
    ]
    for para in body.strip().split("\n\n"):
        story.append(Paragraph(para.replace("\n", "<br/>"), normal))
    doc.build(story)
    return str(path)


def cover_letter_pdf_path(job: Job, cfg: Config) -> str:
    return str(_pdf_path(job, cfg))


def ensure_cover_letter_pdf(job: Job, cfg: Config, text_path: str) -> str:
    path = Path(text_path)
    if not path.exists():
        body = template_cover_letter(job, cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    else:
        body = path.read_text(encoding="utf-8", errors="ignore")
    pdf = _pdf_path(job, cfg)
    # Regenerate from the current text every time this is called. The PDF is
    # what gets uploaded, so it must never lag behind or contain another role.
    write_cover_letter_pdf(job, cfg, body)
    return str(pdf)


def validate_cover_letter_package(
    job: Job,
    cfg: Config,
    text_path: str,
    pdf_path: str | None = None,
) -> list[str]:
    """Return validation problems for the current job's cover-letter package.

    This intentionally runs before any upload/submit path. A stale Figma letter
    attached to an OpenAI application should be impossible to submit.
    """
    errors: list[str] = []
    txt = Path(text_path) if text_path else None
    if not txt or not txt.exists():
        return ["cover letter text file missing"]

    body = txt.read_text(encoding="utf-8", errors="ignore")
    nbody = _norm_text(body)
    company = _norm_text(job.company)
    if company and company not in nbody:
        errors.append(f"cover letter does not mention company {job.company!r}")
    if not _role_matches(body, job.title):
        errors.append(f"cover letter does not match role {job.title!r}")
    site = cfg.profile.website.replace("https://", "").replace(
        "http://", "").strip("/").lower()
    if site and site not in body.lower():
        errors.append(f"cover letter does not mention portfolio {site}")
    # Positioning must match the ROLE FAMILY (a product-designer letter on a
    # design-engineer application is the wrong resume story and vice versa).
    try:
        fam_name, fam = _family(cfg, job.title)
    except CoverLetterNotConfigured as exc:
        return errors + [str(exc)]
    if fam.marker and fam.marker not in body.lower():
        errors.append(
            f"cover letter missing {fam.marker!r} positioning for this "
            f"{fam_name.replace('_', ' ')} role")

    expected_pdf = Path(cover_letter_pdf_path(job, cfg))
    pdf = Path(pdf_path) if pdf_path else expected_pdf
    if not pdf.exists():
        errors.append("cover letter PDF missing")
    elif pdf.resolve() != expected_pdf.resolve():
        errors.append(
            f"cover letter PDF path mismatch: expected {expected_pdf}, got {pdf}"
        )
    return errors


def write_cover_letter(job: Job, cfg: Config, use_llm: bool = False) -> str:
    text = llm_cover_letter(job, cfg) if use_llm else template_cover_letter(job, cfg)
    text_path = _text_path(job, cfg)
    text_path.write_text(text, encoding="utf-8")
    write_cover_letter_pdf(job, cfg, text)
    return str(text_path)
