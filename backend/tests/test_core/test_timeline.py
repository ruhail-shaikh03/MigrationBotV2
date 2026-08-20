"""Laying a tab's rows on a time axis.

The arithmetic is the easy half. What these guard is the honesty of the counts: on the
reference tracker roughly 40 of 412 rows carry both a start and an end date, so a chart
that quietly drops the rest reports a tenth of the sheet as if it were the sheet. Every
row lands in exactly one bucket and the buckets sum to the total, so nothing can go
missing without a count moving.

`undated` and `unparsed` are separate for the same reason core/health.py separates
`no_deadline` from `unreadable_date`: a value like "17/0/2026" looks filled in to a human
scanning the sheet and evaluates to nothing in every calculation.
"""

from datetime import date

from app.core.timeline import classify_row

START = "Start Date"
DUE = "Expected Completetion Date"


def _row(start="", due=""):
    return {START: start, DUE: due, "WRICEF No.": "W-1"}


# --- the four states ---------------------------------------------------------

def test_both_dates_present_and_readable_is_a_bar():
    kind, start, end = classify_row(_row("01/02/2026", "09/02/2026"), START, DUE)
    assert kind == "bar"
    assert start == date(2026, 2, 1)
    assert end == date(2026, 2, 9)


def test_only_a_due_date_is_a_milestone():
    kind, start, end = classify_row(_row("", "09/02/2026"), START, DUE)
    assert kind == "milestone"
    assert start == date(2026, 2, 9)
    assert end == date(2026, 2, 9)


def test_only_a_start_date_is_a_milestone():
    kind, start, end = classify_row(_row("01/02/2026", ""), START, DUE)
    assert kind == "milestone"
    assert start == date(2026, 2, 1)
    # Both ends carry the one readable value, as the docstring promises, so a caller can
    # position the point without asking which column it came from.
    assert end == date(2026, 2, 1)


def test_neither_date_is_undated():
    kind, start, end = classify_row(_row("", ""), START, DUE)
    assert kind == "undated"
    assert start is None and end is None


# --- the distinction that matters --------------------------------------------

def test_a_malformed_date_is_unparsed_not_undated():
    """"17/0/2026" is on the reference tracker. It is present, and it reads as nothing."""
    kind, _, _ = classify_row(_row("", "17/0/2026"), START, DUE)
    assert kind == "unparsed"


def test_unparsed_wins_over_a_readable_partner():
    """Drawing the readable half would hide the unreadable one, which is the whole point
    of having this state at all."""
    kind, start, end = classify_row(_row("17/0/2026", "09/02/2026"), START, DUE)
    assert kind == "unparsed"
    # And it yields no dates at all. Handing back the readable half would let a caller that
    # destructures the return draw the row anyway, which is the state's whole point undone.
    assert start is None and end is None


def test_whitespace_only_counts_as_absent_not_as_unparsed():
    kind, _, _ = classify_row(_row("   ", "  "), START, DUE)
    assert kind == "undated"


# --- date formats and column absence ------------------------------------------

def test_the_dotted_format_the_reference_sheet_uses_is_readable():
    kind, start, _ = classify_row(_row("10.05.2026", "12.05.2026"), START, DUE)
    assert kind == "bar"
    assert start == date(2026, 5, 10)


def test_a_tab_with_no_start_column_still_yields_milestones():
    kind, start, _ = classify_row({DUE: "09/02/2026"}, None, DUE)
    assert kind == "milestone"
    assert start == date(2026, 2, 9)


def test_a_tab_with_neither_column_is_undated():
    kind, _, _ = classify_row({"WRICEF No.": "W-1"}, None, None)
    assert kind == "undated"


def test_an_end_before_its_start_is_still_a_bar_and_is_not_silently_swapped():
    """The caller normalises for drawing and flags it; classification reports what the
    sheet actually says."""
    kind, start, end = classify_row(_row("09/02/2026", "01/02/2026"), START, DUE)
    assert kind == "bar"
    assert start == date(2026, 2, 9)
    assert end == date(2026, 2, 1)
