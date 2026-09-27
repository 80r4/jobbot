"""Filtering + scoring. Returns None if a job is rejected, else a score (higher = better)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from .sources import Job


@lru_cache(maxsize=512)
def _rx(term: str) -> re.Pattern:
    # whole-word, case-insensitive; works for terms like "R&D" and "10+ years"
    return re.compile(r"(?<!\w)" + re.escape(term.lower()) + r"(?!\w)")


def _has(text: str, term: str) -> bool:
    return bool(_rx(term).search(text))


def _age_days(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - d) / timedelta(days=1)


def score(job: Job, f: dict) -> int | None:
    title = job.title.lower()
    text = f"{title} {job.description.lower()}"
    loc = job.location.lower()

    if any(_has(title, t) for t in f.get("exclude_title_any", [])):
        return None
    if any(_has(text, t) for t in f.get("exclude_any", [])):
        return None

    title_any = f.get("title_any", [])
    if title_any and not any(_has(title, t) for t in title_any):
        return None

    locs = f.get("locations_any", [])
    if locs and not any(_has(loc, t) for t in locs):
        return None

    max_age = f.get("max_age_days")
    age = _age_days(job.posted_at)
    if max_age and age is not None and age > max_age:
        return None

    kws = f.get("keywords_any", [])
    hits = sum(1 for k in kws if _has(text, k))
    if kws and hits < int(f.get("min_keyword_hits", 1)):
        return None

    # score: keyword hits, bonus for keyword in title, bonus for freshness
    s = hits * 10 + sum(5 for k in kws if _has(title, k))
    if age is not None:
        s += max(0, 10 - int(age))
    return s
