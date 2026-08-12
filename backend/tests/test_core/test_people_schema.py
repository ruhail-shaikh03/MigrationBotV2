"""Multi-role people/effort resolution and its backward compatibility.

The bug these guard: schema detection used to map a single `assignee_column`, so a sheet
with both a technical and a functional resource column had one of them silently dropped.
"""

import pytest

from app.core.people import (
    collect_assignments,
    display_person,
    normalise_person,
    parse_date,
    parse_effort,
    resolve_effort,
    slugify_role,
)
from app.core.schema import get_effort_columns, get_people_columns
from app.core.schema_detect import _normalise_people_and_effort, _structural_fallback


# --- backward compatibility -------------------------------------------------

def test_legacy_assignee_column_synthesises_single_role():
    """A project registered before people_columns existed still yields one role.

    Nothing re-detects old projects automatically, so without this they would render
    no people at all rather than the single role they have always had.
    """
    people = get_people_columns({"assignee_column": "Technical Resource "})
    assert len(people) == 1
    assert people[0]["header"] == "Technical Resource "  # verbatim, trailing space intact
    assert people[0]["label"] == "Technical Resource"    # stripped for display only
    assert people[0]["key"] == "technical_resource"


def test_people_columns_take_precedence_over_legacy_key():
    tab = {
        "assignee_column": "Technical Resource ",
        "people_columns": [
            {"label": "Technical Resource", "header": "Technical Resource "},
            {"label": "Functinal Resource", "header": "Functinal Resource "},
        ],
    }
    people = get_people_columns(tab)
    assert [p["header"] for p in people] == ["Technical Resource ", "Functinal Resource "]


def test_no_people_columns_is_empty_not_an_error():
    assert get_people_columns({}) == []
    assert get_effort_columns({}) == []


def test_header_whitespace_and_typo_are_never_normalised_away():
    """The header is a lookup key. 'Functinal Resource ' is real data, not a mistake."""
    people = get_people_columns({
        "people_columns": [{"label": "Functional", "header": "Functinal Resource "}]
    })
    assert people[0]["header"] == "Functinal Resource "


def test_malformed_entries_are_dropped_not_crashed():
    tab = {"people_columns": [{"label": "No header"}, "not-a-dict", {"header": "Owner"}]}
    people = get_people_columns(tab)
    assert [p["header"] for p in people] == ["Owner"]


# --- detection normalisation ------------------------------------------------

def test_normalise_rederives_assignee_column_from_first_role():
    result = _normalise_people_and_effort({
        "people_columns": [
            {"label": "Technical Resource", "header": "Technical Resource "},
            {"label": "Functinal Resource", "header": "Functinal Resource "},
        ]
    })
    assert result["assignee_column"] == "Technical Resource "
    assert [p["key"] for p in result["people_columns"]] == [
        "technical_resource", "functinal_resource"
    ]


def test_normalise_seeds_people_from_legacy_key_when_model_omits_it():
    result = _normalise_people_and_effort({"assignee_column": "Owner"})
    assert len(result["people_columns"]) == 1
    assert result["people_columns"][0]["header"] == "Owner"


def test_normalise_deduplicates_repeated_headers():
    result = _normalise_people_and_effort({
        "people_columns": [
            {"label": "Owner", "header": "Owner"},
            {"label": "Owner again", "header": "Owner"},
        ]
    })
    assert len(result["people_columns"]) == 1


def test_normalise_sets_assignee_null_when_sheet_names_nobody():
    result = _normalise_people_and_effort({"people_columns": []})
    assert result["assignee_column"] is None
    assert result["people_columns"] == []


def test_structural_fallback_finds_every_people_column():
    """The fallback runs when the LLM fails; it must not regress to one role either."""
    rows = [["ID", "Description", "Technical Resource ", "Functinal Resource ", "Status"]]
    result = _normalise_people_and_effort(_structural_fallback(rows))
    headers = [p["header"] for p in result["people_columns"]]
    assert "Technical Resource " in headers
    assert "Functinal Resource " in headers


# --- person identity --------------------------------------------------------

def test_normalise_person_is_case_and_whitespace_insensitive():
    assert normalise_person("  Rohail   Shaikh ") == normalise_person("rohail shaikh")


def test_normalise_person_does_not_merge_distinct_names():
    """Merging two colleagues into one workload row is silent corruption."""
    assert normalise_person("R. Shaikh") != normalise_person("Rohail Shaikh")
    assert normalise_person("Sara") != normalise_person("Sara Khan")


def test_display_person_preserves_the_sheets_own_casing():
    assert display_person("  Rohail  Shaikh ") == "Rohail Shaikh"


def test_multi_name_cell_is_one_value_not_two_invented_people():
    """'Shaikh, Rohail' must not become two people who do not exist."""
    rows = {"Owner": "Shaikh, Rohail"}
    assignments = collect_assignments(rows, [{"key": "owner", "label": "Owner", "header": "Owner"}])
    assert len(assignments) == 1
    assert assignments[0]["person"] == "Shaikh, Rohail"


def test_collect_assignments_splits_by_role():
    row = {"Technical Resource ": "Rohail", "Functinal Resource ": "Sara"}
    cols = [
        {"key": "technical_resource", "label": "Technical Resource", "header": "Technical Resource "},
        {"key": "functinal_resource", "label": "Functinal Resource", "header": "Functinal Resource "},
    ]
    out = collect_assignments(row, cols)
    assert {(a["role_key"], a["person"]) for a in out} == {
        ("technical_resource", "Rohail"), ("functinal_resource", "Sara")
    }


def test_same_person_in_two_roles_yields_two_assignments():
    row = {"Tech": "Rohail", "Func": "Rohail"}
    cols = [
        {"key": "tech", "label": "Tech", "header": "Tech"},
        {"key": "func", "label": "Func", "header": "Func"},
    ]
    out = collect_assignments(row, cols)
    assert len(out) == 2
    assert {a["person_key"] for a in out} == {"rohail"}


def test_blank_person_cells_are_skipped():
    row = {"Tech": "   ", "Func": None}
    cols = [
        {"key": "tech", "label": "Tech", "header": "Tech"},
        {"key": "func", "label": "Func", "header": "Func"},
    ]
    assert collect_assignments(row, cols) == []


# --- effort -----------------------------------------------------------------

def test_effort_prefers_an_explicit_column():
    value, source = resolve_effort(
        {"Est Days": "5"}, [{"key": "est_days", "label": "Est Days", "header": "Est Days"}], {}
    )
    assert (value, source) == (5.0, "column")


def test_effort_falls_back_to_a_date_span():
    value, source = resolve_effort(
        {"Start": "2026-01-01", "Go-Live": "2026-01-11"},
        [],
        {"start": "Start", "go_live": "Go-Live"},
    )
    assert (value, source) == (10.0, "dates")


def test_effort_is_absent_rather_than_invented():
    """No effort column and no usable dates must yield None, never a placeholder number."""
    assert resolve_effort({"Description": "something"}, [], {}) == (None, None)


def test_effort_ignores_a_reversed_date_span():
    value, source = resolve_effort(
        {"Start": "2026-02-01", "Go-Live": "2026-01-01"}, [], {"start": "Start", "go_live": "Go-Live"}
    )
    assert (value, source) == (None, None)


@pytest.mark.parametrize("raw,expected", [
    ("5", 5.0), ("5 days", 5.0), ("2.5", 2.5), ("1,200", 1200.0), (7, 7.0),
    ("", None), ("   ", None), ("n/a", None), (None, None), ("TBD", None),
])
def test_parse_effort_tolerates_hand_typed_cells(raw, expected):
    assert parse_effort(raw) == expected


def test_parse_effort_does_not_coerce_junk_to_zero():
    """A zero-day workload is a claim ('nothing on'); unparseable input must not make it."""
    assert parse_effort("unknown") is None


@pytest.mark.parametrize("raw", ["2026-01-15", "15/01/2026", "15-Jan-2026", "15.01.2026"])
def test_parse_date_accepts_common_sheet_formats(raw):
    assert parse_date(raw).isoformat() == "2026-01-15"


def test_parse_date_returns_none_rather_than_guessing():
    assert parse_date("sometime next quarter") is None
    assert parse_date("") is None


def test_slugify_role_is_url_safe():
    assert slugify_role("Technical Resource ") == "technical_resource"
    assert slugify_role("QA / Test Lead") == "qa_test_lead"
