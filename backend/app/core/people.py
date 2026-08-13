"""Resolution of *who* a row is assigned to and *how much* work it represents.

A tracking sheet names its own roles. One sheet has "Technical Resource " and
"Functinal Resource " (both typo and trailing space are real data); another has a single
"Owner"; another has "Developer", "QA Lead" and "Business Owner". Before this module the
schema carried exactly one `assignee_column`, so detection picked one people-column and
silently discarded the rest — on the reference sheet that made every functional
consultant invisible to the app.

The rule this module encodes: the app knows there is a *set* of people-columns and a
*set* of effort-columns. It never knows what they are called. Labels come from the
sheet and are carried through to the UI verbatim.
"""

import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

_WHITESPACE = re.compile(r"\s+")
_NON_SLUG = re.compile(r"[^a-z0-9]+")

# Delimiters that unambiguously separate two people. A comma is deliberately absent:
# "Shaikh, Rohail" is plausibly one person written surname-first, and inventing a
# colleague is a worse failure than under-splitting a shared assignment. A slash or an
# ampersand has no such reading.
_CELL_DELIMITERS = re.compile(r"[/&]+")

# Ordered, tried in sequence. Ambiguous all-numeric dates (03/04/2026) are read
# day-first, matching the sheets this runs against; the format list is the single place
# to change that. Anything unparseable yields None rather than a guess — a wrong date
# silently becomes a wrong effort number, and an absent metric is better than a fabricated one.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%b %d, %Y",
    "%B %d, %Y",
)


def normalise_person(raw: Any) -> str:
    """Canonical comparison key for a person cell.

    Trim, collapse internal whitespace, case-fold. Deliberately *exact* after that —
    no fuzzy or partial matching. "R. Shaikh" and "Rohail Shaikh" stay two people.
    Wrongly merging two colleagues into one workload row is silent data corruption and
    far worse than showing the same person twice, which a human spots immediately.
    An admin-maintained alias map is the right fix and is intentionally not guessed here.
    """
    if raw is None:
        return ""
    return _WHITESPACE.sub(" ", str(raw).strip()).casefold()


def display_person(raw: Any) -> str:
    """The person's name as the sheet spells it, whitespace-collapsed but not case-folded."""
    if raw is None:
        return ""
    return _WHITESPACE.sub(" ", str(raw).strip())


def split_cell(raw: Any) -> List[str]:
    """Every person named by one people-cell, in the order the sheet names them.

    Structural only: this knows delimiters and nothing else. It deliberately does not
    strip honorifics, titles or any other word — that would be one language's vocabulary
    compiled into the engine, on an app whose first constraint is working with any sheet.
    "Mr. Minhaj Alam" -> "Minhaj Alam" is expressible as an alias row instead, which is
    data an admin controls rather than code nobody can see.

    On the reference tracker 60 of 257 assigned cells name two people; before this each
    became its own workload entry, crediting a composite who does not exist.
    """
    text = display_person(raw)
    if not text:
        return []
    return [part for part in (display_person(p) for p in _CELL_DELIMITERS.split(text)) if part]


def slugify_role(label: str) -> str:
    """Stable key for a role label, used only in URLs and API filter params.

    Never used to look up a column — that always goes through the verbatim `header`.
    """
    return _NON_SLUG.sub("_", str(label).strip().casefold()).strip("_")


def parse_date(raw: Any) -> Optional[date]:
    """Parse a sheet date cell, or None if it cannot be read confidently."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_effort(raw: Any) -> Optional[float]:
    """Parse an effort cell to a number of units, or None.

    Tolerates a trailing unit ("5 days", "3d") and thousands separators, because sheets
    are typed by hand. Returns None for anything else rather than coercing to 0 — a
    zero-day workload reads as "this person has nothing on", which is a different claim.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    text = str(raw).strip().replace(",", "")
    if not text:
        return None
    match = re.match(r"^-?\d+(\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def resolve_effort(
    row: Dict[str, Any],
    people_effort_columns: List[Dict[str, Any]],
    date_columns: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[float], Optional[str]]:
    """How many days this row represents, and where that number came from.

    Returns (value, source) where source is "column", "dates", or None.

    Order:
      1. an explicit effort column, if the schema mapped one;
      2. else the span from a start date to a due/go-live date;
      3. else (None, None) — the caller must then show item *counts*, not days, and
         say so. Never synthesise a capacity figure; a fabricated number that looks
         authoritative is worse than an honest absence.
    """
    for col in people_effort_columns or []:
        header = col.get("header")
        if not header:
            continue
        value = parse_effort(row.get(header))
        if value is not None:
            return value, "column"

    dates = date_columns or {}
    start = parse_date(row.get(dates.get("start"))) if dates.get("start") else None
    end_header = dates.get("due") or dates.get("go_live") or dates.get("completion")
    end = parse_date(row.get(end_header)) if end_header else None
    if start and end and end >= start:
        return float((end - start).days), "dates"

    return None, None


def collect_assignments(
    row: Dict[str, Any],
    people_columns: List[Dict[str, Any]],
    resolver: Optional[Any] = None,
) -> List[Dict[str, str]]:
    """Every (role, person) pair named by this row.

    One cell can name several people and one person can hold several roles; each pairing
    is returned separately so callers can show a per-role split as well as a total.

    A cell used to be treated as a single value on the reasoning that splitting
    "Shaikh, Rohail" would invent two people who do not exist. That reasoning is about
    *commas*; it was over-applied to slashes and ampersands, and on the reference tracker
    60 of 257 assigned cells consequently credited a composite like "Minhaj Alam & Dawood"
    while the two real people got nothing. `split_cell` splits on / and & only.

    `resolver` is a `PersonResolver` applying the project's admin-confirmed alias map. It
    is optional and defaults to splitting alone, so attribution keeps working when the
    database is unreachable — degrading to unmerged names rather than to an error.
    """
    from app.core.aliases import PersonResolver  # local: aliases.py imports this module

    resolver = resolver or PersonResolver()
    out: List[Dict[str, str]] = []
    for col in people_columns or []:
        header = col.get("header")
        if not header:
            continue
        for name in resolver.resolve_cell(row.get(header)):
            out.append({
                "role_key": col.get("key") or slugify_role(col.get("label") or header),
                "role_label": col.get("label") or str(header).strip(),
                "person": name,
                # Keyed off the *resolved* name, or an alias would show a merged label
                # over totals that were never actually merged.
                "person_key": normalise_person(name),
            })
    return out
