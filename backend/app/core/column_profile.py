"""Deciding whether a column holds people, from its values and nothing else.

Schema detection used to reason about header wording. That is the least generic signal
a tracking sheet offers: "Consultant" reads like a job title in English and was skipped,
while the next sheet will say Owner, Assigned To, Resource, PIC, or a header in another
language entirely. Value *shape* is the same on every sheet in every domain.

Nothing here ever sees a column name. It takes a list of strings and returns statistics,
which is exactly what lets it work on a sheet this code has never been shown.

Thresholds were fitted against every populated column of two real trackers. An earlier
draft used cardinality and token-count alone and misclassified a column of short
uppercase site codes as people; mean_length and title_case_ratio are what separate a
person's name from a category code.
"""

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

# Below this many non-blank values the statistics are noise, not evidence. One real tab
# has a people column with five entries; classifying from that is guessing.
MIN_SAMPLE = 20

# Letters, spaces and the punctuation that legitimately appears inside names. Digits,
# URLs, dates and code identifiers all fail it.
_ALPHA_ONLY = re.compile(r"^[^\W\d_][\w\s.'\-/&]*$", re.UNICODE)
_TITLE_START = re.compile(r"^[^\W\d_]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class ColumnProfile:
    """Shape statistics for one column's non-blank values.

    Fields are deliberately independent rather than pre-combined into a single score.
    title_case_ratio and mean_tokens carry Latin-script assumptions; keeping them
    separate means a CJK or Arabic sheet can be handled by re-weighting rather than by
    rewriting, when one turns up.
    """
    n: int
    cardinality: float
    mean_tokens: float
    mean_length: float
    alpha_ratio: float
    title_case_ratio: float
    repeat_ratio: float


def _clean(values: Iterable[Any]) -> List[str]:
    out = []
    for value in values or []:
        if value is None:
            continue
        text = _WHITESPACE.sub(" ", str(value).strip())
        if text:
            out.append(text)
    return out


def _is_title_case(text: str) -> bool:
    """First character is an upper-case letter and the value is not shouted.

    A name is "Babar Ali"; a site code is "AWKUM". Comparing against .upper() catches the
    all-caps case without assuming anything about length.
    """
    if not _TITLE_START.match(text):
        return False
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    return letters[0].isupper() and text != text.upper()


def profile_column(values: Iterable[Any]) -> ColumnProfile:
    """Shape statistics over the non-blank values of one column."""
    cleaned = _clean(values)
    n = len(cleaned)
    if n == 0:
        return ColumnProfile(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    folded = [v.casefold() for v in cleaned]
    counts: Dict[str, int] = {}
    for value in folded:
        counts[value] = counts.get(value, 0) + 1

    return ColumnProfile(
        n=n,
        cardinality=len(counts) / n,
        mean_tokens=sum(len(v.split(" ")) for v in cleaned) / n,
        mean_length=sum(len(v) for v in cleaned) / n,
        alpha_ratio=sum(1 for v in cleaned if _ALPHA_ONLY.match(v)) / n,
        title_case_ratio=sum(1 for v in cleaned if _is_title_case(v)) / n,
        repeat_ratio=sum(1 for v in folded if counts[v] > 1) / n,
    )


def _criteria(profile: ColumnProfile) -> List[bool]:
    """Each criterion, evaluated separately so near-misses can be told from rejections."""
    return [
        0.02 <= profile.cardinality <= 0.6,   # not a status enum; not a unique-per-row id
        1.5 <= profile.mean_tokens <= 4.0,    # names are multi-token; category codes are not
        8.0 <= profile.mean_length <= 30.0,   # longer than a code, shorter than free text
        profile.alpha_ratio >= 0.8,           # no digits, URLs or dates
        profile.title_case_ratio >= 0.6,      # consistently capitalised, not shouted
        profile.repeat_ratio >= 0.3,          # people recur across rows
    ]


def people_confidence(profile: ColumnProfile) -> str:
    """"likely", "abstain" or "unlikely" — never a bare boolean.

    "abstain" is not a vote. Callers must treat it as neither applying a role nor
    blocking one; only "likely" is a positive claim. It covers two cases: too little
    data to judge, and a column that misses by exactly one criterion — which is where a
    small team and a short status enum become indistinguishable, and where the LLM's
    reading of meaning has to decide instead.
    """
    if profile.n < MIN_SAMPLE:
        return "abstain"
    failed = _criteria(profile).count(False)
    if failed == 0:
        return "likely"
    if failed == 1:
        return "abstain"
    return "unlikely"
