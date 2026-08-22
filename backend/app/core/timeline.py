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

from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.core.overdue import is_finished_status
from app.core.people import parse_date, split_cell
from app.core.schema import (
    ROW_NUMBER_KEY,
    bind_columns,
    get_people_columns,
    get_start_column,
    header_reads_as_a_start,
    resolve_actual_columns,
    resolve_due_column,
    resolve_header,
)

# Matches ToolResultCard's threshold for switching a bar chart to a table (§14.2): past a
# dozen categories the reader is scanning a list, not comparing magnitudes.
MAX_GROUPS = 12

OTHER_GROUP_LABEL = "Other"
UNGROUPED_LABEL = "All rows"
BLANK_GROUP_LABEL = "(blank)"
UNASSIGNED_GROUP_LABEL = "Unassigned"

# A single mistyped year is enough to make the panel useless. On the reference tracker one
# row carries "02.06.2206" where 2026 was meant; the axis therefore ran 2021 to 2209 and
# the other sixty-one dated rows were squeezed into its leftmost two percent, behind an
# unreadable smear of 189 year labels. Nothing about that chart was wrong — it was a
# faithful drawing of a range nobody wanted to see.
#
# So the axis is bounded by the bulk of the dates rather than by their extremes, with a
# Tukey fence: quartiles, three interquartile ranges either side. Rows outside it keep
# their real dates and are still drawn, clamped to the edge and flagged — never dropped,
# because a chart that quietly omits a row is the exact failure the coverage line exists
# to prevent, and the row with the typo is the one the reader most needs to find.
_FENCE_MULTIPLIER = 3

# A floor under the fence. A tab whose dates all land in one week has an interquartile
# range of zero, and three times zero would fence the axis to a point and declare every
# row on either side of the median an outlier.
_MIN_FENCE_DAYS = 30

# And a reason to bother. Clamping buys legibility only when the outliers are what make
# the span long; below this ratio it would mark rows out of range for no visible gain.
_CLAMP_RATIO = 2

# Too few dates to fence at all. See the note in `_drawn_range`.
_MIN_POINTS = 8


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


def _iso(value: Optional[date]) -> Optional[str]:
    return value.isoformat() if value else None


def _segment(
    row: Dict[str, str],
    start_header: Optional[str],
    due_header: Optional[str],
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """One pair of date columns, resolved into the shape a chart can draw.

    Returns `(kind, segment)`. The segment is None for `undated` and `unparsed` — there is
    nothing to draw at — and the kind is returned alongside so the caller can tell those
    two apart, which is the distinction the coverage line is built on.

    This is `classify_row` plus the normalisation the caller used to do inline: an end
    before its start is swapped for drawing and flagged rather than silently corrected, and
    a milestone records which column it came from so a reader can tell "starts on the 9th"
    from "due on the 9th".
    """
    kind, start, end = classify_row(row, start_header, due_header)
    if kind in ("undated", "unparsed"):
        return kind, None

    reversed_dates = bool(start and end and end < start)
    if reversed_dates:
        start, end = end, start

    milestone_of = None
    if kind == "milestone":
        milestone_of = "start" if start_header and str(row.get(start_header, "")).strip() else "due"

    return kind, {
        "kind": kind,
        "start": _iso(start),
        "end": _iso(end),
        "milestone_of": milestone_of,
        "reversed": reversed_dates,
    }


def _finish(segment: Optional[Dict[str, Any]]) -> Optional[str]:
    """The date a segment says the work ends, or None if it never says.

    A milestone carries the same value at both ends, so `segment["end"]` alone cannot tell
    a recorded finish from a recorded start — and the difference decides both whether a row
    is overdue and whether it slipped. A start-only segment finishes nothing.
    """
    if not segment:
        return None
    if segment["kind"] == "bar" or segment["milestone_of"] == "due":
        return segment["end"]
    return None


def _drawn_range(dates: List[date]) -> Tuple[Optional[date], Optional[date], bool]:
    """The span the axis should cover, which is not always the span the dates cover.

    Returns `(start, end, clamped)`. `clamped` is True only when outliers were actually
    excluded, so the caller can say so rather than leaving the reader to wonder why a date
    they can see in the sheet sits on the edge of the chart.

    Order statistics, not interpolated quartiles: with a handful of dates the difference is
    a day or two on a fence measured in months, and `pts[n // 4]` cannot be got wrong.
    """
    if not dates:
        return None, None, False

    pts = sorted(dates)
    lo_all, hi_all = pts[0], pts[-1]

    # Below this the quartiles are two or three points and the fence they draw is noise. It
    # is also the size of tab a reader takes in whole, where a stretched axis is survivable
    # in a way it is not across four hundred rows. Eight rather than four because the
    # indices below put q3 on the maximum for any n up to four — a fence that can never
    # exclude a high outlier, and a guard that reads as if it does something it does not.
    if len(pts) < _MIN_POINTS:
        return lo_all, hi_all, False

    q1, q3 = pts[len(pts) // 4], pts[(len(pts) * 3) // 4]
    pad = max((q3 - q1).days * _FENCE_MULTIPLIER, _MIN_FENCE_DAYS)
    lo_fence, hi_fence = q1 - timedelta(days=pad), q3 + timedelta(days=pad)

    inliers = [d for d in pts if lo_fence <= d <= hi_fence]
    if not inliers:
        return lo_all, hi_all, False

    lo, hi = inliers[0], inliers[-1]
    # `max(1, ...)` keeps a tab whose inliers share one date — span zero — from dividing the
    # comparison away; there the outliers are the entire span and clamping plainly applies.
    if (hi_all - lo_all).days < max(1, (hi - lo).days) * _CLAMP_RATIO:
        return lo_all, hi_all, False

    return lo, hi, True


def _row_identity(
    row: Dict[str, str],
    id_header: Optional[str],
    desc_header: Optional[str],
    people: List[Dict[str, str]],
    editable: List[str],
) -> Dict[str, Any]:
    """The fields a row carries whether or not it can be drawn.

    Shared by the bars and by the rows that have no date to be drawn at. The row dialog
    addresses a row through exactly these — an ID to write against, a row number so a
    duplicated ID still lands on the row that was clicked, and the editable cells — which
    is what lets an undated row open the very same dialog a bar does. Without that the
    panel can count 347 unscheduled rows and offer no way to schedule any of them.
    """
    ident = str(row.get(id_header, "")) if id_header else ""
    return {
        "id": ident,
        "label": (str(row.get(desc_header, "")).strip() if desc_header else "") or ident,
        "row_number": row.get(ROW_NUMBER_KEY),
        "people": [
            {"key": p["key"], "label": p["label"], "header": p["header"],
             "value": str(row.get(p["header"], ""))}
            for p in people
        ],
        # Raw cell text, not the parsed ISO value. The dialog writes back exactly what it
        # was given unless the user edits it, so a sheet spelling dates "10.05.2026" is not
        # silently rewritten into ISO on every save.
        "values": {h: str(row.get(h, "")) for h in editable},
    }


def _item_identity(item: Dict[str, Any]) -> Tuple[str, Any]:
    """What tells two timeline items apart, spelled to match the client's `itemKey`.

    `DataDisplay.tsx:itemKey` is `${id}-${row_number ?? label}`: the ID alone is not enough
    because 27 of 412 rows on the reference tracker share one, and the label stands in on
    the rare payload carrying no row number. This mirrors it exactly rather than picking
    its own fields, so the rows this collapses are precisely the rows the client would
    collide on — a dedupe that disagreed with the key would leave the duplicate React key
    it was added to prevent.
    """
    row_number = item.get("row_number")
    return item.get("id", ""), row_number if row_number is not None else item.get("label", "")


def _group_labels(
    row: Dict[str, str],
    group_header: Optional[str],
    is_person_column: bool,
    resolver: Optional[Any],
) -> List[str]:
    """Every group this row belongs to.

    A row belongs to more than one group only when the grouping column names more than
    one person — a shared cell like "Ahmed Qamar/Asif" is two people's work, and the
    Workload panel already counts it as two. Every other column is taken verbatim: an
    ampersand in "Sales & Distribution" is one module, and `summarize` makes the same
    distinction for the same reason.
    """
    if not group_header:
        return [UNGROUPED_LABEL]

    raw = row.get(group_header, "")

    if is_person_column:
        names = resolver.resolve_cell(raw) if resolver else split_cell(raw)
        return names or [UNASSIGNED_GROUP_LABEL]

    text = str(raw).strip()
    return [text] if text else [BLANK_GROUP_LABEL]


def _groupable_headers(
    headers: List[str],
    rows: List[Dict[str, str]],
    people_headers: List[str],
    date_headers: Tuple[Optional[str], ...] = (),
) -> List[str]:
    """Columns worth offering as a grouping.

    A free-text description column technically groups; it produces one group per row and
    a chart nobody can read. Cardinality decides, except for people columns, which are
    always offered — a 40-person roster is exactly the grouping someone wants even though
    it exceeds the cap and spills into `Other`.

    `rows` must be the tab's rows, **not** the filtered ones. Which columns can group a
    tab is a fact about the tab: computed over a filter narrowed to one person whose rows
    all leave `Module` blank, `Module` drops out of the list while still being the active
    grouping, and the client's `<select value>` then matches no option and silently shows
    something else — the control misreporting what the server actually grouped by.

    `date_headers` are the resolved start and due columns, skipped because grouping a
    timeline by the very column it is drawn from is never what anyone means: it yields one
    group per distinct date, all of them single-day.
    """
    skip = {h for h in date_headers if h}
    out: List[str] = []
    for header in headers:
        if not header or header in skip:
            continue
        if header in people_headers:
            out.append(header)
            continue
        distinct = {str(r.get(header, "")).strip() for r in rows}
        distinct.discard("")
        if 0 < len(distinct) <= MAX_GROUPS:
            out.append(header)
    return out


def build_timeline(
    headers: List[str],
    rows: List[Dict[str, str]],
    tab_schema: Dict[str, Any],
    *,
    group_by: Optional[str] = None,
    resolver: Optional[Any] = None,
    today: Optional[date] = None,
    all_rows: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Everything a timeline panel needs, computed from the tab's own vocabulary.

    `today` is a parameter rather than `date.today()` so a timeline is reproducible; one
    that cannot be regenerated identically cannot be checked against what a user saw.
    `core/digest.py:build_digest` takes it for the same reason.

    `rows` is the filtered set — everything drawn and every count reported is about it.
    `all_rows` is the tab before filtering and feeds one thing only: `groupable`, which
    answers "what could this tab be grouped by" and must not shrink because the reader
    narrowed to one person. It defaults to `rows` for callers with no filter to speak of.

    The due column is resolved first and handed to `get_start_column` as an exclusion,
    because the two word lists overlap and a single-date tab would otherwise resolve one
    header to both ends of every bar.
    """
    today = today or date.today()

    due_name, due_from_schema = resolve_due_column(tab_schema, headers)

    # A *guessed* deadline that reads as a start is a start. `_DUE_WORDS` contains
    # "planned" and `_START_WORDS` contains "start", so on a tab whose only date column is
    # "Planned Start Date" and whose schema maps nothing, the due scan claims it, the start
    # scan is then handed it as an exclusion and finds nothing, and every row becomes a
    # deadline milestone — unfinished rows whose *start* has merely passed are drawn
    # overdue, counted overdue and captioned overdue. That is §16.6's inversion in mirror
    # image: an obligation the sheet never recorded, invented by a substring match.
    #
    # Only the scan is overruled. A `date_columns` mapping is a human decision about that
    # sheet and outranks this vocabulary, so `due_from_schema` short-circuits the test —
    # and `get_due_column` itself is untouched, because `summarize(overdue)`, the health
    # panel and the grid's overdue filter share it and this branch has not verified them.
    if due_name and not due_from_schema and header_reads_as_a_start(due_name):
        start_name = get_start_column(tab_schema, headers) or due_name
        due_name = None
    else:
        start_name = get_start_column(tab_schema, headers, exclude=due_name)

    due_header = resolve_header(headers, due_name)
    start_header = resolve_header(headers, start_name)

    # The other half of a four-column tab: when the work actually started and finished, as
    # against when it was meant to. `(None, None)` on a tab that records only one pair, and
    # every branch below is written so that case draws exactly what it drew before.
    actual_start_name, actual_due_name = resolve_actual_columns(
        tab_schema, headers, exclude=(start_name, due_name)
    )
    actual_start_header = resolve_header(headers, actual_start_name)
    actual_due_header = resolve_header(headers, actual_due_name)
    has_actual = bool(actual_start_header or actual_due_header)

    id_header = resolve_header(headers, tab_schema.get("primary_id_column"))
    desc_header = resolve_header(headers, tab_schema.get("description_column"))
    status_header = resolve_header(headers, tab_schema.get("status_column"))
    people = bind_columns(get_people_columns(tab_schema), headers)
    people_headers = [p["header"] for p in people]

    # Which cells the row dialog may edit. Dates and status join the people columns here;
    # RBAC still gates every one of them field-by-field at dispatch (§6.2), so widening
    # the affordance cannot widen anyone's permissions.
    #
    # Deduplicated because `people_columns` is free-form: nothing stops a schema declaring
    # the status column as a people column too, and a header listed twice renders the same
    # field twice in the row dialog.
    editable = list(dict.fromkeys(
        h for h in (
            [start_header, due_header, actual_start_header, actual_due_header, status_header]
            + people_headers
        ) if h
    ))

    group_header = resolve_header(
        headers, group_by if group_by else tab_schema.get("module_column")
    )
    is_person_group = group_header in people_headers if group_header else False

    counts = {"total": len(rows), "charted": 0, "milestone_only": 0, "undated": 0, "unparsed": 0}
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    bucket_counts: Dict[str, int] = {}
    # Every drawable item exactly once, whatever number of groups it ends up in. A people
    # grouping puts one shared row in two buckets deliberately, so the buckets are the
    # wrong place to compute a range or to stamp a flag from — and because the entries are
    # the same dict object, stamping it here is visible through every bucket holding it.
    placed: List[Dict[str, Any]] = []
    unplaced: List[Dict[str, Any]] = []

    today_iso = today.isoformat()

    for row in rows:
        plan_kind, planned = _segment(row, start_header, due_header)
        actual_kind, actual = (
            _segment(row, actual_start_header, actual_due_header)
            if has_actual else ("undated", None)
        )

        # One bucket for the row, decided across both segments. `unparsed` still beats
        # everything for the reason classify_row gives — a chart that draws the readable
        # half of a row hides the half that is wrong — and a row is a "bar" when either
        # segment spans time, because that is what the coverage line means by the word.
        if "unparsed" in (plan_kind, actual_kind):
            kind = "unparsed"
        elif planned is None and actual is None:
            kind = "undated"
        elif "bar" in (plan_kind, actual_kind):
            kind = "bar"
        else:
            kind = "milestone"

        if kind == "undated":
            counts["undated"] += 1
        elif kind == "unparsed":
            counts["unparsed"] += 1
        elif kind == "bar":
            counts["charted"] += 1
        else:
            counts["milestone_only"] += 1

        labels = _group_labels(row, group_header, is_person_group, resolver)
        for label in labels:
            bucket_counts[label] = bucket_counts.get(label, 0) + 1
            buckets.setdefault(label, [])

        if kind in ("undated", "unparsed"):
            # Listed rather than merely counted. These are the rows a reader opens the
            # panel to schedule, and every one of them was previously unreachable from it:
            # the only way into the row dialog was clicking a bar, which by definition
            # these have not got. They are kept out of `groups` — 347 empty tracks would
            # bury the 62 real ones — and carried in their own array for the panel below
            # the chart.
            unplaced.append({
                **_row_identity(row, id_header, desc_header, people, editable),
                "kind": kind,
            })
            continue

        status = str(row.get(status_header, "")) if status_header else ""
        primary = planned or actual or {}

        # The row's envelope: earliest date it records to latest, across both segments.
        # The axis range, the group rollups and the row ordering all read these, and all
        # three want "how far does this row reach", not "which of its two bars is real".
        # The chart draws from the segments themselves.
        span = [
            iso for segment in (planned, actual) if segment
            for iso in (segment["start"], segment["end"]) if iso
        ]

        planned_finish, actual_finish = _finish(planned), _finish(actual)
        item = {
            **_row_identity(row, id_header, desc_header, people, editable),
            "kind": kind,
            "start": min(span) if span else None,
            "end": max(span) if span else None,
            # Named, never aliased. A tab recording one pair of dates gets `actual: null`
            # and draws exactly what it drew before any of this existed.
            "planned": planned,
            "actual": actual,
            # How late the work landed, in days, and only when both ends of the comparison
            # are recorded. Negative means early. This is a fact about a finished row and
            # is the reason `overdue` below refuses to describe one.
            "slip_days": (
                (date.fromisoformat(actual_finish) - date.fromisoformat(planned_finish)).days
                if planned_finish and actual_finish else None
            ),
            "milestone_of": primary.get("milestone_of"),
            "reversed": bool(
                (planned or {}).get("reversed") or (actual or {}).get("reversed")
            ),
            "status": status,
            # Overdue only ever describes a real deadline, so a start-only milestone is
            # never overdue however old it is — nothing was promised for that date. And a
            # recorded actual finish discharges the promise however late it was: that row
            # slipped, which `slip_days` says precisely. Reporting delivered work as
            # overdue is §16.6's inversion in a third costume.
            "overdue": bool(
                planned_finish
                and not actual_finish
                and planned_finish < today_iso
                and not is_finished_status(status)
            ),
            # Set below, once every date is in and the fence can be drawn. Present here so
            # the key exists on every item regardless of which branch computes it.
            "out_of_range": False,
        }
        placed.append(item)
        for label in labels:
            buckets[label].append(item)

    groups: List[Dict[str, Any]] = []
    for label, items in buckets.items():
        starts = [i["start"] for i in items if i["start"]]
        ends = [i["end"] for i in items if i["end"]]
        groups.append({
            "label": label,
            "count": bucket_counts.get(label, len(items)),
            # ISO dates sort lexicographically, so min/max on the strings is the same
            # answer as parsing them back and is one fewer conversion to get wrong.
            "start": min(starts) if starts else None,
            "end": max(ends) if ends else None,
            "items": sorted(items, key=lambda i: (i["start"] or "9999", i["label"])),
        })

    # Earliest first; groups with nothing dated sink to the bottom rather than sorting as
    # if they began at the dawn of the axis.
    groups.sort(key=lambda g: (g["start"] is None, g["start"] or "", g["label"]))

    if len(groups) > MAX_GROUPS:
        head, tail = groups[:MAX_GROUPS], groups[MAX_GROUPS:]
        tail_starts = [g["start"] for g in tail if g["start"]]
        tail_ends = [g["end"] for g in tail if g["end"]]

        # The fold is the one place a row can appear twice under one label. A people
        # grouping puts a shared cell ("Ahmed Qamar/Asif") in two buckets on purpose, and
        # `_groupable_headers` offers people columns *because* a large roster spills past
        # the cap — so both of that row's buckets landing in the tail is the expected case,
        # not a corner. Concatenating then draws the row twice under one collapsed group
        # while `counts.charted` counts it once, and hands the client two children with one
        # key. Per-group duplication elsewhere is deliberate and stays; this one is not.
        folded: List[Dict[str, Any]] = []
        seen = set()
        for group in tail:
            for item in group["items"]:
                identity = _item_identity(item)
                if identity in seen:
                    continue
                seen.add(identity)
                folded.append(item)

        # A grouping column can legitimately hold the value "Other" — a plausible module
        # name — and that group can survive into `head`. Appending a second group under the
        # same label fuses the two in any client that keys its list and its collapse state
        # on the label, which is what §14.12 recorded as knowingly shipped. Every other
        # label is a `buckets` key and therefore already distinct, so making this one free
        # of `head` closes it. Growing the string terminates; it only runs at all on a tab
        # that really does have a group called "Other".
        taken = {g["label"] for g in head}
        other_label = OTHER_GROUP_LABEL
        while other_label in taken:
            other_label = f"{other_label} (folded)"

        head.append({
            "label": other_label,
            "count": sum(g["count"] for g in tail),
            "start": min(tail_starts) if tail_starts else None,
            "end": max(tail_ends) if tail_ends else None,
            "items": folded,
            "collapsed_groups": len(tail),
        })
        groups = head

    # Read off `placed`, not off `groups`: the same item appears under two labels when the
    # grouping column names two people, and a duplicated date would shift the quartiles
    # towards whichever rows happen to be shared.
    all_dates: List[date] = []
    for item in placed:
        for iso in (item["start"], item["end"]):
            if iso:
                all_dates.append(date.fromisoformat(iso))

    range_start, range_end, range_clamped = _drawn_range(all_dates)
    lo_iso, hi_iso = _iso(range_start), _iso(range_end)
    if range_clamped:
        for item in placed:
            item["out_of_range"] = bool(
                (item["start"] and lo_iso and item["start"] < lo_iso)
                or (item["end"] and hi_iso and item["end"] > hi_iso)
            )

    reason = None
    if not start_header and not due_header:
        reason = "This tab declares no start or deadline column, so there is nothing to place on a timeline."
    elif counts["charted"] + counts["milestone_only"] == 0:
        reason = "No row on this tab records a readable date."
    if reason:
        groups = []

    return {
        "start_header": start_header,
        "due_header": due_header,
        # Null on a tab that records one pair of dates, which is most of them. The panel
        # reads these to decide whether to draw one bar per row or a plan and an outcome,
        # and stamps all four names beside the coverage line so "no baseline" is
        # distinguishable from "the baseline resolved to the wrong column".
        "actual_start_header": actual_start_header,
        "actual_due_header": actual_due_header,
        "group_by": group_header,
        "groupable": _groupable_headers(
            headers,
            rows if all_rows is None else all_rows,
            people_headers,
            (start_header, due_header),
        ),
        "editable_headers": editable,
        "people_columns": people,
        "range_start": lo_iso,
        "range_end": hi_iso,
        # Whether the two above are the extremes of the data or a fence drawn inside them.
        # The panel says so out loud: a reader who can see 2206 in the sheet and not on the
        # chart is owed the reason, and the row carrying it is the one worth fixing.
        "range_clamped": range_clamped,
        "today": today.isoformat(),
        "groups": groups,
        # Rows with no date to draw at, in the same shape as a drawn one so the row dialog
        # opens on either without knowing which it got.
        "unplaced": unplaced,
        "counts": counts,
        "reason": reason,
    }
