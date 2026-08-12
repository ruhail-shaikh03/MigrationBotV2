"""Pure logic behind the dashboard read endpoints.

The endpoints themselves need Postgres, Redis and a Sheets service, so what is covered
here is the row shaping, filtering and aggregation — the parts that decide whether a PM
sees the right numbers.
"""

from datetime import date, timedelta

import pytest

from app.api.dashboard import _column_descriptors, _is_overdue, _matches, _row_dicts

TECH = "Technical Resource "
FUNC = "Functinal Resource "


def _schema():
    return {
        "status_column": "Dev Status",
        "people_columns": [
            {"key": "technical_resource", "label": "Technical Resource", "header": TECH},
            {"key": "functinal_resource", "label": "Functinal Resource", "header": FUNC},
        ],
        "effort_columns": [{"key": "est_days", "label": "Est. Days", "header": "Est Days", "unit": "days"}],
    }


# --- row shaping ------------------------------------------------------------

def test_short_rows_are_padded_not_truncated():
    """Sheets omits trailing empty cells; dropping columns to match would hide real ones."""
    headers = ["ID", "Description", "Dev Status"]
    rows = [["SD-001"], ["SD-002", "Pricing"]]
    out = _row_dicts(headers, rows)
    assert out[0] == {"ID": "SD-001", "Description": "", "Dev Status": ""}
    assert out[1]["Description"] == "Pricing"


def test_blank_headers_are_dropped():
    out = _row_dicts(["ID", "", "Status"], [["a", "junk", "b"]])
    assert out[0] == {"ID": "a", "Status": "b"}


def test_row_dicts_handles_rows_longer_than_headers():
    out = _row_dicts(["ID"], [["a", "extra"]])
    assert out[0] == {"ID": "a"}


# --- column descriptors -----------------------------------------------------

def test_people_columns_are_marked_and_labelled_from_the_sheet():
    cols = _column_descriptors(_schema(), ["ID", TECH.strip(), FUNC.strip(), "Est Days"])
    by_header = {c["header"]: c for c in cols}
    assert by_header[TECH.strip()]["kind"] == "person"
    assert by_header[TECH.strip()]["label"] == "Technical Resource"
    assert by_header[FUNC.strip()]["kind"] == "person"
    assert by_header["Est Days"]["kind"] == "effort"
    assert by_header["ID"]["kind"] == "plain"


def test_descriptor_matching_survives_header_whitespace():
    """get_header_row strips headers while schema headers keep their trailing space."""
    cols = _column_descriptors(_schema(), ["Technical Resource"])
    assert cols[0]["kind"] == "person"


def test_legacy_assignee_only_project_still_marks_its_one_role():
    cols = _column_descriptors({"assignee_column": TECH}, ["ID", "Technical Resource"])
    assert [c["kind"] for c in cols] == ["plain", "person"]


# --- overdue ----------------------------------------------------------------

def test_overdue_requires_a_past_date():
    tomorrow = date.today() + timedelta(days=1)
    assert _is_overdue(tomorrow, {}, None) is False


def test_overdue_true_when_past_and_unfinished():
    yesterday = date.today() - timedelta(days=1)
    assert _is_overdue(yesterday, {"Dev Status": "In Progress"}, "Dev Status") is True


@pytest.mark.parametrize("status", ["Done", "Completed", "CLOSED", "Deployed", "Cancelled"])
def test_finished_rows_are_not_overdue(status):
    yesterday = date.today() - timedelta(days=1)
    assert _is_overdue(yesterday, {"Dev Status": status}, "Dev Status") is False


@pytest.mark.parametrize("status", ["Go-Live Pending", "Pre-Live", "Awaiting Deployment"])
def test_unfinished_statuses_that_merely_read_as_final_stay_overdue(status):
    """Hiding an overdue item is the worse error: nobody chases what they cannot see."""
    yesterday = date.today() - timedelta(days=1)
    assert _is_overdue(yesterday, {"Dev Status": status}, "Dev Status") is True


def test_overdue_without_a_status_column_falls_back_to_the_date_alone():
    yesterday = date.today() - timedelta(days=1)
    assert _is_overdue(yesterday, {"Dev Status": "Done"}, None) is True


# --- filter helper ----------------------------------------------------------

def test_matches_is_case_insensitive_substring():
    assert _matches({"Dev Status": "In Progress"}, "Dev Status", "progress") is True
    assert _matches({"Dev Status": "In Progress"}, "Dev Status", "done") is False


def test_matches_is_a_noop_without_a_header_or_needle():
    """An unmapped status column must not silently filter every row away."""
    assert _matches({"a": "b"}, None, "anything") is True
    assert _matches({"a": "b"}, "a", "") is True
