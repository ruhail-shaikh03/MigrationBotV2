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

from app.core.aliases import PersonResolver
from app.core.timeline import (
    MAX_GROUPS,
    OTHER_GROUP_LABEL,
    UNGROUPED_LABEL,
    build_timeline,
    classify_row,
)

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


# ---------------------------------------------------------------------------
# build_timeline
# ---------------------------------------------------------------------------

TODAY = date(2026, 3, 1)

HEADERS = ["WRICEF No.", "Description", "Module", "Dev Status", "Start Date",
           "Expected Completetion Date", "Developer Name"]

SCHEMA = {
    "primary_id_column": "WRICEF No.",
    "description_column": "Description",
    "module_column": "Module",
    "status_column": "Dev Status",
    "date_columns": {"start": "Start Date", "due": "Expected Completetion Date"},
    "people_columns": [{"key": "dev", "label": "Developer", "header": "Developer Name"}],
}


def _tracker_row(rid, module="SD", start="", due="", status="In Progress",
                 dev="Sara Iqbal", desc="Migrate BOM report", row_number=None):
    row = {
        "WRICEF No.": rid, "Description": desc, "Module": module,
        "Dev Status": status, "Start Date": start,
        "Expected Completetion Date": due, "Developer Name": dev,
    }
    if row_number is not None:
        row["__row_number__"] = row_number
    return row


def _build(rows, **kwargs):
    kwargs.setdefault("today", TODAY)
    return build_timeline(HEADERS, rows, SCHEMA, **kwargs)


# --- the counts are the contract ---------------------------------------------

def test_every_row_lands_in_exactly_one_bucket():
    rows = [
        _tracker_row("W-1", start="01/02/2026", due="09/02/2026"),
        _tracker_row("W-2", due="09/02/2026"),
        _tracker_row("W-3"),
        _tracker_row("W-4", due="17/0/2026"),
    ]
    counts = _build(rows)["counts"]
    assert counts == {"total": 4, "charted": 1, "milestone_only": 1, "undated": 1, "unparsed": 1}
    assert counts["charted"] + counts["milestone_only"] + counts["undated"] + counts["unparsed"] == counts["total"]


def test_undrawable_rows_are_counted_but_produce_no_items():
    """The drawable sibling is load-bearing: without it nothing on the tab parses, the
    refusal path empties `groups`, and the assertion passes without ever reaching the code
    that skips an undrawable row."""
    rows = [
        _tracker_row("W-1", start="01/02/2026", due="09/02/2026"),
        _tracker_row("W-3"),
        _tracker_row("W-4", due="17/0/2026"),
    ]
    result = _build(rows)
    assert result["counts"]["total"] == 3
    assert sum(len(g["items"]) for g in result["groups"]) == 1


# --- grouping -----------------------------------------------------------------

def test_rows_group_by_the_schema_module_column_by_default():
    rows = [
        _tracker_row("W-1", module="SD", start="01/02/2026", due="09/02/2026"),
        _tracker_row("W-2", module="MM", start="03/02/2026", due="11/02/2026"),
    ]
    result = _build(rows)
    assert result["group_by"] == "Module"
    assert {g["label"] for g in result["groups"]} == {"SD", "MM"}


def test_a_tab_with_no_module_column_yields_one_implicit_group():
    schema = {k: v for k, v in SCHEMA.items() if k != "module_column"}
    result = build_timeline(HEADERS, [_tracker_row("W-1", start="01/02/2026", due="09/02/2026")],
                            schema, today=TODAY)
    assert result["group_by"] is None
    assert [g["label"] for g in result["groups"]] == [UNGROUPED_LABEL]


def test_an_explicit_group_by_overrides_the_default():
    rows = [_tracker_row("W-1", status="On Hold", start="01/02/2026", due="09/02/2026")]
    result = _build(rows, group_by="Dev Status")
    assert result["group_by"] == "Dev Status"
    assert [g["label"] for g in result["groups"]] == ["On Hold"]


def test_grouping_by_a_people_column_splits_a_shared_cell():
    """The dashboard already reports two people here; the timeline must not report one."""
    rows = [_tracker_row("W-1", dev="Ahmed Qamar/Asif", start="01/02/2026", due="09/02/2026")]
    result = _build(rows, group_by="Developer Name")
    assert {g["label"] for g in result["groups"]} == {"Ahmed Qamar", "Asif"}


def test_grouping_by_a_plain_column_never_splits_on_an_ampersand():
    """"Sales & Distribution" is one module, not two — the distinction summarize makes."""
    rows = [_tracker_row("W-1", module="Sales & Distribution", start="01/02/2026", due="09/02/2026")]
    result = _build(rows)
    assert [g["label"] for g in result["groups"]] == ["Sales & Distribution"]


def test_grouping_by_a_people_column_applies_the_alias_map():
    resolver = PersonResolver({"madiha": ["Madiha Shah Bukhari"]})
    rows = [
        _tracker_row("W-1", dev="Madiha", start="01/02/2026", due="09/02/2026"),
        _tracker_row("W-2", dev="Madiha Shah Bukhari", start="02/02/2026", due="10/02/2026"),
    ]
    result = _build(rows, group_by="Developer Name", resolver=resolver)
    assert [g["label"] for g in result["groups"]] == ["Madiha Shah Bukhari"]
    assert result["groups"][0]["count"] == 2


def test_groups_past_the_cap_fold_into_one_other_bucket():
    """The fold is the only lossy-*looking* step in the function, so the bucket has to
    account for everything it swallowed — its count, its rows and its span, not just its
    name. Dates vary per row on purpose: with one shared span, `Other`'s would be
    indistinguishable from every other group's and a wrong rollup would still look right.
    """
    rows = [
        _tracker_row(f"W-{i}", module=f"M{i:02d}",
                     start=f"{i + 1:02d}/02/2026", due=f"{i + 1:02d}/03/2026")
        for i in range(MAX_GROUPS + 5)
    ]
    result = _build(rows)
    labels = [g["label"] for g in result["groups"]]
    assert len(labels) == MAX_GROUPS + 1
    assert labels[-1] == OTHER_GROUP_LABEL

    # Groups sort earliest-start first, so the five folded away are the five latest —
    # M12..M16, starting 13-17 Feb and ending 13-17 Mar.
    other = result["groups"][-1]
    assert other["count"] == 5
    assert other["collapsed_groups"] == 5
    assert len(other["items"]) == 5
    assert other["start"] == "2026-02-13"
    assert other["end"] == "2026-03-17"


# --- rollup -------------------------------------------------------------------

def test_a_group_header_spans_the_min_and_max_of_its_children():
    """Deliberately out of order: the latest end is listed first and the earliest start
    last, so neither `starts[0]` nor `ends[-1]` can pass for a real min/max."""
    rows = [
        _tracker_row("W-2", start="05/02/2026", due="23/02/2026"),
        _tracker_row("W-1", start="01/02/2026", due="09/02/2026"),
    ]
    group = _build(rows)["groups"][0]
    assert group["start"] == "2026-02-01"
    assert group["end"] == "2026-02-23"


def test_a_group_with_nothing_dated_has_no_span_but_keeps_its_count():
    """Needs a dated sibling group: a tab where *nothing* is dated returns a reason and no
    groups at all, which is the separate case below."""
    rows = [
        _tracker_row("W-1", module="SD", start="01/02/2026", due="09/02/2026"),
        _tracker_row("W-2", module="MM"),
        _tracker_row("W-3", module="MM"),
    ]
    groups = {g["label"]: g for g in _build(rows)["groups"]}
    assert groups["MM"]["start"] is None and groups["MM"]["end"] is None
    assert groups["MM"]["count"] == 2


def test_a_milestone_contributes_to_its_group_span():
    rows = [
        _tracker_row("W-1", start="01/02/2026", due="09/02/2026"),
        _tracker_row("W-2", due="28/02/2026"),
    ]
    group = _build(rows)["groups"][0]
    assert group["end"] == "2026-02-28"


# --- items --------------------------------------------------------------------

def test_an_item_carries_the_row_number_for_the_duplicate_id_pin():
    rows = [_tracker_row("W-1", start="01/02/2026", due="09/02/2026", row_number=47)]
    item = _build(rows)["groups"][0]["items"][0]
    assert item["row_number"] == 47


def test_an_item_is_labelled_with_its_description():
    rows = [_tracker_row("W-1", desc="Migrate BOM report", start="01/02/2026", due="09/02/2026")]
    assert _build(rows)["groups"][0]["items"][0]["label"] == "Migrate BOM report"


def test_a_past_deadline_on_unfinished_work_is_overdue():
    rows = [_tracker_row("W-1", start="01/01/2026", due="09/01/2026", status="In Progress")]
    assert _build(rows)["groups"][0]["items"][0]["overdue"] is True


def test_a_past_deadline_on_finished_work_is_not_overdue():
    """"Completed" is the reference tracker's value, and it must not read as overdue —
    the §16.6 divergence that had chat reporting 171 finished rows as late."""
    rows = [_tracker_row("W-1", start="01/01/2026", due="09/01/2026", status="Completed")]
    assert _build(rows)["groups"][0]["items"][0]["overdue"] is False


def test_a_start_only_milestone_is_never_overdue_however_old():
    """A start date promises nothing, so a stale one is not a missed deadline. Reporting it
    as one invents an obligation the sheet never recorded."""
    rows = [_tracker_row("W-1", start="01/01/2026", due="")]
    item = _build(rows)["groups"][0]["items"][0]
    assert item["kind"] == "milestone"
    assert item["milestone_of"] == "start"
    assert item["overdue"] is False


def test_a_due_only_milestone_past_its_date_is_overdue():
    """The mirror. A deadline with no start is still a deadline, and a milestone that never
    flags is the §16.6 under-reporting again, one bucket over."""
    rows = [_tracker_row("W-1", start="", due="09/01/2026")]
    item = _build(rows)["groups"][0]["items"][0]
    assert item["kind"] == "milestone"
    assert item["milestone_of"] == "due"
    assert item["overdue"] is True


def test_a_reversed_pair_is_drawn_forwards_and_flagged():
    rows = [_tracker_row("W-1", start="09/02/2026", due="01/02/2026")]
    item = _build(rows)["groups"][0]["items"][0]
    assert item["start"] == "2026-02-01"
    assert item["end"] == "2026-02-09"
    assert item["reversed"] is True


def test_an_item_carries_the_raw_cell_text_for_the_editable_fields():
    """The modal writes back what the user types; handing it an ISO date would rewrite a
    sheet that spells dates '10.05.2026' into a different format on every edit."""
    rows = [_tracker_row("W-1", start="10.05.2026", due="12.05.2026")]
    item = _build(rows)["groups"][0]["items"][0]
    assert item["values"]["Start Date"] == "10.05.2026"
    assert item["values"]["Developer Name"] == "Sara Iqbal"


# --- headers, range and refusal ------------------------------------------------

def test_the_resolved_column_names_are_reported():
    result = _build([_tracker_row("W-1", start="01/02/2026", due="09/02/2026")])
    assert result["start_header"] == "Start Date"
    assert result["due_header"] == "Expected Completetion Date"


def test_a_schema_header_with_a_trailing_space_binds_to_the_stripped_row_key():
    """The §7.2 asymmetry: schema_config stores headers verbatim, row dicts are stripped."""
    schema = {**SCHEMA, "date_columns": {"start": "Start Date ", "due": "Expected Completetion Date"}}
    result = build_timeline(HEADERS, [_tracker_row("W-1", start="01/02/2026", due="09/02/2026")],
                            schema, today=TODAY)
    assert result["start_header"] == "Start Date"
    assert result["counts"]["charted"] == 1


def test_the_axis_range_spans_every_drawn_date():
    rows = [
        _tracker_row("W-1", start="01/02/2026", due="09/02/2026"),
        _tracker_row("W-2", due="28/02/2026"),
    ]
    result = _build(rows)
    assert result["range_start"] == "2026-02-01"
    assert result["range_end"] == "2026-02-28"


def test_a_tab_with_no_date_columns_says_so_instead_of_returning_zeroes():
    schema = {"primary_id_column": "WRICEF No."}
    result = build_timeline(["WRICEF No."], [{"WRICEF No.": "W-1"}], schema, today=TODAY)
    assert result["reason"] is not None
    assert result["groups"] == []


def test_a_tab_where_no_row_is_readable_says_so():
    result = _build([_tracker_row("W-1"), _tracker_row("W-2")])
    assert result["reason"] is not None
    # Refusal is the one path that empties the chart, so it is the one place a row could
    # vanish without a count moving. The counts have to survive it intact — a caption
    # reading "0 of 0" over a refused tab is a different and much softer lie than "2 rows,
    # neither dated".
    assert result["counts"] == {"total": 2, "charted": 0, "milestone_only": 0,
                                "undated": 2, "unparsed": 0}


def test_a_tab_with_drawable_rows_has_no_reason():
    assert _build([_tracker_row("W-1", start="01/02/2026", due="09/02/2026")])["reason"] is None


def test_groupable_offers_low_cardinality_columns_and_omits_free_text():
    rows = [_tracker_row(f"W-{i}", desc=f"unique description {i}", start="01/02/2026",
                         due="09/02/2026") for i in range(MAX_GROUPS + 5)]
    groupable = _build(rows)["groupable"]
    assert "Module" in groupable
    assert "Description" not in groupable


def test_a_people_column_is_always_groupable_regardless_of_cardinality():
    rows = [_tracker_row(f"W-{i}", dev=f"Person {i}", start="01/02/2026", due="09/02/2026")
            for i in range(MAX_GROUPS + 5)]
    assert "Developer Name" in _build(rows)["groupable"]
