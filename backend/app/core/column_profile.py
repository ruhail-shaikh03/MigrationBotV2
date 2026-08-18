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

import random
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

# Below this many non-blank values the statistics are noise, not evidence. One real tab
# has a people column with five entries; classifying from that is guessing.
MIN_SAMPLE = 20

# Statistics are taken over at most this many values, drawn from across the whole column
# rather than from the top of it.
#
# Not a performance guard — a scale correction. `cardinality` is distinct-over-total, so a
# column naming 33 people measures 0.08 on a 400-row tab and 0.0017 on a 20 000-row one,
# which fails the 0.02 floor. The same column, on the same kind of sheet, would be
# recognised on the small tracker and silently rejected on the large one. Capping makes the
# figure mean "distinct values per sample" and therefore comparable between sheets, which is
# the whole premise of profiling by shape.
#
# 1000 is chosen against the floor it has to clear, not picked round: at that size the 0.02
# cardinality minimum reads as "at least 20 distinct values in a full sample", which is a
# scale-free statement of the thing the floor is actually for — telling a roster of people
# from a short status enum. A larger cap re-introduces the bug at a larger sheet size (33
# people in a 2000-value sample measures 0.0165, still under the floor).
#
# How the values are drawn matters as much as the cap. Taking the first N samples one module
# or one team and reads their repetition as the sheet's. Taking every Nth is worse: sheet
# data is periodic — grouped by module, assigned round-robin — and a stride that shares a
# factor with that period aliases. Thirty-three names cycling, sampled every third row, yields
# eleven distinct values and a cardinality a third of the truth. So the sample is drawn
# pseudo-randomly from a fixed seed: immune to period, and identical run to run, which the
# health panel and detection both need in order to agree with each other.
MAX_SAMPLE = 1000

# Fixed, so a profile is reproducible. A verdict that changed between two reads of the same
# unchanged sheet would be untraceable to anything.
_SAMPLE_SEED = 20260818

# Letters, spaces and the punctuation that legitimately appears inside names. Digits,
# URLs, dates and code identifiers all fail it.
_ALPHA_ONLY = re.compile(r"^[^\W\d_][\w\s.'\-/&]*$", re.UNICODE)
_TITLE_START = re.compile(r"^[^\W\d_]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


# A roster has a long tail; a workflow has a short fixed list. Four statuses are four
# whether the tab has a hundred rows or a hundred thousand, so an *absolute* count of
# distinct values separates them where the cardinality *ratio* cannot.
#
# The ratio alone was doing this job by accident. "Completed / Not Started / In Testing /
# Hold" measures 0.01 across 400 rows and is rejected, and 0.033 across 120 rows and is
# accepted as people — the same four labels, classified differently because the sheet was
# shorter. Every criterion here is meant to describe the values; that one described the
# row count.
#
# Set at 8 rather than higher because a small team is a real thing. Below it the column
# misses one criterion and lands on "abstain", which is the honest answer: a team of four
# and a workflow of four states are not distinguishable by shape, only by meaning.
#
# This replaced a *lower bound* on the cardinality ratio, which stated the same idea in a
# scale-dependent way: `cardinality >= 0.02` demands 8 distinct values in a 412-row tab and
# 20 in a 1000-value sample, so a column naming eight people was recognised on a small tab
# and rejected on a large one. Only the *upper* bound survives, and it does a different
# job — rejecting a column with a unique value per row.
MIN_DISTINCT = 8


@dataclass(frozen=True)
class ColumnProfile:
    """Shape statistics for one column's non-blank values.

    Fields are deliberately independent rather than pre-combined into a single score.
    title_case_ratio and mean_tokens carry Latin-script assumptions; keeping them
    separate means a CJK or Arabic sheet can be handled by re-weighting rather than by
    rewriting, when one turns up.
    """
    n: int
    distinct: int
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


def _sample(values: List[str], cap: int) -> List[str]:
    """At most `cap` values, drawn from across the column and stable between runs."""
    if cap <= 0 or len(values) <= cap:
        return values
    return random.Random(_SAMPLE_SEED).sample(values, cap)


def profile_column(values: Iterable[Any], cap: int = MAX_SAMPLE) -> ColumnProfile:
    """Shape statistics over the non-blank values of one column.

    Blanks are removed before sampling, so a mostly-empty column still contributes its full
    quota of real values rather than a sample of nothing.
    """
    cleaned = _sample(_clean(values), cap)
    n = len(cleaned)
    if n == 0:
        return ColumnProfile(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    folded = [v.casefold() for v in cleaned]
    counts: Dict[str, int] = {}
    for value in folded:
        counts[value] = counts.get(value, 0) + 1

    return ColumnProfile(
        n=n,
        distinct=len(counts),
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
        profile.distinct >= MIN_DISTINCT,     # a roster, not a fixed workflow vocabulary
        profile.cardinality <= 0.6,           # values recur; not a unique identifier per row
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
