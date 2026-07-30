"""Turn your reference answers markdown into answers for real form questions.

The whole point of this module: the browser filler hands it a field label
("Are you legally authorized to work in the US?", "LinkedIn Profile", "Why do
you want to join?") and it returns the answer you wrote, or nothing.

Matching happens in tiers, most trustworthy first:
  1. Structured work experience, but only inside work/employment sections
  2. Known field aliases    (email, phone, work authorization, links, ...)
  3. Fuzzy match to your Q/A pairs
  4. Optional LLM, constrained to your facts, returns NEEDS_HUMAN if it cannot
  5. Nothing  -> the field is left for you

It never fabricates. If the answer is not in your file (and the LLM cannot
ground it in your file), the field comes back unanswered so the job is held
for you rather than submitted with a guess.

Template format (see answers.example.md):

    ## Fields
    - first_name: Alex
    - work_authorization: Authorized to work in the US
    - require_sponsorship: No

    ## Questions
    Q: Why do you want to work here?
    A: ...

    ## Context
    Free-form background the LLM may draw on for uncovered questions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .llm import complete

# Canonical field key -> phrases that commonly label it on an ATS form.
FIELD_ALIASES: dict[str, list[str]] = {
    "first_name": ["first name", "given name", "legal first name"],
    "last_name": ["last name", "family name", "surname", "legal last name"],
    "full_name": ["full name", "name"],
    "email": ["email", "e mail"],
    "phone": ["phone", "mobile", "telephone", "cell", "cellphone"],
    "country": [
        "country", "based in any of these countries",
        "countries where we are accepting",
        "which country do you live in", "country of residence",
    ],
    # Word-boundary matching (see resolve) makes the bare "state" phrase safe:
    # it must appear as its own word, so "Personal statement" and "United
    # States" never match it.
    "state": [
        "state", "state province", "state or province", "province",
        "state of residence", "which state do you live in", "state region",
    ],
    "location": [
        "location", "current location", "city", "where are you based",
        "where are you currently located", "where are you located",
        "currently located", "current city", "where do you intend to work",
        "intend to work", "address from which you plan on working",
        "address you will be working from",
    ],
    "linkedin": ["linkedin"],
    # NOTE: no "portfolio password" alias on purpose — password-ish labels are
    # never auto-filled (see apply.never_fill_keywords in config).
    "website": ["website", "portfolio", "personal site", "url"],
    "current_company": ["current company", "current employer", "present company",
                        "company you currently work"],
    "github": ["github"],
    "work_authorization": [
        "authorized to work", "legally authorized", "work authorization",
        "eligible to work", "right to work", "authorized to work in the country",
    ],
    "require_sponsorship": [
        "require sponsorship", "need sponsorship", "visa sponsorship",
        "will you now or in the future require", "sponsorship",
    ],
    "willing_to_relocate": ["relocate", "willing to relocate", "open to relocation"],
    "us_canada_based": [
        "currently based in the u s or canada",
        "based in the u s or canada",
        "based in the us or canada",
        "located in the u s or canada",
        "located in the us or canada",
    ],
    "desired_salary": [
        "desired salary", "salary expectation", "compensation expectation",
        "expected compensation", "salary requirement",
    ],
    "start_date": [
        "start date", "available to start", "availability", "notice period",
        "when can you start", "when can you start a new role",
        "earliest start date", "available start date",
        "earliest you would want to start", "earliest you could start",
    ],
    "office_three_days_per_week": [
        "three days per week", "3 days per week", "work from our us office",
        "work from us office", "able to work from our us office",
    ],
    "in_person_office": [
        "in person in one of our offices", "in one of our offices",
        "work in person", "in office 25", "25 of the time",
        "open to working in person", "onsite 25",
        "anchor days", "commit to working from one of our offices",
        "working from one of our offices",
    ],
    # "Do you live in <office area> and can commit to coming in N days a
    # week?" knockout questions. The answer comes from answers.md
    # (office_area_commit) — set your own standing policy there, e.g. "Yes"
    # when you are willing to relocate and commit to the office.
    "office_area_commit": [
        "live in the sf bay area", "live in the bay area",
        "bay area and can commit", "commit to being in the office",
        "in the office a few times a week", "office a few days a week",
        "can commit to working from the office",
        "commute to the office", "commuting distance of the office",
        "working in a hybrid environment",
        "coming into our san francisco office",
        "office approximately 50 of the time",
        "office approximately 3 days",
    ],
    "interviewed_before": [
        "ever interviewed", "interviewed before", "interviewed at anthropic",
        "previously interviewed", "interviewed with us", "interviewed here",
    ],
    "pronouns": ["pronouns"],
    # Consent / agreement checkboxes (privacy policy, data retention).
    # Add `- consent: Yes` under ## Fields to authorize checking these;
    # without it every consent box is held for you to decide.
    "consent": [
        "i agree", "do you agree", "consent", "acknowledge",
        "privacy policy", "retain your data", "retain my data",
        "agree to allow", "terms and conditions",
        "arbitration", "arbitration agreement",
        "privacy notice", "applicant privacy",
        "double check all the information",
        "information provided is accurate", "ensuring accuracy",
    ],
    "phonetic_name": [
        "phonetic spelling", "pronunciation", "pronunciation guide",
        "how do you pronounce", "pronounce it correctly", "how to pronounce",
    ],
    "how_did_you_hear": [
        "how did you hear", "referral source", "where did you find",
        "where did you first hear", "first hear about", "hear about this role",
        "hear about this position", "hear about us",
    ],
    "worked_for_company_before": [
        "worked for figma before",
        "worked for this company before",
        "worked for company before",
        "employee or a contractor",
        "contractor consultant",
    ],
    "gender": ["gender"],
    "hispanic_latino": ["hispanic", "latino"],
    "race": ["race", "ethnicity"],
    "veteran_status": ["veteran"],
    "disability_status": ["disability"],
}

# US state name <-> abbreviation, both directions. Greenhouse State selects
# often offer ONLY abbreviations ("WA"), while the natural answers.md value is
# the full name ("Washington") — _fit_options maps across.
US_STATE_ABBR = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct",
    "delaware": "de", "florida": "fl", "georgia": "ga", "hawaii": "hi",
    "idaho": "id", "illinois": "il", "indiana": "in", "iowa": "ia",
    "kansas": "ks", "kentucky": "ky", "louisiana": "la", "maine": "me",
    "maryland": "md", "massachusetts": "ma", "michigan": "mi",
    "minnesota": "mn", "mississippi": "ms", "missouri": "mo",
    "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm",
    "new york": "ny", "north carolina": "nc", "north dakota": "nd",
    "ohio": "oh", "oklahoma": "ok", "oregon": "or", "pennsylvania": "pa",
    "rhode island": "ri", "south carolina": "sc", "south dakota": "sd",
    "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt",
    "virginia": "va", "washington": "wa", "west virginia": "wv",
    "wisconsin": "wi", "wyoming": "wy", "district of columbia": "dc",
}
US_STATE_FULL = {abbr: name for name, abbr in US_STATE_ABBR.items()}

# Values that read as yes / no, so we can map them onto radio and select options.
_YES = {"yes", "y", "true", "authorized to work in the us", "authorized",
        "i am authorized", "willing", "open"}
# Agree-style options that a "Yes" answer maps onto — consent/acknowledgement
# selects often offer no literal Yes ("Acknowledge/Confirm", "I have reviewed
# and confirmed that all the information provided is accurate...").
_AGREE_OPTIONS = {"acknowledge", "confirm", "acknowledge confirm", "agree",
                  "accept", "i agree", "i accept", "i acknowledge", "i confirm"}
_AGREE_PREFIXES = ("i acknowledge", "i agree", "i confirm", "i accept",
                   "i have reviewed and confirmed", "i have read and")
_NO = {"no", "n", "false", "do not require", "not required"}
QUESTION_MATCH_THRESHOLD = 0.72
MONTH_NAMES = {
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def _tokens(s: str) -> set[str]:
    return set(_norm(s).split())


def _is_decline_text(ntext: str) -> bool:
    return any(
        phrase in ntext for phrase in (
            "decline", "self identify", "not wish", "do not wish",
            "don t wish", "wish to answer", "prefer not", "choose not",
        )
    )


def _is_negative_text(ntext: str) -> bool:
    tokens = set(ntext.split())
    return (
        bool(tokens & {"no", "not", "none", "neither", "without"})
        or "do not" in ntext
        or "don t" in ntext
        or "does not" in ntext
        or "not a" in ntext
    )


def _is_no_like_option(ntext: str) -> bool:
    if _is_decline_text(ntext):
        return False
    return (
        ntext in {"no", "false"}
        or ntext.startswith("no")
        or "not hispanic" in ntext
        or "not latino" in ntext
        or "do not require" in ntext
        or "don t require" in ntext
        or "do not need" in ntext
        or "don t need" in ntext
        or "not a protected veteran" in ntext
        or "not protected veteran" in ntext
        or (
            "veteran" in ntext
            and _is_negative_text(ntext)
        )
        or "do not have a disability" in ntext
        or "don t have a disability" in ntext
        or (
            "disability" in ntext
            and _is_negative_text(ntext)
        )
    )


def _is_placeholder_value(value: str) -> bool:
    nvalue = _norm(value)
    return (
        not nvalue
        or nvalue in {"todo", "todo confirm", "tbd", "to be confirmed"}
        or nvalue.startswith("todo")
    )


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


@dataclass
class Resolved:
    value: str
    source: str  # field | question | llm
    confidence: float


class AnswerBook:
    def __init__(self, fields: dict[str, str], qa: list[tuple[str, str]],
                 context: str = "", use_llm: bool = False,
                 llm_settings: Any = None):
        self.fields = fields
        self.qa = qa
        self.context = context
        self.use_llm = use_llm
        self.llm_settings = llm_settings
        self._norm_qa = [(_norm(q), q, a) for q, a in qa]
        self._work_count = self._count_work_experiences()

    # ---- loading -----------------------------------------------------------
    @classmethod
    def from_file(
        cls, path: str, use_llm: bool = False, llm_settings: Any = None
    ) -> "AnswerBook":
        if not path or not Path(path).exists():
            return cls({}, [], "", use_llm, llm_settings)
        return cls.from_text(Path(path).read_text(), use_llm, llm_settings)

    @classmethod
    def from_text(
        cls, text: str, use_llm: bool = False, llm_settings: Any = None
    ) -> "AnswerBook":
        section = None
        fields: dict[str, str] = {}
        qa: list[tuple[str, str]] = []
        context_lines: list[str] = []
        pending_q: str | None = None

        for line in text.splitlines():
            header = re.match(r"^\s*#+\s*(.+?)\s*$", line)
            if header:
                section = _norm(header.group(1))
                continue

            if section and section.startswith("field"):
                m = re.match(r"^\s*[-*]?\s*([A-Za-z0-9_ ]+?)\s*[:=]\s*(.+?)\s*$", line)
                if m:
                    key = _norm(m.group(1)).replace(" ", "_")
                    val = m.group(2).strip()
                    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                        val = val[1:-1]
                    fields[key] = val

            elif section and section.startswith("question"):
                mq = re.match(r"^\s*Q\s*[:.]\s*(.+?)\s*$", line, re.IGNORECASE)
                ma = re.match(r"^\s*A\s*[:.]\s*(.+?)\s*$", line, re.IGNORECASE)
                if mq:
                    pending_q = mq.group(1).strip()
                elif ma and pending_q is not None:
                    qa.append((pending_q, ma.group(1).strip()))
                    pending_q = None
                elif pending_q is None and qa and line.strip():
                    # continuation line of the previous answer
                    q, a = qa[-1]
                    qa[-1] = (q, (a + " " + line.strip()).strip())

            elif section and section.startswith("context"):
                context_lines.append(line)

        return cls(fields, qa, "\n".join(context_lines).strip(), use_llm, llm_settings)

    # ---- resolving ---------------------------------------------------------
    def work_experience_count(self) -> int:
        return self._work_count

    def resolve_work_experience(
        self,
        label: str,
        field_type: str = "text",
        options: list[str] | None = None,
        index: int = 1,
        meta: str = "",
        context: str = "",
    ) -> Resolved | None:
        """Resolve a structured work-history field from work_N_* facts.

        This is intentionally separate from normal aliases so generic labels
        like "Location" only become work-history answers when the DOM context
        looks like Work Experience / Employment History.
        """
        if index < 1 or index > self._work_count:
            return None

        key = self._work_field_key(label, meta, options)
        if key is None:
            return None

        ntext = _norm(f"{label} {meta} {context}")
        strong_context = any(
            marker in ntext for marker in (
                "work experience", "employment history", "work history",
                "professional experience", "experience section",
                "employment section",
            )
        )
        implied_work_field = any(
            marker in ntext for marker in (
                "employer", "company name", "job title", "position title",
                "current employer", "current company",
            )
        )
        if not (strong_context or implied_work_field):
            return None

        value = self._work_value(index, key)
        if value is None or _is_placeholder_value(value):
            return None
        fitted = self._fit_options(value, field_type, options)
        if fitted is None:
            return None
        return Resolved(fitted, "work_experience", 0.95)

    def resolve(self, label: str, field_type: str = "text",
                options: list[str] | None = None) -> Resolved | None:
        nlabel = _norm(label)
        if not nlabel:
            return None

        # 1) known field aliases. Containment is at WORD boundaries (" state "
        # never matches "statement" or "united states of america") so short
        # aliases stay safe; near-miss inflections ("relocation" for
        # "relocate") are still caught by the fuzzy ratio branch below.
        padded_label = f" {nlabel} "
        best_key, best_score, best_len = None, 0.0, 0
        for key, phrases in FIELD_ALIASES.items():
            if key not in self.fields:
                continue
            for phrase in phrases:
                if f" {phrase} " in padded_label:
                    score = max(len(phrase) / max(len(nlabel), 1), 0.9)
                    if score > best_score or (
                        score == best_score and len(phrase) > best_len
                    ):
                        best_key, best_score = key, score
                        best_len = len(phrase)
                else:
                    r = _ratio(phrase, nlabel)
                    if r > best_score and r >= 0.82:
                        best_key, best_score = key, r
                        best_len = len(phrase)
        if best_key:
            value = self.fields[best_key]
            if _is_placeholder_value(value):
                return None
            value = self._fit_options(value, field_type, options)
            if value is not None:
                return Resolved(value, "field", round(best_score, 2))

        # 2) fuzzy match to your Q/A pairs
        q_best, q_ans, q_score = None, None, 0.0
        for nq, _q, a in self._norm_qa:
            r = _ratio(nq, nlabel)
            # also reward keyword containment for short labels
            if nq and nq in nlabel or nlabel in nq:
                r = max(r, 0.8)
            if r > q_score:
                q_best, q_ans, q_score = nq, a, r
        if q_ans is not None and q_score >= QUESTION_MATCH_THRESHOLD:
            fitted = self._fit_options(q_ans, field_type, options)
            if fitted is not None:
                return Resolved(fitted, "question", round(q_score, 2))

        # 3) optional LLM, constrained to your facts
        if self.use_llm:
            llm = self._llm_answer(label, field_type, options)
            if llm is not None:
                return Resolved(llm, "llm", 0.5)

        # 4) nothing
        return None

    def _count_work_experiences(self) -> int:
        max_idx = 0
        for key in self.fields:
            m = re.match(r"^work_(\d+)_(company|title|location|start_|end_|current|description)", key)
            if m:
                max_idx = max(max_idx, int(m.group(1)))
        return max_idx

    def _work_value(self, index: int, key: str) -> str | None:
        value = self.fields.get(f"work_{index}_{key}")
        if value:
            return value
        if key == "start_date":
            month = self.fields.get(f"work_{index}_start_month", "")
            year = self.fields.get(f"work_{index}_start_year", "")
            return " ".join(v for v in (month, year) if v) or None
        if key == "end_date":
            month = self.fields.get(f"work_{index}_end_month", "")
            year = self.fields.get(f"work_{index}_end_year", "")
            if month.lower() == "present" or year.lower() == "present":
                return "Present"
            return " ".join(v for v in (month, year) if v) or None
        return None

    def _work_field_key(
        self,
        label: str,
        meta: str = "",
        options: list[str] | None = None,
    ) -> str | None:
        ntext = _norm(f"{label} {meta}")
        nopts = [_norm(o) for o in options or []]
        has_month_options = any(o in MONTH_NAMES for o in nopts)
        has_year_options = any(re.fullmatch(r"(19|20)\d{2}", o) for o in nopts)

        if any(x in ntext for x in ("currently work", "current role", "current position",
                                    "currently employed", "i currently work")):
            return "current"
        if "employer" in ntext or "company name" in ntext:
            return "company"
        if ("company" in ntext and "worked for" not in ntext
                and "this company" not in ntext):
            return "company"
        if any(x in ntext for x in ("job title", "position title", "role title")):
            return "title"
        if any(x in ntext for x in ("title", "position", "role")):
            return "title"
        if "location" in ntext or "city" in ntext:
            return "location"
        if any(x in ntext for x in ("description", "responsibilities", "summary",
                                    "what did you do", "achievements")):
            return "description"

        is_start = any(x in ntext for x in ("start", "from", "begin"))
        is_end = any(x in ntext for x in ("end", "to", "until", "finish"))
        if is_start:
            if "month" in ntext or has_month_options:
                return "start_month"
            if "year" in ntext or has_year_options:
                return "start_year"
            return "start_date"
        if is_end:
            if "month" in ntext or has_month_options:
                return "end_month"
            if "year" in ntext or has_year_options:
                return "end_year"
            return "end_date"
        return None

    def _fit_options(self, value: str, field_type: str,
                     options: list[str] | None) -> str | None:
        """For radios / selects, map a free value onto one of the offered
        options. Returns None if it cannot map confidently (so the field holds)."""
        if field_type not in {"select", "radio", "checkbox"} or not options:
            return value

        nval = _norm(value)
        wants_decline = _is_decline_text(nval)
        if wants_decline:
            for opt in options:
                nopt = _norm(opt)
                if _is_decline_text(nopt):
                    return opt
        # Exact option match first. This is important for EEO fields:
        # "male" is a substring of "female", but it must never select Female.
        for opt in options:
            if _norm(opt) == nval:
                return opt
        # US state name <-> abbreviation ("Washington" <-> "WA"): Greenhouse
        # State selects often offer only the abbreviations.
        state_alt = US_STATE_ABBR.get(nval) or US_STATE_FULL.get(nval)
        if state_alt:
            for opt in options:
                if _norm(opt) == state_alt:
                    return opt
        if nval == "male":
            for opt in options:
                nopt = _norm(opt)
                if nopt in {"man", "i identify as a man"}:
                    return opt
        if nval == "female":
            for opt in options:
                nopt = _norm(opt)
                if nopt in {"woman", "i identify as a woman"}:
                    return opt
        # yes / no mapping
        want_yes = (
            nval in {_norm(v) for v in _YES}
            or nval.startswith("yes")
            or nval.startswith("authorized")
            or "authorized to work" in nval
        )
        want_no = (
            nval in {_norm(v) for v in _NO}
            or nval.startswith("no")
            or _is_no_like_option(nval)
        )
        for opt in options:
            no = _norm(opt)
            if want_yes and (no in {"yes", "true"} or no.startswith("yes")):
                return opt
            if want_yes and (no in _AGREE_OPTIONS
                             or no.startswith(_AGREE_PREFIXES)):
                return opt
            if not want_no or _is_decline_text(no):
                continue
            if _is_no_like_option(no):
                return opt
        # Conservative containment fallback for longer labels. Require token
        # containment rather than raw substrings to avoid male/female and
        # no/not collisions.
        val_negative = _is_negative_text(nval) and not wants_decline
        for opt in options:
            nopt = _norm(opt)
            if val_negative and not _is_negative_text(nopt):
                continue
            opt_tokens = _tokens(opt)
            val_tokens = _tokens(value)
            if not opt_tokens or not val_tokens:
                continue
            if len(nval) >= 4 and val_tokens.issubset(opt_tokens):
                return opt
            if len(nopt) >= 4 and opt_tokens.issubset(val_tokens):
                return opt
        # fuzzy fallback
        best, best_r = None, 0.0
        for opt in options:
            nopt = _norm(opt)
            if val_negative and not _is_negative_text(nopt):
                continue
            r = _ratio(nopt, nval)
            if r > best_r:
                best, best_r = opt, r
        return best if best_r >= 0.6 else None

    def _llm_answer(self, label: str, field_type: str,
                    options: list[str] | None) -> str | None:
        facts = "\n".join(f"- {k}: {v}" for k, v in self.fields.items())
        qa = "\n".join(f"Q: {q}\nA: {a}" for q, a in self.qa)
        opt_line = f"\nChoose exactly one of: {options}" if options else ""
        prompt = (
            "You are filling one field of a job application for a candidate. "
            "Use ONLY the facts below. Do not invent qualifications, dates, or "
            "numbers. If the field asks for information not present in the facts, "
            "reply with exactly NEEDS_HUMAN and nothing else.\n\n"
            f"FACTS:\n{facts}\n\nPRIOR ANSWERS:\n{qa}\n\nCONTEXT:\n{self.context}\n\n"
            f"FIELD LABEL: {label}{opt_line}\n\n"
            "Reply with just the answer text, or NEEDS_HUMAN."
        )
        max_tokens = getattr(self.llm_settings, "answer_max_tokens", 300)
        out = complete(prompt, self.llm_settings, max_tokens=max_tokens)

        if not out or out.upper().startswith("NEEDS_HUMAN"):
            return None
        return self._fit_options(out, field_type, options)
