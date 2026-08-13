"""Resolution of the deadline column that "overdue" is measured against.

The bug these guard, observed in production on two separate trackers: every tab had
`date_columns.go_live = null`, and `read.py` resolved it with
`date_cols.get("go_live", "Go-Live Date")`. A key that *exists* holding None ignores the
default, so the lookup returned None, `summarize(report_type="overdue")` failed on every
project, and the agent burned all eight iterations guessing at date substrings through
search_rows before giving up with no answer at all.

The second half of the bug: detection had no slot for a plain due date. The sheet's
deadline column, "Expected Completetion Date", mapped nowhere, while "Date Completion" —
when work actually finished — was mapped as `completion`.
"""

import pytest

from app.core.overdue import is_finished_status
from app.core.schema import get_due_column
from app.core.schema_detect import _structural_fallback


# --- the null-default trap --------------------------------------------------

def test_explicit_null_go_live_does_not_resolve_to_a_column():
    """`.get(key, default)` returns the stored None; the default never applies."""
    assert get_due_column({"date_columns": {"go_live": None, "completion": "Date Completion"}}) is None


def test_null_go_live_falls_back_to_a_due_looking_header():
    """This is what unbreaks the projects already in production — nothing re-detects them."""
    headers = ["WRICEF No.", "Expected Completetion Date", "Status", "Date Completion"]
    schema = {"date_columns": {"go_live": None, "completion": "Date Completion"}}
    assert get_due_column(schema, headers) == "Expected Completetion Date"


def test_a_mapped_due_column_wins_over_the_header_scan():
    headers = ["Target Date", "Expected Finish"]
    assert get_due_column({"date_columns": {"due": "Target Date"}}, headers) == "Target Date"


def test_due_is_preferred_over_go_live_when_both_are_mapped():
    schema = {"date_columns": {"due": "Deadline", "go_live": "Go-Live Date"}}
    assert get_due_column(schema) == "Deadline"


# --- what must NOT be treated as a deadline ---------------------------------

def test_an_actual_completion_date_is_never_used_as_a_deadline():
    """Measuring against when work finished reports finished work as overdue."""
    headers = ["ID", "Date Completion", "Sign-Off Date", "Status"]
    assert get_due_column({"date_columns": {}}, headers) is None


def test_no_due_column_reports_none_rather_than_guessing():
    assert get_due_column({}, ["ID", "Owner", "Notes"]) is None


def test_header_scan_returns_the_verbatim_header():
    """Trailing whitespace is real data and must survive — it is used as a lookup key."""
    assert get_due_column({}, ["Due Date "]) == "Due Date "


def test_header_scan_is_case_insensitive():
    assert get_due_column({}, ["TARGET DATE"]) == "TARGET DATE"


def test_blank_string_is_treated_as_unmapped():
    assert get_due_column({"date_columns": {"due": "  "}}, ["Deadline"]) == "Deadline"


# --- detection ---------------------------------------------------------------

def test_structural_fallback_maps_expected_completion_as_due_not_completion():
    """The word "expected" decides it: a deadline, not a record of finished work."""
    headers = ["WRICEF No.", "Status", "Expected Completetion Date", "Date Assigned"]
    dates = _structural_fallback([headers])["date_columns"]
    assert dates["due"] == "Expected Completetion Date"
    # Falsy entries are dropped from the returned mapping, so "absent" is the assertion.
    assert "completion" not in dates


def test_detection_keeps_a_real_completion_column_separate_from_the_deadline():
    headers = ["ID", "Target Date", "Completion Date", "Status"]
    dates = _structural_fallback([headers])["date_columns"]
    assert dates["due"] == "Target Date"
    assert dates["completion"] == "Completion Date"


def test_go_live_still_detected_when_that_is_the_wording():
    dates = _structural_fallback([["ID", "Go-Live Date", "Status"]])["date_columns"]
    assert dates["go_live"] == "Go-Live Date"


def test_detected_schema_round_trips_through_the_resolver():
    headers = ["WRICEF No.", "Expected Completetion Date", "Date Completion", "Status"]
    schema = _structural_fallback([headers])
    assert get_due_column(schema, headers) == "Expected Completetion Date"


# --- "reads as finished" ----------------------------------------------------
#
# summarize compared the status to its exclusion list by exact equality while the
# dashboard matched substrings. The reference tracker's status is "Completed" and the
# exclusion list said "Complete", so chat reported all 171 finished rows as overdue and
# the dashboard did not. Both now route through core/overdue.py.

@pytest.mark.parametrize("status", ["Completed", "Complete", "COMPLETED", "Dev Done", "Closed - duplicate", "Cancelled", "Retired"])
def test_finished_statuses_are_excluded(status):
    assert is_finished_status(status) is True


@pytest.mark.parametrize("status", ["Not Started", "in process", "Hold", "In Testing", "Needs to be moved to PRD", ""])
def test_unfinished_statuses_from_the_real_tracker_stay_overdue(status):
    assert is_finished_status(status) is False


def test_go_live_pending_is_not_finished():
    """"live" is deliberately not a finished-word — this is an unfinished state."""
    assert is_finished_status("Go-Live Pending") is False


def test_caller_supplied_exclusions_replace_the_defaults():
    assert is_finished_status("In Review", ["In Review"]) is True
    assert is_finished_status("Completed", ["In Review"]) is False


def test_supplied_exclusions_still_match_inflections():
    """Callers name the state, not its exact spelling in every row."""
    assert is_finished_status("Signed Off", ["signed off"]) is True
    assert is_finished_status("Completed", ["Complete"]) is True
