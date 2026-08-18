"""The sheet health assessment.

Two things are being guarded, and only one of them is arithmetic.

The first is that a check which cannot run must never be reported as a check that passed.
`DataQualityChecker` — the module this replaced on the dashboard path — returns `[]` from
`consistency_checks` and `100.0` from `completeness_score` when it can resolve none of the
headers it is looking for, so a tracker it knows nothing about scores perfectly. Most of
the tests below are about the difference between zero and unknown.

The second is header binding. schema_config stores headers verbatim, trailing spaces
included; row dicts are keyed by the stripped header row. An unbound lookup misses every
row, which does not raise, does not log, and reports the column as universally blank.
"""

import pytest

from app.core.health import assess_tab
from app.core.schema import bind_columns, resolve_header


# --- header binding, the silent failure -------------------------------------

def test_a_schema_header_with_a_trailing_space_binds_to_the_stripped_key():
    """The exact shape CLAUDE.md warns about: `"Technical Resource "` in schema_config."""
    assert resolve_header(["Technical Resource"], "Technical Resource ") == "Technical Resource"


def test_an_exact_match_wins_over_a_whitespace_insensitive_one():
    headers = ["Owner", "Owner "]
    assert resolve_header(headers, "Owner ") == "Owner "


def test_a_column_the_tab_does_not_have_resolves_to_none_not_to_itself():
    """Returning the wanted string would turn a missing column into an all-blank one."""
    assert resolve_header(["Status", "Owner"], "Deadline") is None


def test_bind_columns_drops_columns_the_tab_no_longer_has():
    bound = bind_columns(
        [{"key": "dev", "label": "Dev", "header": "Developer Name "},
         {"key": "qa", "label": "QA", "header": "QA Owner"}],
        ["Developer Name", "Status"],
    )
    assert [c["header"] for c in bound] == ["Developer Name"]


def test_unassigned_is_not_reported_for_a_column_that_merely_failed_to_bind():
    """Without binding this said 2 of 2 rows have nobody named, which is the opposite."""
    headers = ["ID", "Technical Resource"]
    rows = [
        {"ID": "W-1", "Technical Resource": "Sara Iqbal"},
        {"ID": "W-2", "Technical Resource": "Bilal Ahmed"},
    ]
    schema = {
        "primary_id_column": "ID",
        "people_columns": [{"key": "tech", "label": "Tech", "header": "Technical Resource "}],
    }
    report = assess_tab(headers, rows, schema)
    assert _check(report, "unassigned")["count"] == 0


# --- zero is not unknown -----------------------------------------------------

def test_a_tab_with_no_deadline_column_skips_the_check_rather_than_scoring_it():
    headers = ["Task", "Owner"]
    rows = [{"Task": "Migrate BOM", "Owner": "Sara"}]
    report = assess_tab(headers, rows, {})
    assert _check(report, "no_deadline") is None
    assert _skip(report, "no_deadline") is not None


def test_completeness_is_none_when_the_schema_names_no_critical_fields():
    """Not 100.0 — nobody has said what matters on this sheet, so there is nothing to score."""
    report = assess_tab(["A"], [{"A": ""}], {})
    assert report["completeness"] is None


def test_completeness_is_scored_over_the_declared_fields_only():
    headers = ["ID", "Owner", "Notes"]
    rows = [
        {"ID": "1", "Owner": "Sara", "Notes": ""},
        {"ID": "2", "Owner": "", "Notes": ""},
    ]
    report = assess_tab(headers, rows, {"critical_fields": ["ID", "Owner"]})
    assert report["completeness"]["score"] == 75.0
    assert report["completeness"]["cells"] == 4


# --- the checks themselves ---------------------------------------------------

@pytest.fixture
def tracker():
    """A tab shaped like the reference trackers, including their actual defects."""
    headers = ["WRICEF No.", "Dev Status", "Developer Name", "Expected Completetion Date"]
    schema = {
        "primary_id_column": "WRICEF No.",
        "status_column": "Dev Status",
        "people_columns": [{"key": "dev", "label": "Developer", "header": "Developer Name"}],
        "date_columns": {"due": "Expected Completetion Date"},
    }
    rows = [
        {"WRICEF No.": "W-1", "Dev Status": "Completed", "Developer Name": "Sara Iqbal",
         "Expected Completetion Date": "2026-01-15"},
        {"WRICEF No.": "W-2", "Dev Status": "", "Developer Name": "",
         "Expected Completetion Date": ""},
        {"WRICEF No.": "", "Dev Status": "In Progress", "Developer Name": "Bilal Ahmed",
         "Expected Completetion Date": "17/0/2026"},
        {"WRICEF No.": "W-1", "Dev Status": "In Progress", "Developer Name": "Sara",
         "Expected Completetion Date": "2026-03-01"},
    ]
    return headers, rows, schema


def test_rows_without_an_id_are_counted(tracker):
    assert _check(assess_tab(*tracker), "no_id")["count"] == 1


def test_a_repeated_id_counts_every_row_that_shares_it(tracker):
    """Two rows, not one: both are ambiguous, and a write could land on either."""
    assert _check(assess_tab(*tracker), "duplicate_id")["count"] == 2


def test_a_date_that_cannot_be_parsed_is_reported_separately_from_a_blank(tracker):
    headers, rows, schema = tracker
    report = assess_tab(headers, rows, schema)
    unreadable = _check(report, "unreadable_date")
    assert unreadable["count"] == 1
    assert unreadable["samples"][0]["value"] == "17/0/2026"
    # And it is not also counted as missing — the cell is not blank.
    assert _check(report, "no_deadline")["count"] == 1


def test_samples_carry_the_spreadsheet_row_number(tracker):
    headers, rows, schema = tracker
    report = assess_tab(headers, rows, schema, data_start_row=3)
    # The unreadable date is the third data row, so row 5 in the sheet.
    assert _check(report, "unreadable_date")["samples"][0]["row"] == 5


def test_near_duplicate_names_are_surfaced_as_a_prompt_not_a_defect(tracker):
    """"Sara" and "Sara Iqbal" are offered for review; nothing is merged here."""
    check = _check(assess_tab(*tracker), "duplicate_people")
    assert check["count"] == 1
    assert check["samples"][0]["column"] == "Sara"


def test_checks_are_ordered_worst_first(tracker):
    severities = [c["severity"] for c in assess_tab(*tracker)["checks"]]
    order = {"error": 0, "warning": 1, "info": 2, "ok": 3}
    assert severities == sorted(severities, key=lambda s: order[s])


def test_severity_reflects_the_share_of_the_tab_not_the_raw_count():
    """One missing deadline in four rows is a footnote; in two rows it is not."""
    schema = {"date_columns": {"due": "Due"}}
    small = assess_tab(["Due"], [{"Due": ""}, {"Due": "2026-01-01"}], schema)
    assert _check(small, "no_deadline")["severity"] == "error"

    rows = [{"Due": ""}] + [{"Due": "2026-01-01"}] * 19
    large = assess_tab(["Due"], rows, schema)
    assert _check(large, "no_deadline")["severity"] == "info"


def test_an_empty_tab_produces_no_division_error():
    report = assess_tab(["ID"], [], {"primary_id_column": "ID"})
    assert report["total_rows"] == 0
    assert _check(report, "no_id")["severity"] == "ok"


# --- roles the schema never learned about --------------------------------------
#
# The failure being made visible: detection reads value shape now, so a re-detected tab
# finds its people columns whatever they are called — but nothing re-detects an existing
# project, and until someone does, the UI cannot tell "this sheet has one resource column"
# from "this sheet was never re-detected" (TDD §16.3). Nobody had a reason to go and look.
#
# Fixtures are named by shape, never by the sheet they came from.

_NAMES = ["Muhammad Zeshan Ayub", "Madiha Shah Bukhari", "Huzaima Ather", "Taymoor Ahmed",
          "Neeha Nehal", "Amna Lateef Korejo", "Arjmand Bano", "Babar Ali"]
_CODES = ["AROR", "AWKUM", "BNWU", "CUVAS", "EUM", "GUDGK", "HSA"]
_STATES = ["Completed", "Not Started", "In Testing", "Hold"]


def _tab(mapped_people=(), extra_columns=None):
    """A tab of 120 rows with an ID, a status, a mapped-or-not people column and extras."""
    columns = {"Second Owner": _NAMES, "Dev Status": _STATES}
    columns.update(extra_columns or {})
    headers = ["WRICEF No."] + list(columns)
    rows = [
        {"WRICEF No.": f"W-{i}", **{h: v[i % len(v)] for h, v in columns.items()}}
        for i in range(120)
    ]
    schema = {
        "primary_id_column": "WRICEF No.",
        "status_column": "Dev Status",
        "people_columns": [
            {"key": h.lower().replace(" ", "_"), "label": h, "header": h} for h in mapped_people
        ],
    }
    return headers, rows, schema


def test_a_person_shaped_column_nobody_mapped_is_flagged():
    report = assess_tab(*_tab())
    check = _check(report, "unmapped_people")
    assert check["count"] == 1
    assert check["samples"][0]["column"] == "Second Owner"


def test_the_same_column_is_silent_once_it_is_mapped():
    report = assess_tab(*_tab(mapped_people=["Second Owner"]))
    assert _check(report, "unmapped_people")["count"] == 0


def test_nothing_mapped_at_all_is_an_error_not_a_warning():
    """Every person-shaped number on the tab is empty rather than partial."""
    assert _check(assess_tab(*_tab()), "unmapped_people")["severity"] == "error"


def test_a_second_missed_column_beside_a_mapped_one_is_a_warning():
    headers, rows, schema = _tab(
        mapped_people=["Developer Name"], extra_columns={"Developer Name": _NAMES[::-1]}
    )
    assert _check(assess_tab(headers, rows, schema), "unmapped_people")["severity"] == "warning"


def test_severity_does_not_soften_just_because_the_sheet_is_wide():
    """A proportion is the wrong measure: two missed columns out of forty is 5% of the
    columns and 100% of the missing assignments."""
    padding = {f"Note {i}": [""] for i in range(36)}
    report = assess_tab(*_tab(extra_columns=padding))
    assert _check(report, "unmapped_people")["severity"] == "error"


def test_a_status_column_is_not_mistaken_for_people():
    assert _check(assess_tab(*_tab(mapped_people=["Second Owner"])), "unmapped_people")["count"] == 0


def test_short_uppercase_codes_are_not_flagged():
    headers, rows, schema = _tab(mapped_people=["Second Owner"], extra_columns={"Site": _CODES})
    assert _check(assess_tab(headers, rows, schema), "unmapped_people")["count"] == 0


def test_free_text_is_not_flagged():
    descriptions = [f"Migrate the {w} report from legacy into the new system" for w in
                    ("billing", "hostel", "transcript", "admission", "payroll")]
    headers, rows, schema = _tab(
        mapped_people=["Second Owner"], extra_columns={"Description": descriptions}
    )
    assert _check(assess_tab(headers, rows, schema), "unmapped_people")["count"] == 0


def test_a_tab_too_short_to_judge_flags_nothing():
    """Below the profiler's minimum sample the statistics are noise, not evidence."""
    headers = ["WRICEF No.", "Second Owner"]
    rows = [{"WRICEF No.": f"W-{i}", "Second Owner": _NAMES[i]} for i in range(5)]
    assert _check(assess_tab(headers, rows, {}), "unmapped_people")["count"] == 0


def test_the_detail_says_what_the_missing_column_costs():
    check = _check(assess_tab(*_tab()), "unmapped_people")
    assert "workload" in check["detail"] and "Admin" in check["detail"]


# --- helpers -----------------------------------------------------------------

def _check(report, key):
    return next((c for c in report["checks"] if c["key"] == key), None)


def _skip(report, key):
    return next((s for s in report["skipped"] if s["key"] == key), None)
