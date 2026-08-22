import re
from typing import Any, Dict, List, Optional, Tuple

from app.core.people import slugify_role

# schema_config (models/project.py:Project.schema_config) is JSONB in one of two
# shapes distinguished by a top-level "tabs" key: multi-tab (what
# schema_detect.py:detect_all_tabs produces) or flat, single-tab. Before this module
# existed the disambiguation was copy-pasted at seven call sites (TDD §16.20) —
# read.py, write.py (x3), worker.py, chat.py, agentic_loop.py — each spelled
# slightly differently. This is the one implementation.


def get_tab_schema(schema_config: Dict[str, Any], active_tab: str) -> Dict[str, Any]:
    """Resolve the effective per-tab schema dict for active_tab."""
    if "tabs" in schema_config:
        return schema_config.get("tabs", {}).get(active_tab, {})
    return schema_config


def get_people_columns(tab_schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The tab's people-columns, normalised to one shape for every caller.

    Detection now emits a `people_columns` list, because a sheet can name several
    responsible parties per row (a technical *and* a functional consultant, a QA owner,
    a business owner) and the old single `assignee_column` silently kept only one.

    Projects registered before that change still carry only `assignee_column`, and
    nothing re-detects them automatically (an admin re-runs detection from
    /admin/projects). So a legacy config is synthesised into a one-entry list here
    rather than at each call site: consumers read one shape, and an un-migrated project
    keeps working with the single role it always had instead of rendering no people at all.
    """
    columns = tab_schema.get("people_columns")
    if isinstance(columns, list) and columns:
        out = []
        for col in columns:
            header = col.get("header") if isinstance(col, dict) else None
            if not header:
                continue
            label = col.get("label") or str(header).strip()
            out.append({
                "key": col.get("key") or slugify_role(label),
                "label": label,
                "header": header,
            })
        if out:
            return out

    legacy = tab_schema.get("assignee_column")
    if legacy:
        label = str(legacy).strip()
        return [{"key": slugify_role(label), "label": label, "header": legacy}]

    return []


def get_effort_columns(tab_schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The tab's effort/duration columns, or [] when the sheet has none.

    An empty list is a normal, expected outcome — plenty of trackers record no
    level-of-effort at all. Callers must fall back to item counts and label them as
    counts rather than presenting a derived number as if the sheet had stated it.
    """
    columns = tab_schema.get("effort_columns")
    if not isinstance(columns, list):
        return []
    out = []
    for col in columns:
        header = col.get("header") if isinstance(col, dict) else None
        if not header:
            continue
        label = col.get("label") or str(header).strip()
        out.append({
            "key": col.get("key") or slugify_role(label),
            "label": label,
            "header": header,
            "unit": col.get("unit") or "days",
        })
    return out


# Words that mark a column as a *deadline*. Deliberately excludes "completion",
# "closed" and "sign-off": those record when something actually finished, and treating
# one as a deadline reports finished work as overdue — the exact inversion of the
# question being asked.
_DUE_WORDS = ("due", "deadline", "target", "expected", "planned", "go-live", "go live", "golive", "eta")


def resolve_due_column(
    tab_schema: Dict[str, Any], headers: Optional[List[str]] = None
) -> Tuple[Optional[str], bool]:
    """`get_due_column`'s answer, plus **where it came from**.

    Returns `(header, from_schema)`. `from_schema` is True only when a human mapped the
    column in `date_columns`; a header the scan below guessed at reports False, and so
    does the None result.

    That distinction exists because the scan and `get_start_column`'s scan share
    vocabulary — `"planned"` is a due word and `"start"` is a start word, so
    `"Planned Start Date"` matches both — and a caller drawing a *span* has to be able to
    tell a stated deadline from a guessed one before it treats such a header as the right
    edge. `core/timeline.py:build_timeline` is that caller and the only one so far; see
    §10.9 for why it prefers the start reading. A guess is worth overruling, and a human's
    mapping is not.

    `get_due_column` keeps its own signature and delegates here, because every consumer of
    "overdue" resolves through it and none of them should have to grow a tuple.
    """
    dates = tab_schema.get("date_columns") or {}
    for key in ("due", "go_live"):
        value = dates.get(key)
        if value and str(value).strip():
            return value, True

    for header in headers or []:
        if header and any(word in str(header).casefold() for word in _DUE_WORDS):
            return header, False

    return None, False


def get_due_column(tab_schema: Dict[str, Any], headers: Optional[List[str]] = None) -> Optional[str]:
    """The column holding each row's deadline, verbatim, or None if the sheet has none.

    Every consumer of "overdue" resolves through here so chat and the dashboard cannot
    disagree about what the word means.

    Two failures made this necessary, both observed in production:

    * `date_columns.get("go_live", "Go-Live Date")` looks like it has a default, but
      detection writes the key with a literal `null`, so `.get` returns None and the
      default never applies. `summarize(report_type="overdue")` then failed on every
      project, which is what sent the agent looping through search_rows guessing at date
      substrings until it hit the iteration cap.
    * Detection has no slot for a plain due date, so a sheet whose deadline column is
      "Expected Completetion Date" mapped nothing, while "Date Completion" — the
      *actual* completion date — was mapped as `completion`.

    Hence the header scan: it recovers a deadline column on projects that will never be
    re-detected (there is no automatic backfill). It is a read-path heuristic only,
    never consulted for a write, and it returns None rather than settling for a
    completion date — "no due-date column" is a true and useful answer.
    """
    return resolve_due_column(tab_schema, headers)[0]


# Words that mark a column as a *planned start*, and the guard that keeps them off people
# columns. schema_detect's structural fallback matches start columns with
# ["start", "created", "opened", "assigned"] — but "assigned" is a substring of "Assigned
# To", and "created"/"opened"/"raised" are each a substring of "Created By"/"Opened
# By"/"Raised By". Those are people columns on most trackers. One idea covers all of them:
# a header that names *who* did something never says *when* it happened. So "assigned" is
# left out of the word list, and any header carrying a standalone "by" token is rejected
# even when it matches a start word. The words themselves have to stay — "Raised On" and
# "Date Created" are genuine starts — so the guard is on "by", not on the vocabulary.
#
# This matters more than a merely-missing bar: a person's name parses to no date, so every
# such row would land in the `unparsed` bucket that is reserved for malformed dates like
# "17/0/2026", quietly corrupting the coverage figure the panel exists to be honest about.
#
# "by" is matched as a whole token, never as a substring — "Standby Start Date" is a real
# start column and rejecting it would be the guard overreaching. The guard does cost one
# genuine case: "Start By" (plausibly the latest acceptable start) now resolves to nothing,
# which is the safe direction — a missing bar is visible, a bar drawn from a person's name
# is not — and schema_config overrides it for any sheet that really uses that wording.
_START_WORDS = ("start", "begin", "kickoff", "kick-off", "created", "opened", "raised")
_BY_TOKEN = re.compile(r"\bby\b")


def _same_header(a: Optional[str], b: Optional[str]) -> bool:
    """Whitespace- and case-insensitive header comparison."""
    if not a or not b:
        return False
    return str(a).strip().casefold() == str(b).strip().casefold()


def header_reads_as_a_start(header: Optional[str]) -> bool:
    """Whether this header *looks like* a planned start, under the scan's own rules.

    `get_start_column`'s header scan is this predicate plus a loop, and it is written once
    here so a caller asking "would the start scan have taken this header?" cannot answer
    with a second, drifting copy of the vocabulary and the `by` guard. It says nothing
    about what a schema declares — a `schema_config` mapping is a human decision and is
    never subject to this test.
    """
    if not header:
        return False
    candidate = str(header).casefold()
    if _BY_TOKEN.search(candidate):
        return False
    return any(word in candidate for word in _START_WORDS)


# Words that mark a column as recording when something *actually* happened rather than
# when it was meant to. This is very nearly the list `_DUE_WORDS` was written to exclude,
# and for the same reason read from the other side: a completion date is not a deadline,
# so treating it as one reports finished work as overdue — and treating a deadline as a
# completion would report unfinished work as delivered. The two vocabularies are disjoint
# on purpose and the scan below enforces it, refusing any header that also reads as a
# deadline. "Expected Completion Date" is a stated deadline that happens to contain
# "completion", and it must not be read as the day the work landed.
_ACTUAL_TOKEN = "actual"
_ACTUAL_WORDS = (
    "actual", "completion", "completed", "finished", "closed",
    "sign-off", "signoff", "signed off", "delivered", "delivery date",
)


def _header_is_free(header: Optional[str], taken: set) -> bool:
    return bool(header) and str(header).strip().casefold() not in taken


def resolve_actual_columns(
    tab_schema: Dict[str, Any],
    headers: Optional[List[str]] = None,
    *,
    exclude: Tuple[Optional[str], ...] = (),
) -> Tuple[Optional[str], Optional[str]]:
    """The pair recording when work actually started and actually finished, if any.

    Returns `(actual_start, actual_due)`, either or both None. A tracker with two date
    columns has no such pair and gets `(None, None)`, which is what keeps a plain sheet
    drawing exactly as it did before this existed.

    `exclude` is the already-resolved planned pair. A header can only serve one end of one
    segment, and without this a four-column tab whose planned deadline is "Expected
    Completion Date" would hand the same column to both segments and draw a row's plan and
    its outcome as the same bar — the zero-length-bar failure `get_start_column`'s
    `exclude` exists to prevent, one level up.

    Schema first, in the order detection actually writes: `actual_start` and `actual_due`
    are roles a human can map, and `completion` is the role detection has been writing all
    along for "Date Completion" on the reference tracker. Reading it here is what lets a
    sheet that was detected years ago grow a baseline without being re-detected. Every key
    is read with a truthiness test, never `dict.get(key, default)` — detection writes these
    keys holding a literal `null` and a default would never apply (§16.6).
    """
    dates = tab_schema.get("date_columns") or {}
    taken = {str(h).strip().casefold() for h in exclude if h}

    def _mapped(*keys: str) -> Optional[str]:
        for key in keys:
            value = dates.get(key)
            if value and str(value).strip():
                return value
        return None

    actual_start = _mapped("actual_start", "started")
    actual_due = _mapped("actual_due", "actual_end", "completion")

    if actual_start is None:
        # An actual start is a start column wearing the qualifier. Nothing weaker will do:
        # every start-looking header that is *not* qualified is a candidate for the planned
        # start, and claiming one here would silently move the baseline.
        for header in headers or []:
            if not _header_is_free(header, taken):
                continue
            if header_reads_as_a_start(header) and _ACTUAL_TOKEN in str(header).casefold():
                actual_start = header
                break

    if actual_due is None:
        for header in headers or []:
            if not _header_is_free(header, taken) or header_reads_as_a_start(header):
                continue
            low = str(header).casefold()
            # A deadline word disqualifies the header outright, however many completion
            # words it also carries. "Expected Completion Date" is a promise, not a record.
            if any(word in low for word in _DUE_WORDS):
                continue
            if any(word in low for word in _ACTUAL_WORDS):
                actual_due = header
                break

    # One column cannot be both ends of the same segment.
    if _same_header(actual_start, actual_due):
        actual_due = None

    return actual_start, actual_due


def get_start_column(
    tab_schema: Dict[str, Any],
    headers: Optional[List[str]] = None,
    exclude: Optional[str] = None,
) -> Optional[str]:
    """The column holding each row's planned start, verbatim, or None if there is none.

    `get_due_column`'s mirror, and it repeats that function's shape deliberately rather
    than sharing an abstraction: the two word lists encode opposite judgements about the
    same headers, and a shared helper would invite someone to "unify" them.

    Reads the schema key with a truthiness test rather than `dict.get(key, default)`.
    Detection writes `date_columns` keys holding a literal `null` on the LLM path
    (`schema_detect.detect_schema_config`; only `_structural_fallback` strips falsy keys),
    and a key that exists holding None ignores the default — the exact defect that broke
    `summarize(report_type="overdue")` on every registered project (§16.6).

    `exclude` is the already-resolved due column. The two word lists overlap — "Planned
    Start" contains "planned", which is in `_DUE_WORDS` — so without this a single-date
    tab resolves the same header to both ends of the span and every row draws a
    zero-length bar. A zero-length bar looks like data; no bar at all looks like the
    mapping gap it actually is. When the *explicit* mapping is the one that collides, this
    returns None outright rather than scanning on: a schema that names one column for both
    ends of the span is a mapping error, and substituting some other start-looking header
    for the one a human chose would hide it behind a bar drawn from the wrong column.

    The header scan additionally refuses any header carrying a standalone "by" token, so
    "Created By" and "Raised By" stay people columns — see the note above `_START_WORDS`.
    A mapping the schema states explicitly is trusted and skips that guard: it is a human
    decision, not a guess from a substring.
    """
    dates = tab_schema.get("date_columns") or {}
    value = dates.get("start")
    if value and str(value).strip():
        # A mapping that collides with the due column is a mapping error, and the honest
        # answer is no start column at all. Scanning on from here would return some other
        # start-looking header in place of the one the human actually chose — trading a
        # stated decision for a guess, which is exactly what reading the schema first is for.
        return None if _same_header(value, exclude) else value

    for header in headers or []:
        if not header or _same_header(header, exclude):
            continue
        if header_reads_as_a_start(header):
            return header

    return None


def get_available_tabs(schema_config: Dict[str, Any]) -> List[str]:
    """Resolve the tab names this project can switch between, for system-prompt injection.

    Replaces the old get_valid_modules(). The rename is the point: that function returned
    tab names but was injected into the prompt as "Valid modules: …", which the model then
    enforced as an allowlist of ID prefixes. On a single-tab sheet it returned nothing, and
    the prompt substituted a hardcoded SAP list, so an ID like SLCM-0586 was refused on a
    sheet that had never heard of SAP modules. Tabs are a navigation concept (switch_module);
    they are not a vocabulary of legal identifiers, and nothing derives one from the other.

    A flat, single-tab config has nowhere to switch to, so it yields an empty list.
    """
    if "tabs" in schema_config:
        return list(schema_config.get("tabs", {}).keys())
    return []


def resolve_header(headers: Optional[List[str]], wanted: Optional[str]) -> Optional[str]:
    """The key `wanted` actually has in a row dict, or None if the tab has no such column.

    schema_config stores headers *verbatim*, trailing spaces and all (§7.2) — the write
    path needs the exact string to address a cell. Row dicts, however, are keyed by
    `meta.get_header_row`'s output, which is stripped. So `row.get("Technical Resource ")`
    on a tab whose schema declares that trailing space returns None for every row, and the
    column reads as universally blank rather than as missing.

    That silence is why this exists. A column that resolves to nothing looks exactly like
    a column nobody filled in, and the dashboard would have reported every row on such a
    tab as unassigned — a confident, specific, entirely wrong number.

    Exact match wins first, so a sheet that genuinely has two headers differing only in
    whitespace keeps the one its schema names.
    """
    if not wanted:
        return None
    available = [h for h in (headers or []) if h]
    if wanted in available:
        return wanted
    needle = str(wanted).strip().casefold()
    for header in available:
        if str(header).strip().casefold() == needle:
            return header
    return None


def bind_columns(
    columns: List[Dict[str, Any]], headers: Optional[List[str]]
) -> List[Dict[str, Any]]:
    """Re-point a list of role columns at the header keys this tab actually uses.

    Columns the tab does not have are dropped rather than kept with a dead header, so a
    caller iterating the result never has to ask whether a column exists. A schema that
    names a column the sheet has since removed is a stale mapping, not an empty column.
    """
    out = []
    for col in columns or []:
        header = resolve_header(headers, col.get("header"))
        if header:
            out.append({**col, "header": header})
    return out


def default_critical_headers(
    tab_schema: dict, headers: Optional[List[str]] = None
) -> List[str]:
    """The columns worth reporting on when nobody has named `critical_fields` explicitly.

    Built from schema roles — id, module, type, description, status, then every people
    column — and filtered to the headers this tab actually has. Three call sites used to
    carry the same literal list from one customer's WRICEF tracker
    (`["RICEFW ID", "Module", "Type", "Description", "Dev Status", "Technical Resource "]`):
    `search_rows`' default return fields, `run_data_quality_check`'s blank-field default,
    and `DataQualityChecker.completeness_score`. On any other sheet all three resolved to
    columns that do not exist, so search returned nothing useful and the quality checks
    counted every row as blank (§16.3).

    Returns headers in the tab's own spelling, resolved through `resolve_header`, so the
    result can be used as a row-dict key directly.
    """
    roles = [
        tab_schema.get("primary_id_column"),
        tab_schema.get("module_column"),
        tab_schema.get("type_column"),
        tab_schema.get("description_column"),
        tab_schema.get("status_column"),
    ]
    people = [p.get("header") for p in get_people_columns(tab_schema)]

    out: List[str] = []
    for wanted in roles + people:
        if not wanted:
            continue
        # With no header list to check against, the schema's own spelling is all there is.
        resolved = resolve_header(headers, wanted) if headers else wanted
        if resolved and resolved not in out:
            out.append(resolved)
    return out


# The sheet row a row dict came from, attached under a key no header can collide with
# (`_row_dicts` skips empty headers, and a real header is never dunder-wrapped). It lives
# here rather than in api/dashboard.py because core/timeline.py reads it too, and two
# copies of a magic string are how the grid and the timeline end up disagreeing about
# which row the user clicked — which for a duplicated ID is the difference between an
# edit that lands and an edit that lands somewhere else (§16.7).
ROW_NUMBER_KEY = "__row_number__"
