"""Resolution of the column a bar starts at.

This is `get_due_column`'s mirror and it guards the same two traps, because detection
writes `date_columns.start` by the same mechanism that produced the `go_live: null`
failure of §16.6:

  * a key that *exists* holding None ignores `dict.get`'s default, so the default never
    applies and the lookup silently yields nothing;
  * a header scan that is too eager claims a column that is not a date at all.

The second trap is specific to this side. `schema_detect._structural_fallback` matches a
start column with the word list ["start", "created", "opened", "assigned"] — and
"assigned" is a substring of "Assigned To", which is a *people* column on most trackers.
Matching it here would draw every bar from a person's name.
"""

import pytest

from app.core.schema import get_start_column


# --- the null-default trap (the §16.6 shape, on the other end of the bar) ----

def test_explicit_null_start_does_not_resolve_to_a_column():
    assert get_start_column({"date_columns": {"start": None}}) is None


def test_a_mapped_start_column_wins_over_the_header_scan():
    headers = ["Kickoff", "Begin Work"]
    assert get_start_column({"date_columns": {"start": "Kickoff"}}, headers) == "Kickoff"


def test_blank_string_is_treated_as_unmapped():
    assert get_start_column({"date_columns": {"start": "  "}}, ["Start Date"]) == "Start Date"


# --- the header scan ---------------------------------------------------------

def test_header_scan_finds_a_start_column_when_the_schema_maps_nothing():
    headers = ["WRICEF No.", "Start Date", "Expected Completetion Date", "Status"]
    assert get_start_column({}, headers) == "Start Date"


def test_header_scan_returns_the_verbatim_header():
    """Trailing whitespace is real data and is used as a lookup key."""
    assert get_start_column({}, ["Start Date "]) == "Start Date "


def test_header_scan_is_case_insensitive():
    assert get_start_column({}, ["KICKOFF DATE"]) == "KICKOFF DATE"


def test_raised_on_is_recognised_as_a_start():
    """The wording schema_detect's own prompt example uses."""
    assert get_start_column({}, ["ID", "Raised On", "Status"]) == "Raised On"


# --- what must NOT be treated as a start -------------------------------------

def test_a_people_column_is_never_claimed_as_a_start_date():
    """"assigned" is a substring of "Assigned To". Matching it draws every bar from a name."""
    assert get_start_column({}, ["ID", "Assigned To", "Status"]) is None


def test_a_completion_column_is_never_a_start():
    assert get_start_column({}, ["ID", "Date Completion", "Sign-Off Date"]) is None


def test_no_start_column_reports_none_rather_than_guessing():
    assert get_start_column({}, ["ID", "Owner", "Notes"]) is None


# --- the collision with _DUE_WORDS -------------------------------------------

def test_the_resolved_due_column_is_never_also_returned_as_the_start():
    """"Planned Start" contains "planned", which is in _DUE_WORDS. If get_due_column has
    already claimed it, returning it here too gives every row a zero-length bar — which
    renders as data rather than as the mapping error it is."""
    headers = ["ID", "Planned Start", "Status"]
    assert get_start_column({}, headers, exclude="Planned Start") is None


def test_exclusion_matching_ignores_whitespace_and_case():
    headers = ["ID", "Start Date "]
    assert get_start_column({}, headers, exclude="start date") is None


def test_exclusion_does_not_suppress_a_genuinely_different_column():
    headers = ["ID", "Start Date", "Due Date"]
    assert get_start_column({}, headers, exclude="Due Date") == "Start Date"
