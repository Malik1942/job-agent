"""Score a job against a profile.

This is intentionally a transparent, explainable heuristic rather than a black
box: title overlap, skill hits, and location fit, minus hard filters. Every
score carries a list of reasons so you can see why something ranked where it
did. An LLM scorer can be swapped in behind the same score() signature later
(see coverletter.py for the same pattern).
"""

from __future__ import annotations

import re

from .config import Config
from .models import Job


def _contains_any(text: str, needles: list[str]) -> list[str]:
    low = text.lower()
    return [n for n in needles if n.lower() in low]


def _contains_phrase(text: str, needles: list[str]) -> list[str]:
    low = text.lower()
    hits = []
    for needle in needles:
        parts = re.split(r"[\s-]+", needle.lower().strip())
        if not parts:
            continue
        pattern = r"(?<![a-z0-9])" + r"[\s-]+".join(map(re.escape, parts))
        pattern += r"(?![a-z0-9])"
        if re.search(pattern, low):
            hits.append(needle)
    return hits


def score_job(job: Job, cfg: Config) -> Job:
    profile = cfg.profile
    filters = cfg.filters
    haystack = f"{job.title}\n{job.description}\n{job.location}"

    reasons: list[str] = []

    # Hard exclude: anything matching drops straight to zero.
    hit_exclude = _contains_any(haystack, filters.exclude_keywords)
    if hit_exclude:
        job.score = 0.0
        job.reasons = [f"excluded by keyword: {', '.join(hit_exclude)}"]
        return job

    # Hard require: if a require list exists, at least one must be present.
    if filters.require_keywords and not _contains_any(haystack, filters.require_keywords):
        job.score = 0.0
        job.reasons = ["missing all required keywords"]
        return job

    # Hardware/facilities "… Design Engineer" roles: excluded on the TITLE only
    # (word-boundary), so "Mechanical/Physical/Electrical/Data Center Design
    # Engineer" drop out but a plain "Design Engineer" stays.
    hit_title_exclude = _contains_phrase(job.title, filters.title_exclude_keywords)
    if hit_title_exclude:
        job.score = 0.0
        job.reasons = [f"hardware/facilities role (title): {', '.join(hit_title_exclude)}"]
        return job

    score = 0.0

    # Title match against the roles you actually want (weight 0.45).
    title_hits = _contains_any(job.title, profile.titles)
    if title_hits:
        score += 0.45
        reasons.append(f"title matches: {', '.join(title_hits)}")

    role_hits = _contains_phrase(job.title, cfg.scoring.role_priority)
    if role_hits:
        best_idx = min(cfg.scoring.role_priority.index(hit) for hit in role_hits)
        denom = max(len(cfg.scoring.role_priority) - 1, 1)
        bonus = cfg.scoring.role_priority_bonus * (1 - best_idx / denom)
        score += bonus
        reasons.append(f"role priority: {role_hits[0]}")

    # Skill overlap (weight up to 0.40, scaled by how many of your skills hit).
    skill_hits = _contains_any(haystack, profile.skills)
    if profile.skills:
        frac = len(skill_hits) / len(profile.skills)
        score += 0.40 * min(frac * 2, 1.0)  # hitting half your skills maxes this
        if skill_hits:
            reasons.append(f"skills: {', '.join(skill_hits[:6])}")

    # Location fit (weight 0.15). Empty preference means location is fine.
    if not profile.locations:
        score += 0.15
    else:
        loc_hits = _contains_any(job.location or "", profile.locations)
        remote = "remote" in (job.location or "").lower()
        if loc_hits or remote:
            score += 0.15
            reasons.append("location fits" if loc_hits else "remote")
        else:
            reasons.append(f"location may not fit: {job.location or 'unknown'}")

    preferred_hits = _contains_phrase(
        f"{job.title}\n{job.description[:2000]}",
        cfg.scoring.preferred_keywords,
    )
    if preferred_hits:
        score += cfg.scoring.preferred_bonus
        reasons.append(f"preferred level: {', '.join(preferred_hits[:4])}")

    penalty_hits = _contains_phrase(job.title, cfg.scoring.penalty_keywords)
    if penalty_hits:
        score -= cfg.scoring.penalty
        reasons.append(f"level penalty: {', '.join(penalty_hits[:4])}")

    job.score = round(max(0.0, min(score, 1.0)), 2)
    job.reasons = reasons or ["no strong signals"]
    return job


def score_all(jobs: list[Job], cfg: Config) -> list[Job]:
    scored = [score_job(j, cfg) for j in jobs]
    scored.sort(key=lambda j: j.score, reverse=True)
    return scored
