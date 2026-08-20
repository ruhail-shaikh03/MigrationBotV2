"""A tab's rows laid out on a time axis, expressed only in terms the tab declares.

The dashboard already answers "what is the state of the work" and the health panel
answers "how much of that state is recorded". This answers "when" — and it inherits the
health panel's problem directly, because on the reference tracker roughly 40 of 412 rows
carry both a start and an end date. A chart drawn over those 40 and captioned with
nothing reports a tenth of a tracker as though it were the tracker.

So every row lands in exactly one of four buckets and the buckets sum to the total:

    bar        both dates present and readable
    milestone  exactly one present and readable
    undated    neither cell has content
    unparsed   a cell has content that will not parse

`undated` and `unparsed` are separate for the same reason core/health.py separates
`no_deadline` from `unreadable_date`. A value like "17/0/2026" — real, on the reference
sheet — looks filled in to a human and evaluates to nothing in every calculation, so
folding it into "undated" hides precisely the defect worth surfacing.

No header string is spelled in this module. Every column arrives from a schema role.
"""

from datetime import date
from typing import Dict, Optional, Tuple

from app.core.people import parse_date

# Matches ToolResultCard's threshold for switching a bar chart to a table (§14.2): past a
# dozen categories the reader is scanning a list, not comparing magnitudes.
MAX_GROUPS = 12

OTHER_GROUP_LABEL = "Other"
UNGROUPED_LABEL = "All rows"
BLANK_GROUP_LABEL = "(blank)"
UNASSIGNED_GROUP_LABEL = "Unassigned"


def classify_row(
    row: Dict[str, str],
    start_header: Optional[str],
    due_header: Optional[str],
) -> Tuple[str, Optional[date], Optional[date]]:
    """Which bucket this row falls in, and the dates it yielded.

    Returns `(kind, start, end)`. For a milestone both dates are the one readable value,
    so a caller can position it without asking which end it came from.

    Two precedence rules are deliberate:

    * **`unparsed` beats a readable partner.** A row with a good due date and a junk start
      date is reported as unreadable rather than drawn as a milestone. Drawing it would
      hide the unreadable cell, and surfacing that cell is the only reason this state
      exists. The cost is one row missing from the chart; the alternative is a chart that
      launders bad data into clean-looking output.
    * **An end before its start is still a bar, unswapped.** Classification reports what
      the sheet says. The caller normalises the pair for drawing and flags it, so the
      anomaly stays visible instead of being silently corrected into plausibility.
    """
    raw_start = row.get(start_header, "") if start_header else ""
    raw_end = row.get(due_header, "") if due_header else ""

    has_start = bool(str(raw_start).strip())
    has_end = bool(str(raw_end).strip())

    if not has_start and not has_end:
        return "undated", None, None

    parsed_start = parse_date(raw_start) if has_start else None
    parsed_end = parse_date(raw_end) if has_end else None

    if (has_start and parsed_start is None) or (has_end and parsed_end is None):
        return "unparsed", None, None

    if parsed_start is not None and parsed_end is not None:
        return "bar", parsed_start, parsed_end

    only = parsed_start if parsed_start is not None else parsed_end
    return "milestone", only, only
