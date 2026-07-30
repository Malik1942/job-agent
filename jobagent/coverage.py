"""Pre-flight answer coverage: does your answers file cover what each ATS asks?

Diffs every ATS profile's expected_fields against the AnswerBook, so you learn
"Ashby will ask for X and you have no answer for it" BEFORE a live form rejects
you — the exact failure class that blocked two OpenAI applications on an
unmapped arbitration-agreement acknowledgement.

Checks are STATIC (field aliases + your Q/A pairs), with the LLM deliberately
off so a would-be LLM answer never masks a real gap.

Scope: this reports whether your answers file can PRODUCE an answer for each
label (the missing-alias / missing-value failure class). It does not simulate
mapping that answer onto a specific radio/checkbox option set, so a "covered"
result means "you have an answer," not "the filler is guaranteed to place it."
"""

from __future__ import annotations

from dataclasses import dataclass

from .answers import AnswerBook
from .config import Config


@dataclass
class ATSCoverage:
    source: str
    submit_mode: str
    covered: list[str]
    gaps: list[str]

    @property
    def total(self) -> int:
        return len(self.covered) + len(self.gaps)


def coverage_report(cfg: Config, book: AnswerBook) -> list[ATSCoverage]:
    """For each ATS profile, split its expected_fields into ones the answer
    book can resolve (covered) and ones it cannot (gaps)."""
    reports: list[ATSCoverage] = []
    for source, prof in sorted(cfg.autonomy.ats_profiles.items()):
        covered: list[str] = []
        gaps: list[str] = []
        for label in prof.expected_fields:
            resolved = book.resolve(label)
            (covered if resolved is not None else gaps).append(label)
        reports.append(ATSCoverage(source, prof.submit_mode, covered, gaps))
    return reports


def total_gaps(reports: list[ATSCoverage]) -> int:
    return sum(len(r.gaps) for r in reports)
