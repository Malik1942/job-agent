"""Load and validate config.yaml into typed objects."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml


@dataclass
class ResumeVariant:
    """A role-specific resume: used when any keyword appears in the job title.
    Variants are checked in config order — put the most specific first."""
    path: str = ""
    keywords: list[str] = field(default_factory=list)


@dataclass
class CoverLetterFamily:
    """One letter 'voice' for a role family. All prose comes from the user's
    config — the generator has no built-in positioning."""
    keywords: list[str] = field(default_factory=list)  # matched against title
    identity: str = ""     # "I am a ..." paragraph opener
    craft: str = ""        # how-you-work sentence(s)
    closing_mix: str = ""  # "X, Y, and Z" for the closing sentence
    marker: str = ""       # lowercase phrase the validator requires in the letter


@dataclass
class CoverLetterProject:
    """A project story, included when a keyword appears in the job text."""
    keywords: list[str] = field(default_factory=list)
    text: str = ""


@dataclass
class CoverLetterConfig:
    # Family used when no family's keywords match the job title.
    default_family: str = ""
    families: dict = field(default_factory=dict)   # name -> CoverLetterFamily
    projects: list = field(default_factory=list)   # [CoverLetterProject]


@dataclass
class Profile:
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    website: str = ""    # portfolio URL; used in letters + validator when set
    linkedin: str = ""   # public profile URL for PDF letterhead
    pronouns: str = ""   # optional; forms that ask get this
    summary: str = ""
    titles: list[str] = field(default_factory=list)     # roles you want
    skills: list[str] = field(default_factory=list)     # keywords that count for you
    locations: list[str] = field(default_factory=list)  # acceptable locations, empty = any
    resume_path: str = ""   # fallback resume when no variant matches
    # Role-specific resumes, matched against the job title (first hit wins):
    #   resume_variants:
    #     - { keywords: ["design engineer", "ux engineer"], path: "de.pdf" }
    #     - { keywords: ["ux designer"], path: "ux.pdf" }
    resume_variants: list = field(default_factory=list)
    answers_path: str = ""  # markdown file of reference answers for form questions

    def resume_for_title(self, title: str) -> str:
        """The resume to attach for this job. First variant whose keyword
        appears in the title wins; otherwise the fallback resume_path. A
        missing file is NOT silently substituted — the filler holds the job
        instead of attaching the wrong variant."""
        t = " ".join((title or "").casefold().split())
        for v in self.resume_variants:
            if any(k.casefold() in t for k in v.keywords if k):
                return v.path
        return self.resume_path

    @property
    def any_resume_configured(self) -> bool:
        return bool(self.resume_path or self.resume_variants)


@dataclass
class ATSProfile:
    """Per-ATS behavior, keyed by source kind (greenhouse | ashby | lever | …).

    submit_mode: "auto" = the agent drives + submits the form; "manual" = the
      platform flags automation (Ashby-type), so the agent prepares the
      application and a human clicks submit.
    expected_fields: representative form labels this ATS commonly requires.
      `jobagent coverage` checks your answers file can resolve each, so you
      learn "Ashby will ask for X and you have no answer" BEFORE a live form
      rejects you — the class of failure that blocked two OpenAI applications
      on an unmapped arbitration acknowledgement.
    """
    submit_mode: str = "auto"
    expected_fields: list[str] = field(default_factory=list)


def _default_ats_profiles() -> dict[str, ATSProfile]:
    # Seeded from labels these ATSes actually asked in past runs (the tracker),
    # not guesses. Override or extend per kind under autonomy.ats_profiles.
    return {
        "ashby": ATSProfile(submit_mode="manual", expected_fields=[
            "Legal Name", "Preferred Name",
            "Where are you currently located?",
            "Are you authorized to work in the country where the job is located?",
            "When can you start a new role?",
            "Applicant Arbitration Agreement Acknowledgement",
            "What are your preferred pronouns?",
            "Are you able to work from our US office three days per week?",
        ]),
        "greenhouse": ATSProfile(submit_mode="auto", expected_fields=[
            "First Name", "Last Name", "Gender", "Are you Hispanic/Latino?",
            "Veteran Status", "Disability Status", "Please identify your race",
            "Location (City)", "From where do you intend to work?",
            "Are you authorized to work in the country for which you applied?",
        ]),
        "lever": ATSProfile(submit_mode="auto", expected_fields=[
            "Full name", "Current location", "Current company",
            "LinkedIn URL", "Portfolio URL",
        ]),
    }


@dataclass
class Autonomy:
    # "review": everything above threshold lands in the tracker as needs_review.
    # "auto": eligible ATS jobs are submitted without a human gate.
    mode: str = "review"
    # Master safety switch. When False, apply() never touches a live endpoint.
    live_submit: bool = False
    # Minimum score (0..1) to act on a job at all.
    min_score: float = 0.55
    # Safety cap: max auto-submit attempts the agent will drive per run. Once
    # reached, remaining eligible jobs are left for the next run (jobs are
    # processed highest-score first). 0 = no cap. Only limits AUTO mode; review
    # mode opens no browser and is never capped.
    max_applications_per_run: int = 25
    # Companies you want surfaced/ranked, but never browser-filled or submitted
    # by the agent. Matching is case-insensitive and allows subsidiaries or
    # board labels such as "Google DeepMind".
    manual_only_companies: list[str] = field(default_factory=lambda: [
        "Apple",
        "Google",
        "Microsoft",
    ])
    # ATS platforms whose server-side anti-fraud flags automated submits as
    # spam (Ashby's "Fraudulent Candidate Detection"). The agent FILLS and
    # screenshots these but NEVER clicks submit — you finish them in your own
    # browser, which is the only reliable + above-board path for these. Matched
    # against a job's source kind (greenhouse | lever | ashby | ...).
    manual_submit_kinds: list[str] = field(default_factory=lambda: ["ashby"])
    # Refuse to click submit when the egress IP looks like a datacenter/VPN —
    # Greenhouse's #1 fraud signal and the same class Ashby flags. The job is
    # held as needs_review with the reason so you can turn the VPN off and
    # rerun. Compliance gate, not evasion. See jobagent/netcheck.py.
    block_datacenter_ip: bool = True
    # Per-ATS profiles (submit routing + expected form fields). Supersedes
    # manual_submit_kinds for routing; manual_submit_kinds still forces a kind
    # manual for back-compat. See ATSProfile and `jobagent coverage`.
    ats_profiles: dict = field(default_factory=_default_ats_profiles)
    # Two-phase approvals: an Approve first FILLS the form and screenshots it
    # (no submit), then a separate Confirm actually submits. Gives you a look
    # at the exact filled form before anything goes out. Applies to the Slack
    # bot and the web panel; single-phase behavior returns if set to false.
    two_phase_approval: bool = True
    # Pacing between batch submissions (Approve All / Confirm All), in
    # seconds. A random pause in [min, max] separates browser runs so bursts
    # do not look bot-like to ATS rate limiters. Set both to 0 to disable.
    batch_pause_min_seconds: int = 45
    batch_pause_max_seconds: int = 120


@dataclass
class ApprovalWeb:
    # Bind address for the phone approval panel. Default is localhost-only;
    # set to a Tailscale/VPN interface IP to reach it from your phone, or
    # 0.0.0.0 to expose on the whole LAN (not recommended).
    host: str = "127.0.0.1"
    port: int = 8787
    # Generate a fresh token every time the panel starts (old links stop
    # working). Ignored when JOBAGENT_APPROVAL_WEB_TOKEN is set.
    rotate_token: bool = True


@dataclass
class Filters:
    # Any of these substrings in a title or description drops the job to 0.
    exclude_keywords: list[str] = field(default_factory=list)
    # If set, a job must contain at least one of these to survive.
    require_keywords: list[str] = field(default_factory=list)
    # Hardware / facilities engineering roles whose TITLE matches "… Design
    # Engineer" and get mis-caught by the "Design Engineer" title preference,
    # but are not product/UX/design-engineer work. Matched against the TITLE
    # ONLY (word-boundary), never the description — a designer's profile may
    # legitimately say "hardware"/"wearables", so a description blacklist
    # would kill real fits.
    # Calibrated against real OpenAI/Whoop postings (silicon, robotics,
    # data-center electrical, AV/IT, NPI manufacturing). A bare "Design
    # Engineer" (no hardware qualifier) is deliberately NOT excluded, so a
    # software/hardware-hybrid design role still survives.
    title_exclude_keywords: list[str] = field(default_factory=lambda: [
        "mechanical engineer", "mechanical design engineer",
        "electrical engineer", "electrical design engineer",
        "physical design engineer",
        "data center engineer", "data center design engineer",
        "audiovisual engineer", "audiovisual design engineer", "a/v engineer",
        "hardware engineer", "hardware design engineer",
        "firmware engineer", "asic", "vlsi", "rtl design", "silicon design",
        "hvac", "facilities engineer", "structural engineer", "civil engineer",
        "thermal engineer", "packaging engineer",
        "manufacturing engineer", "manufacturing design engineer",
    ])


@dataclass
class Scoring:
    # Role preference: earlier entries get a larger small bonus. This lets the
    # ranking prefer AI/Product Designer roles before design-engineer and UX.
    role_priority: list[str] = field(default_factory=lambda: [
        "AI Product Designer",
        "Product Designer",
        "AI Designer",
        "Design Engineer",
        "UX Designer",
        "User Experience Designer",
        "Interaction Designer",
    ])
    role_priority_bonus: float = 0.30
    # Boost roles that look aligned with early-career / new-grad searches.
    preferred_keywords: list[str] = field(default_factory=lambda: [
        "new grad",
        "new graduate",
        "recent graduate",
        "university graduate",
        "graduate",
        "entry level",
        "entry-level",
        "early career",
        "junior",
        "associate",
    ])
    preferred_bonus: float = 0.20
    # Keep senior roles visible, but rank them lower than new-grad matches.
    penalty_keywords: list[str] = field(default_factory=lambda: [
        "senior",
        "staff",
        "principal",
        "lead",
        "manager",
        "director",
        "head of",
    ])
    penalty: float = 0.20


@dataclass
class Apply:
    # Run the browser without a visible window. Keep False the first few times
    # so you can watch what it does.
    headless: bool = False
    # Controls WHEN an unanswered required field holds the job, not WHETHER.
    # True (recommended): hold early, right after filling, with the full
    # fill/answer log. False: proceed through fill, but the unconditional
    # pre-submit invariant STILL holds any required field that is empty,
    # sitting on a placeholder, filled wrong, or that the answers file could
    # not cover — the tool never submits an unhandled required field either
    # way. (Before the pre-submit invariant, False could submit half-filled
    # applications; it no longer can.)
    hold_on_unknown_required: bool = True
    # Use the LLM to answer questions your template does not cover directly.
    # It is constrained to your provided facts and returns nothing rather than
    # inventing an answer. Needs a configured Claude or OpenAI/Codex key.
    use_llm_for_answers: bool = False
    # Where fill screenshots go for your review.
    screenshot_dir: str = "output/screenshots"
    # In visible dry-runs, keep the browser open briefly before closing so you
    # can watch/review the filled form. 0 disables the pause.
    dry_run_pause_seconds: int = 8
    # Browser context locale + IANA timezone. Set these to your REAL values so
    # the browser reports the truth and never trips an ATS "timezone/location
    # mismatch" fraud signal (headless Chromium otherwise reports UTC). This is
    # honesty, not spoofing — set them to where you actually are. Empty
    # timezone_id auto-detects the machine's timezone; empty locale skips it.
    locale: str = "en-US"
    timezone_id: str = ""
    # Labels matching any of these keywords are NEVER auto-filled (portfolio
    # passwords, access codes, ...). If such a field is required, the job is
    # held as needs_review for you instead of the tool guessing something
    # that reads as junk to a recruiter.
    never_fill_keywords: list[str] = field(default_factory=lambda: [
        "password", "passcode", "access code",
    ])


@dataclass
class EmailVerify:
    # Some ATS boards (Oura's Greenhouse) email a numeric code after submit
    # that must be entered to complete the application. When enabled, the
    # filler reads the code from the APPLICATION inbox over IMAP and finishes
    # the step. Legitimate: it satisfies the anti-spam check for the real
    # applicant, it does not bypass it. Off by default (needs an app password).
    enabled: bool = False
    imap_host: str = "imap.gmail.com"
    # Inbox to read. Empty -> profile.email (the address on the applications,
    # which is where the codes actually land). For Gmail use an App Password.
    imap_user: str = ""
    # The IMAP/app password is read from this env var, never from config.
    password_env: str = "JOBAGENT_IMAP_PASSWORD"
    # How long to wait for the code email to arrive after clicking submit.
    timeout_seconds: int = 120


@dataclass
class Verification:
    """Trust gates for reading a verification code out of the inbox. The inbox
    is UNTRUSTED input (anyone can email a fake "security code"), so a code is
    used only when it clears all three gates: it comes from an allowlisted
    sender FOR THE ATS THAT TRIGGERED THE POLL (gate 1), the token immediately
    follows a known anchor phrase and is not an English word (gate 2), and it is
    bound to that ATS + the current time window at form-entry (gate 3).
    Principle: never trust, verify provenance."""
    # Gate 1: per-ATS allowlist of sender domains. An EMPTY list means "not yet
    # calibrated" — the job HOLDS and asks for a real sample rather than
    # guessing (no extrapolating one ATS's sender to another).
    senders: dict = field(default_factory=lambda: {
        "greenhouse": ["greenhouse-mail.io", "greenhouse.io"],
        "lever": [],
        "ashby": [],
    })
    # Gate 2: the code token must immediately follow one of these phrases.
    # Empty -> the DEFAULT_ANCHORS data constant.
    anchors: list = field(default_factory=list)
    # Gate 3: clock-skew allowance (seconds) on the message-timestamp window.
    clock_skew_seconds: int = 120


@dataclass
class Logging:
    # Diagnostics for the form filler (per-field fill actions, scan-diff
    # re-render signals, pre-submit validation). INFO is the useful default;
    # DEBUG adds per-field readback values. `file` gives the launchd daily run
    # a durable place to write — empty means stderr only.
    level: str = "INFO"
    file: str = ""


@dataclass
class LLM:
    # "auto" uses Anthropic when ANTHROPIC_API_KEY exists, otherwise OpenAI
    # when OPENAI_API_KEY exists. Use "anthropic", "openai", or "off" to force.
    provider: str = "auto"
    # Lets the daily runner use LLM cover letters without needing a CLI flag.
    cover_letters: bool = False
    anthropic_model: str = "claude-sonnet-4-6"
    openai_model: str = "gpt-5.5"
    answer_max_tokens: int = 300
    cover_letter_max_tokens: int = 600


@dataclass
class EmailNotify:
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    username: str = ""
    from_addr: str = ""
    to_addrs: list[str] = field(default_factory=list)
    # The password is read from this environment variable, never from config.
    password_env: str = "JOBAGENT_SMTP_PASSWORD"


@dataclass
class SlackNotify:
    enabled: bool = False
    # The incoming-webhook URL is read from this env var, never from config.
    webhook_env: str = "JOBAGENT_SLACK_WEBHOOK"


@dataclass
class Notify:
    email: EmailNotify = field(default_factory=EmailNotify)
    slack: SlackNotify = field(default_factory=SlackNotify)
    # Only notify about new records with these statuses.
    only_status: list[str] = field(default_factory=lambda: ["needs_review", "applied"])
    min_score: float = 0.0


@dataclass
class Source:
    kind: str            # greenhouse | lever | ashby | workable |
                         # smartrecruiters | recruitee | adzuna | scan_only
    token: str = ""      # board token / company slug the API needs
    label: str = ""      # human name for logs, defaults to token


@dataclass
class Config:
    profile: Profile
    autonomy: Autonomy
    filters: Filters
    scoring: Scoring
    sources: list[Source]
    apply: Apply
    llm: LLM
    notify: Notify
    coverletter: CoverLetterConfig = field(default_factory=CoverLetterConfig)
    logging: Logging = field(default_factory=Logging)
    email_verify: EmailVerify = field(default_factory=EmailVerify)
    verification: Verification = field(default_factory=Verification)
    approval_web: ApprovalWeb = field(default_factory=ApprovalWeb)
    db_path: str = "jobagent.db"
    output_dir: str = "output"
    csv_path: str = "output/applied.csv"      # human log of submitted jobs
    csv_statuses: list[str] = field(default_factory=lambda: ["applied"])


def load_config(path: str | Path) -> Config:
    data = yaml.safe_load(Path(path).read_text()) or {}

    p = dict(data.get("profile") or {})
    raw_variants = p.pop("resume_variants", None) or []
    if not isinstance(raw_variants, list):
        raise ValueError("profile.resume_variants must be a list of "
                         "{keywords, path} entries")
    profile = Profile(**p)
    for spec in raw_variants:
        if not isinstance(spec, dict) or not spec.get("path"):
            raise ValueError(
                f"each profile.resume_variants entry needs a path: {spec!r}")
        profile.resume_variants.append(ResumeVariant(
            path=str(spec["path"]),
            keywords=[str(k) for k in (spec.get("keywords") or [])],
        ))
    a = dict(data.get("autonomy") or {})
    raw_profiles = a.pop("ats_profiles", None) or {}
    if not isinstance(raw_profiles, dict):
        raise ValueError("autonomy.ats_profiles must be a mapping of kind -> profile")
    autonomy = Autonomy(**a)
    # yaml overrides merge ONTO the seeded profile for that kind (replace), so
    # restating one field (e.g. submit_mode) keeps the seeded expected_fields.
    for kind, spec in raw_profiles.items():
        if not isinstance(spec, dict):
            raise ValueError(
                f"autonomy.ats_profiles['{kind}'] must be a mapping, "
                f"got {type(spec).__name__}")
        base = autonomy.ats_profiles.get(kind) or ATSProfile()
        try:
            autonomy.ats_profiles[kind] = replace(base, **spec)
        except TypeError as exc:
            raise ValueError(f"invalid autonomy.ats_profiles['{kind}']: {exc}")
    # Back-compat: manual_submit_kinds forces a kind to manual — UNLESS the user
    # explicitly set that kind's profile in yaml (an explicit profile wins, so
    # ats_profiles.<kind>.submit_mode: auto can re-enable auto-submit).
    for kind in autonomy.manual_submit_kinds:
        if kind in raw_profiles:
            continue
        base = autonomy.ats_profiles.get(kind) or ATSProfile()
        autonomy.ats_profiles[kind] = replace(base, submit_mode="manual")
    # Normalize + validate: a typo like "Manual" must not silently downgrade a
    # fraud-flagging ATS to auto-submit.
    for kind, prof in autonomy.ats_profiles.items():
        mode = str(prof.submit_mode).strip().lower()
        if mode not in ("auto", "manual"):
            raise ValueError(
                f"autonomy.ats_profiles['{kind}'].submit_mode must be 'auto' or "
                f"'manual', got {prof.submit_mode!r}")
        prof.submit_mode = mode
    filters = Filters(**(data.get("filters") or {}))
    scoring = Scoring(**(data.get("scoring") or {}))
    apply_cfg = Apply(**(data.get("apply") or {}))
    llm_cfg = LLM(**(data.get("llm") or {}))
    logging_cfg = Logging(**(data.get("logging") or {}))
    email_verify_cfg = EmailVerify(**(data.get("email_verify") or {}))
    verification_cfg = Verification(**(data.get("verification") or {}))

    n = data.get("notify") or {}
    notify = Notify(
        email=EmailNotify(**(n.get("email") or {})),
        slack=SlackNotify(**(n.get("slack") or {})),
        only_status=n.get("only_status", ["needs_review", "applied"]),
        min_score=n.get("min_score", 0.0),
    )

    sources = []
    for raw in data.get("sources") or []:
        src = Source(**raw)
        if not src.label:
            src.label = src.token or src.kind
        sources.append(src)

    cl_raw = data.get("coverletter") or {}
    families: dict[str, CoverLetterFamily] = {}
    for name, spec in (cl_raw.get("families") or {}).items():
        if not isinstance(spec, dict):
            raise ValueError(f"coverletter.families['{name}'] must be a mapping")
        families[str(name)] = CoverLetterFamily(
            keywords=[str(k) for k in (spec.get("keywords") or [])],
            identity=str(spec.get("identity") or ""),
            craft=str(spec.get("craft") or ""),
            closing_mix=str(spec.get("closing_mix") or ""),
            marker=str(spec.get("marker") or "").strip().lower(),
        )
    projects = []
    for spec in cl_raw.get("projects") or []:
        if not isinstance(spec, dict) or not spec.get("text"):
            raise ValueError(f"each coverletter.projects entry needs text: {spec!r}")
        projects.append(CoverLetterProject(
            keywords=[str(k) for k in (spec.get("keywords") or [])],
            text=str(spec["text"]),
        ))
    default_family = str(cl_raw.get("default_family") or "")
    if families and not default_family:
        default_family = next(iter(families))
    if default_family and families and default_family not in families:
        raise ValueError(
            f"coverletter.default_family {default_family!r} is not one of "
            f"{sorted(families)}")
    coverletter = CoverLetterConfig(
        default_family=default_family, families=families, projects=projects)

    return Config(
        profile=profile,
        autonomy=autonomy,
        filters=filters,
        scoring=scoring,
        sources=sources,
        apply=apply_cfg,
        llm=llm_cfg,
        notify=notify,
        coverletter=coverletter,
        logging=logging_cfg,
        email_verify=email_verify_cfg,
        verification=verification_cfg,
        approval_web=ApprovalWeb(**(data.get("approval_web") or {})),
        db_path=data.get("db_path", "jobagent.db"),
        output_dir=data.get("output_dir", "output"),
        csv_path=data.get("csv_path", "output/applied.csv"),
        csv_statuses=data.get("csv_statuses", ["applied"]),
    )


def profile_gaps(cfg: Config) -> list[str]:
    """What still blocks personalized use. Empty list == ready to run.
    The CLI shows these with a pointer to `jobagent setup`."""
    gaps: list[str] = []
    p = cfg.profile
    if not p.name.strip():
        gaps.append("profile.name is empty")
    if not p.email.strip() or "example.com" in p.email:
        gaps.append("profile.email is empty or still the example value")
    if not p.titles:
        gaps.append("profile.titles has no target roles")
    if not p.any_resume_configured:
        gaps.append("no resume configured (profile.resume_path / resume_variants)")
    if not p.answers_path.strip():
        gaps.append("profile.answers_path is empty")
    if not cfg.coverletter.families:
        gaps.append("coverletter.families is empty — no letter content configured")
    for name, fam in cfg.coverletter.families.items():
        if not fam.identity.strip():
            gaps.append(f"coverletter.families.{name}.identity is empty")
        if not fam.craft.strip():
            gaps.append(f"coverletter.families.{name}.craft is empty")
        if not fam.marker.strip():
            gaps.append(f"coverletter.families.{name}.marker is empty")
    return gaps
