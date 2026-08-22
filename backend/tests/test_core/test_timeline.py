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

from datetime import date, timedelta

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


def test_a_shared_cell_folded_from_two_groups_is_listed_once():
    """A people grouping puts a shared cell in two buckets deliberately — the Workload
    panel counts it twice for the same reason. When *both* of those buckets fall past the
    cap, concatenating the tail lists the one row twice under `Other`: drawn twice, keyed
    twice in the client, and counted once by `counts.charted`.

    People columns are offered as groupings precisely because a large roster overflows the
    cap, so this is the expected shape of a people-grouped fold, not a corner of it."""
    rows = [
        _tracker_row(f"W-{i}", dev=f"P{i:02d}", start=f"{i + 1:02d}/02/2026",
                     due=f"{i + 1:02d}/03/2026", row_number=i + 3)
        for i in range(MAX_GROUPS + 3)
    ]
    # Two people, neither of whom appears anywhere else, so both of this row's buckets
    # hold one row and both sort into the tail.
    rows.append(_tracker_row("W-SHARED", dev="P90/P91", start="20/02/2026",
                             due="20/03/2026", row_number=99))
    result = _build(rows, group_by="Developer Name")

    other = [g for g in result["groups"] if g.get("collapsed_groups")][0]
    ids = [i["id"] for i in other["items"]]
    # Groups sort earliest-start first, so the five folded away are P12, P13, P14 and the
    # two the shared cell created. Spelled in full rather than as a count, because order
    # is part of the contract: the dedupe keeps the first sighting, not the last.
    assert ids == ["W-12", "W-13", "W-14", "W-SHARED"]
    assert other["collapsed_groups"] == 5
    # The count still sees both of the shared row's memberships. That per-group duplication
    # is Workload parity and is deliberate; only the row's second *listing* was the defect.
    assert other["count"] == 5


def test_the_folded_bucket_never_collides_with_a_real_group_called_other():
    """"Other" is a plausible module name. A second group under that label fuses with it in
    any client keying its list and its collapse state on the label — one toggle driving two
    rows, and a duplicate React key."""
    rows = [
        _tracker_row(f"W-{i}", module=f"M{i:02d}", start=f"{i + 1:02d}/02/2026",
                     due=f"{i + 1:02d}/03/2026")
        for i in range(MAX_GROUPS + 5)
    ]
    # Earliest start on the tab, so it sorts to the front of `head` and survives the fold
    # rather than being absorbed into it.
    rows.append(_tracker_row("W-OTHER", module=OTHER_GROUP_LABEL, start="01/01/2026",
                             due="09/01/2026"))
    labels = [g["label"] for g in _build(rows)["groups"]]
    assert len(labels) == len(set(labels))
    assert OTHER_GROUP_LABEL in labels
    # The real group keeps the plain label; the fold is the one that moves.
    real = [g for g in _build(rows)["groups"] if g["label"] == OTHER_GROUP_LABEL][0]
    assert real.get("collapsed_groups") is None


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


def test_a_header_named_in_two_roles_is_offered_for_editing_once():
    """`people_columns` is free-form, so nothing forbids a schema naming its status column
    as a people column too — a tracker whose "Dev Status" cell holds "In Progress (Sara)"
    is a plausible reason to. Listing the header twice would render the same field twice in
    the row dialog, and a second box writing to the same cell is a conflict the user has no
    way to resolve."""
    schema = {**SCHEMA, "people_columns": [
        {"key": "dev", "label": "Developer", "header": "Developer Name"},
        {"key": "status_owner", "label": "Status owner", "header": "Dev Status"},
    ]}
    result = build_timeline(HEADERS, [_tracker_row("W-1", start="01/02/2026", due="09/02/2026")],
                            schema, today=TODAY)
    editable = result["editable_headers"]
    assert editable.count("Dev Status") == 1
    # The other roles survive, in first-occurrence order: the duplicate does not shunt the
    # status column down to where the people columns start.
    assert editable == ["Start Date", "Expected Completetion Date", "Dev Status", "Developer Name"]


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


def test_the_date_columns_the_chart_is_drawn_from_are_never_offered_as_groupings():
    """Grouping a timeline by its own start column yields one single-day group per distinct
    date. It is never what anyone means, and low cardinality would otherwise offer it."""
    rows = [_tracker_row("W-1", start="01/02/2026", due="09/02/2026")]
    groupable = _build(rows)["groupable"]
    assert "Start Date" not in groupable
    assert "Expected Completetion Date" not in groupable


def test_groupable_describes_the_tab_not_the_filter():
    """Which columns can group a tab is a fact about the tab. Computed over a filtered set
    — one person, all of whose rows leave `Module` blank — `Module` drops out of the list
    while still being the active grouping, and the client's `<select value>` then matches
    no option and silently displays some other column as the one in force."""
    everything = [
        _tracker_row("W-1", module="SD", dev="Sara Iqbal", start="01/02/2026", due="09/02/2026"),
        _tracker_row("W-2", module="", dev="Asif", start="03/02/2026", due="11/02/2026"),
    ]
    filtered = [everything[1]]
    result = build_timeline(HEADERS, filtered, SCHEMA, today=TODAY, all_rows=everything)
    assert "Module" in result["groupable"]
    # And the counts still describe the filtered set, which is the half that must not move.
    assert result["counts"]["total"] == 1


# --- the two scans competing for one header -----------------------------------

def test_a_lone_planned_start_column_is_a_start_not_a_deadline():
    """`_DUE_WORDS` has "planned" and `_START_WORDS` has "start", so "Planned Start Date"
    matches both. Resolving due first and unguarded made every row a deadline milestone,
    and every unfinished row whose start had merely passed came back overdue — an
    obligation the sheet never recorded, which is §16.6's inversion in mirror image.

    The one case with no explicit `exclude` to separate them, and the schema maps nothing:
    exactly where the guessing has to be got right."""
    headers = ["ID", "Planned Start Date", "Status"]
    rows = [{"ID": "A", "Planned Start Date": "01/01/2026", "Status": "In Progress"}]
    result = build_timeline(headers, rows, {"primary_id_column": "ID"}, today=TODAY)

    assert result["start_header"] == "Planned Start Date"
    assert result["due_header"] is None
    item = result["groups"][0]["items"][0]
    assert item["milestone_of"] == "start"
    assert item["overdue"] is False


def test_a_scanned_deadline_that_reads_as_no_kind_of_start_still_wins():
    """The overrule is narrow. "Target Completion Date" carries a due word and no start
    word, so it stays the deadline and a past one on unfinished work is still overdue."""
    headers = ["ID", "Target Completion Date", "Status"]
    rows = [{"ID": "A", "Target Completion Date": "01/01/2026", "Status": "In Progress"}]
    result = build_timeline(headers, rows, {"primary_id_column": "ID"}, today=TODAY)

    assert result["due_header"] == "Target Completion Date"
    assert result["start_header"] is None
    assert result["groups"][0]["items"][0]["overdue"] is True


def test_an_explicit_schema_deadline_outranks_the_start_vocabulary():
    """A `date_columns` mapping is a human decision about that sheet. Only the *scan* is
    overruled — a schema that really does call its deadline column "Planned Start Date"
    keeps it, and this is what stops the fix reaching `summarize(overdue)`'s behaviour."""
    headers = ["ID", "Kickoff Date", "Planned Start Date", "Status"]
    rows = [{"ID": "A", "Kickoff Date": "01/01/2026",
             "Planned Start Date": "20/01/2026", "Status": "In Progress"}]
    schema = {"primary_id_column": "ID", "date_columns": {"due": "Planned Start Date"}}
    result = build_timeline(headers, rows, schema, today=TODAY)

    assert result["due_header"] == "Planned Start Date"
    assert result["start_header"] == "Kickoff Date"
    assert result["groups"][0]["items"][0]["kind"] == "bar"


# --- the axis is bounded by the data, not by its typos -------------------------

def _spread(n, first=date(2026, 1, 1), step=7):
    """`n` bar rows a week apart — enough points for the fence to have quartiles."""
    return [
        _tracker_row(
            f"W-{i}",
            start=(first + timedelta(days=i * step)).strftime("%d/%m/%Y"),
            due=(first + timedelta(days=i * step + 3)).strftime("%d/%m/%Y"),
        )
        for i in range(n)
    ]


def test_one_mistyped_year_does_not_stretch_the_axis_over_two_centuries():
    """Observed live: "02.06.2206" for 2026 on one row of the reference tracker ran the
    axis from 2021 to 2209 and squeezed the other sixty-one dated rows into its leftmost
    two percent, behind 189 overlapping year labels."""
    rows = _spread(12) + [_tracker_row("W-BAD", start="01/01/2026", due="02/06/2206")]
    result = _build(rows)

    assert result["range_clamped"] is True
    # The fence lands inside the typo, not on it. The bulk of the work ends in March 2026.
    assert result["range_end"] < "2027-01-01"
    assert result["range_start"] == "2026-01-01"


def test_the_row_carrying_the_typo_is_flagged_rather_than_dropped():
    """A chart that silently omits a row is the failure the coverage line exists to
    prevent — and this is the one row the reader most needs to find and fix."""
    rows = _spread(12) + [_tracker_row("W-BAD", start="01/01/2026", due="02/06/2206")]
    result = _build(rows)

    items = [i for g in result["groups"] for i in g["items"]]
    assert len(items) == 13
    flagged = [i for i in items if i["out_of_range"]]
    assert [i["id"] for i in flagged] == ["W-BAD"]
    # Its real date survives intact; only the axis was bounded.
    assert flagged[0]["end"] == "2206-06-02"


def test_a_genuinely_long_plan_is_not_clamped():
    """The fence must not fire on a tab that really does span years evenly. Clamping here
    would mark honest rows out of range and shorten an axis nobody complained about."""
    rows = _spread(24, first=date(2021, 1, 1), step=90)
    result = _build(rows)

    assert result["range_clamped"] is False
    assert result["range_start"] == "2021-01-01"
    assert not any(i["out_of_range"] for g in result["groups"] for i in g["items"])


def test_a_handful_of_dates_is_never_clamped():
    """Below `_MIN_POINTS` the quartiles are two or three points and the fence they draw is
    noise. A small tab is also one a reader takes in whole, so a stretched axis there is
    survivable in a way it is not across four hundred rows.

    Six dates, one of them wild: enough that the fence *would* fire without the guard —
    q1 and q3 both land in January and push the outlier outside — and few enough that it
    must not."""
    rows = [
        _tracker_row("W-1", start="01/01/2026", due="09/01/2026"),
        _tracker_row("W-2", start="03/01/2026", due="11/01/2026"),
        _tracker_row("W-3", start="01/01/2026", due="02/06/2206"),
    ]
    result = _build(rows)

    assert result["range_clamped"] is False
    assert result["range_end"] == "2206-06-02"


def test_a_cluster_on_one_date_does_not_fence_its_own_stragglers_out():
    """Zero interquartile range times three is zero: the fence collapses onto the median
    and every row either side of it becomes an outlier. `_MIN_FENCE_DAYS` is the floor.

    The shape that exposes it: a dense cluster all on one date, a few legitimate rows three
    weeks later, and one real typo. Without the floor the fence is the cluster's single
    date, so the three-week rows are flagged and the axis stops short of them — the guard
    firing on exactly the honest rows it exists to protect."""
    rows = (
        [_tracker_row(f"W-{i}", start="02/02/2026", due="02/02/2026") for i in range(20)]
        + [_tracker_row(f"S-{i}", start="22/02/2026", due="22/02/2026") for i in range(2)]
        + [_tracker_row("W-BAD", start="02/02/2026", due="02/06/2206")]
    )
    result = _build(rows)

    assert result["range_clamped"] is True
    # The stragglers are inside the fence; only the typo is out.
    assert result["range_end"] == "2026-02-22"
    flagged = {i["id"] for g in result["groups"] for i in g["items"] if i["out_of_range"]}
    assert flagged == {"W-BAD"}


def test_the_axis_does_not_move_when_the_grouping_changes():
    """Grouping by a people column puts a shared row in two buckets on purpose. Read the
    quartiles off `groups` and those rows count twice, so the fence — and with it the axis
    and every out-of-range flag — depends on which column the reader grouped by. Changing
    Group by must never move the dates.

    Weighted deliberately: ten single-owner rows in February against six shared rows whose
    deadlines are years out. Counted once the far dates are a minority and the fence
    excludes them; counted twice they are the majority and it does not."""
    schema = {**SCHEMA, "people_columns": [
        {"key": "dev", "label": "Developer", "header": "Developer Name"}]}
    rows = (
        [_tracker_row(f"W-{i}", start="01/02/2026", due="09/02/2026", dev="Sara Iqbal")
         for i in range(10)]
        + [_tracker_row(f"F-{i}", start="01/02/2026", due="02/06/2206", dev="Sara/Omar")
           for i in range(6)]
    )

    grouped = build_timeline(HEADERS, rows, schema, group_by="Developer Name", today=TODAY)
    ungrouped = build_timeline(HEADERS, rows, schema, today=TODAY)

    assert grouped["range_end"] == ungrouped["range_end"] == "2026-02-09"
    assert grouped["range_clamped"] is ungrouped["range_clamped"] is True


# --- rows with nothing to draw are reachable, not merely counted ---------------

def test_undated_and_unparsed_rows_are_listed_so_they_can_be_scheduled():
    """347 of 412 rows on the reference tracker are undated. Clicking a bar was the only
    way into the row dialog, so those rows could be counted from this panel and edited from
    nowhere in it — the panel could not do the one thing its name promises."""
    rows = [
        _tracker_row("W-1", start="01/02/2026", due="09/02/2026"),
        _tracker_row("W-2"),
        _tracker_row("W-3", due="17/0/2026"),
    ]
    result = _build(rows)

    assert [(u["id"], u["kind"]) for u in result["unplaced"]] == [
        ("W-2", "undated"), ("W-3", "unparsed"),
    ]
    # And they stay out of the chart: 347 empty tracks would bury the 62 real ones.
    assert sum(len(g["items"]) for g in result["groups"]) == 1


def test_an_unplaced_row_carries_everything_the_dialog_needs_to_write():
    """It opens the same dialog a bar does, and a write addresses a row by ID plus row
    number. A listing that could not be edited would just be a longer count."""
    result = _build([_tracker_row("W-2", row_number=42, desc="Payroll extract")])

    row = result["unplaced"][0]
    assert row["id"] == "W-2"
    assert row["label"] == "Payroll extract"
    assert row["row_number"] == 42
    assert set(result["editable_headers"]) <= set(row["values"])


def test_a_tab_where_nothing_parses_still_lists_its_rows():
    """`reason` empties `groups`, and emptying the listing with it would leave a reader
    told there is nothing to draw and given no way to change that."""
    result = _build([_tracker_row("W-1"), _tracker_row("W-2")])

    assert result["reason"]
    assert result["groups"] == []
    assert len(result["unplaced"]) == 2


# --- planned against actual, on a tab that records four dates ------------------

HEADERS4 = ["ID", "Description", "Module", "Status", "Planned Start Date",
            "Planned Finish Date", "Actual Start Date", "Actual Finish Date", "Owner"]

SCHEMA4 = {
    "primary_id_column": "ID",
    "description_column": "Description",
    "module_column": "Module",
    "status_column": "Status",
    # What detection writes: the planned pair. The actual pair is found by the qualifier
    # scan, which is the case a sheet detected before baselines existed will be in.
    "date_columns": {"start": "Planned Start Date", "due": "Planned Finish Date"},
    "people_columns": [{"key": "own", "label": "Owner", "header": "Owner"}],
}


def _row4(rid="R-1", ps="", pf="", as_="", af="", status="In Progress", module="SD"):
    return {
        "ID": rid, "Description": "Migrate BOM report", "Module": module, "Status": status,
        "Planned Start Date": ps, "Planned Finish Date": pf,
        "Actual Start Date": as_, "Actual Finish Date": af, "Owner": "Sara Iqbal",
    }


def _build4(rows, **kwargs):
    kwargs.setdefault("today", TODAY)
    return build_timeline(HEADERS4, rows, SCHEMA4, **kwargs)


def _only_item(result):
    items = [i for g in result["groups"] for i in g["items"]]
    assert len(items) == 1
    return items[0]


def test_a_two_date_tab_reports_no_actual_pair_and_no_actual_segment():
    """The no-regression property, asserted rather than assumed: every tracker recording
    one pair of dates must come through this feature unchanged."""
    result = _build([_tracker_row("W-1", start="01/02/2026", due="09/02/2026")])

    assert result["actual_start_header"] is None
    assert result["actual_due_header"] is None
    item = _only_item(result)
    assert item["actual"] is None
    assert item["planned"] == {
        "kind": "bar", "start": "2026-02-01", "end": "2026-02-09",
        "milestone_of": None, "reversed": False,
    }


def test_a_four_date_tab_resolves_both_pairs_and_draws_both():
    result = _build4([_row4(ps="01/02/2026", pf="09/02/2026",
                            as_="03/02/2026", af="15/02/2026")])

    assert result["start_header"] == "Planned Start Date"
    assert result["due_header"] == "Planned Finish Date"
    assert result["actual_start_header"] == "Actual Start Date"
    assert result["actual_due_header"] == "Actual Finish Date"

    item = _only_item(result)
    assert item["planned"]["start"] == "2026-02-01"
    assert item["planned"]["end"] == "2026-02-09"
    assert item["actual"]["start"] == "2026-02-03"
    assert item["actual"]["end"] == "2026-02-15"


def test_the_envelope_spans_both_segments():
    """`start`/`end` are what the axis, the group rollups and the row ordering read. A row
    whose actual work ran past its plan reaches further than its plan says."""
    item = _only_item(_build4([_row4(ps="01/02/2026", pf="09/02/2026",
                                     as_="03/02/2026", af="15/02/2026")]))
    assert (item["start"], item["end"]) == ("2026-02-01", "2026-02-15")


def test_slip_is_the_gap_between_the_two_finishes():
    item = _only_item(_build4([_row4(ps="01/02/2026", pf="09/02/2026",
                                     as_="03/02/2026", af="15/02/2026")]))
    assert item["slip_days"] == 6


def test_finishing_early_slips_negative():
    item = _only_item(_build4([_row4(ps="01/02/2026", pf="09/02/2026",
                                     as_="01/02/2026", af="05/02/2026")]))
    assert item["slip_days"] == -4


def test_slip_is_none_when_only_one_finish_is_recorded():
    """Half a comparison is not a number. A row still running has not slipped by any
    amount — it is simply unfinished, which `overdue` is the word for."""
    item = _only_item(_build4([_row4(ps="01/02/2026", pf="09/02/2026", as_="03/02/2026")]))
    assert item["slip_days"] is None


def test_a_passed_deadline_with_no_actual_finish_is_overdue():
    item = _only_item(_build4([_row4(ps="01/01/2026", pf="09/01/2026", as_="03/01/2026")]))
    assert item["overdue"] is True


def test_a_recorded_actual_finish_discharges_the_deadline_however_late():
    """Delivered work is never overdue. It slipped, and `slip_days` says by how much —
    reporting it as overdue would be §16.6's inversion in a third costume."""
    item = _only_item(_build4([_row4(ps="01/01/2026", pf="09/01/2026",
                                     as_="03/01/2026", af="20/01/2026")]))
    assert item["overdue"] is False
    assert item["slip_days"] == 11


def test_an_actual_start_alone_does_not_discharge_the_deadline():
    """A milestone carries its one date at both ends, so `actual["end"]` is set even when
    only a start was recorded. Reading it as a finish would cancel the overdue flag on
    every row anybody had merely begun."""
    item = _only_item(_build4([_row4(ps="01/01/2026", pf="09/01/2026", as_="03/01/2026")]))
    assert item["actual"]["milestone_of"] == "start"
    assert item["actual"]["end"] == "2026-01-03"
    assert item["overdue"] is True


def test_a_row_recording_only_actuals_is_drawn_rather_than_counted_undated():
    """Work that happened without ever being planned is still work that happened."""
    result = _build4([_row4(as_="03/02/2026", af="15/02/2026")])

    assert result["counts"]["charted"] == 1
    assert result["counts"]["undated"] == 0
    item = _only_item(result)
    assert item["planned"] is None
    assert item["actual"]["kind"] == "bar"


def test_a_row_with_neither_pair_is_still_undated():
    result = _build4([_row4()])
    assert result["counts"] == {"total": 1, "charted": 0, "milestone_only": 0,
                                "undated": 1, "unparsed": 0}


def test_an_unreadable_actual_date_makes_the_whole_row_unparsed():
    """`unparsed` beats a readable partner across segments too, for the reason it beats one
    within a segment: drawing the good half hides the cell that is wrong."""
    result = _build4([_row4(ps="01/02/2026", pf="09/02/2026", af="17/0/2026")])
    assert result["counts"]["unparsed"] == 1
    assert result["counts"]["charted"] == 0


def test_all_four_date_columns_are_editable_from_the_row_dialog():
    """Rescheduling means writing to whichever of the four is wrong, so all four have to be
    reachable. Two of them being uneditable is the complaint that started this."""
    result = _build4([_row4(ps="01/02/2026", pf="09/02/2026")])
    assert set(result["editable_headers"]) >= {
        "Planned Start Date", "Planned Finish Date",
        "Actual Start Date", "Actual Finish Date",
    }
