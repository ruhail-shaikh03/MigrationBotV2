# Timeline Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only timeline (Gantt-style) panel to `/project/[id]` that draws each tracker row as a bar or milestone on a shared time axis, grouped by a column the sheet already has.

**Architecture:** One pure, I/O-free module (`core/timeline.py`) computes everything from a header list, row dicts and the tab's `schema_config`. One thin endpoint (`api/timeline.py`) composes the existing read pipeline around it — the same `_resolve_project` → `get_tab_matrix` → `_row_dicts` → `_filter_rows` chain the grid and CSV export already use. The frontend adds a fourth `view` state whose renderer lives in the shared `DataDisplay.tsx` primitives, and extracts the grid's inline-edit machinery into a reusable hook so the timeline's row dialog cannot drift from the grid's cells.

**Tech Stack:** FastAPI + Pydantic v2 + SQLAlchemy 2.0 (async), pytest; Next.js 16 App Router, React 19, Tailwind v4, lucide-react. **No new runtime dependency is added on either side.**

**Spec:** `docs/superpowers/specs/2026-08-20-timeline-panel-design.md`

## Global Constraints

- **Branch:** all work happens on `feat/timeline-panel`, cut from `main` at `cd2b2fa`. `main` is currently clean.
- **No new dependencies.** Nothing may be added to `backend/requirements.txt` or `frontend/package.json`. Recharts has no Gantt primitive; the renderer is hand-written CSS/absolute positioning.
- **No writes, no new scope, no new table.** This feature adds no Sheets write path, no OAuth scope, no Postgres table, no Alembic migration, and no agent tool.
- **No header string may be spelled in `core/timeline.py`.** Every column is resolved from a `schema_config` role through `resolve_header` / `bind_columns`. This is the rule `core/health.py` states in its module docstring and the reason `core/data_quality.py` was not reused on the dashboard path.
- **`today` is always a parameter,** never `date.today()` inside pure code. Matches `core/digest.py:build_digest`.
- **Python:** local interpreter is 3.13.2; CI runs 3.12. Do not use 3.13-only syntax.
- **Local test command** (the seven env vars are required — `app/config.py` raises at import without them, so pytest cannot even collect):

```bash
cd /d/TMC/MigrationBot/migrationbot/backend && \
DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/migrationbot_test" \
REDIS_URL="redis://localhost:6379" \
DEEPSEEK_API_KEY=mock \
JWT_SECRET=test-secret-32-characters-long-xx \
CORS_ORIGINS="http://localhost:3000" \
ADMIN_EMAILS="a@example.com" \
DEFAULT_SPREADSHEET_ID=test \
python -m pytest tests/test_core tests/test_sheets -q
```

- **Baseline to protect:** `382 passed` on that command, measured on `main` at `cd2b2fa`. Every task must leave it at 382 + the tests that task added.
- **`tests/test_db.py` and `tests/integration/` are NOT runnable locally** (no Docker, no Postgres on this machine). Never run bare `pytest`; always scope to `tests/test_core tests/test_sheets`.
- **Ruff is scoped to `E9,F`** (`backend/pyproject.toml`), line length 120. Run `ruff check backend/app` from the repo root.
- **Frontend has no test runner.** `devDependencies` contains no jest/vitest and none is to be added. Frontend verification is `npx tsc --noEmit`, `npm run lint`, `npm run build` — and per TDD §15.4 those prove the code *compiles*, not that it works. The rendering must be checked against a deploy before this is called done.
- **TDD.md is updated before the final commit**, from the merged diff rather than from this plan. TDD.md has twice recorded itself asserting code that never existed; do not let it happen a third time.

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/app/core/timeline.py` | **New.** Pure. Classifies each row as bar/milestone/undated/unparsed, groups rows, computes group rollups and coverage counts. No I/O, no imports from `app.api` or `app.sheets`. |
| `backend/app/core/schema.py` | **Modify.** Add `get_start_column` and move `ROW_NUMBER_KEY` here so `core/` and `api/` share one literal. |
| `backend/app/api/timeline.py` | **New.** One `GET` endpoint. Composition only — no computation. Modelled on `api/digest.py`. |
| `backend/app/api/dashboard.py` | **Modify.** Import `ROW_NUMBER_KEY` from `core/schema.py` instead of defining `_ROW_NUMBER_KEY` locally. |
| `backend/app/main.py` | **Modify.** Mount the new router. |
| `backend/tests/test_core/test_start_column.py` | **New.** Mirrors `test_due_column.py`. |
| `backend/tests/test_core/test_timeline.py` | **New.** The pure module's behaviour. |
| `backend/tests/test_core/test_timeline_api.py` | **New.** The endpoint's gating and composition. |
| `frontend/src/hooks/useRowEdits.ts` | **New.** Owns `pending`, `editing`, `saveCell`, the `queue_update` listener and the job-status poll — lifted verbatim out of `project/[id]/page.tsx`. |
| `frontend/src/components/DataDisplay.tsx` | **Modify.** Add `EditableCell` and `TimelineChart` beside the existing primitives. |
| `frontend/src/app/project/[id]/page.tsx` | **Modify.** Fourth view; consume the hook instead of local edit state. |
| `TDD.md` | **Modify.** New §10.9 and §14.12; §16.3 note. |

**Ordering rationale.** Tasks 1–4 are backend and each is independently testable. Task 5 is a pure refactor with no behaviour change, deliberately isolated so a reviewer can reject it without rejecting the feature. Tasks 6–7 build the UI on top. Task 8 documents and verifies.

---

## Task 1: `get_start_column` — resolve the left edge of a bar

**Files:**
- Modify: `backend/app/core/schema.py` (append after `get_due_column`, which ends at line 121)
- Test: `backend/tests/test_core/test_start_column.py` (create)

**Interfaces:**
- Consumes: `resolve_header` (already in `core/schema.py`)
- Produces: `get_start_column(tab_schema: Dict[str, Any], headers: Optional[List[str]] = None, exclude: Optional[str] = None) -> Optional[str]` — used by Task 2.

- [ ] **Step 1: Cut the branch**

```bash
cd /d/TMC/MigrationBot/migrationbot && git checkout main && git pull && git checkout -b feat/timeline-panel && git status --short
```

Expected: on `feat/timeline-panel`; only `docs/superpowers/specs/2026-08-20-timeline-panel-design.md` and `docs/superpowers/plans/2026-08-20-timeline-panel.md` untracked.

- [ ] **Step 2: Commit the spec and this plan first**

```bash
cd /d/TMC/MigrationBot/migrationbot && git add docs/superpowers && git commit -m "docs(timeline): spec and implementation plan for the timeline panel

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

- [ ] **Step 3: Write the failing test**

Create `backend/tests/test_core/test_start_column.py`:

```python
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
```

- [ ] **Step 4: Run the test to verify it fails**

```bash
cd /d/TMC/MigrationBot/migrationbot/backend && \
DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/migrationbot_test" REDIS_URL="redis://localhost:6379" DEEPSEEK_API_KEY=mock JWT_SECRET=test-secret-32-characters-long-xx CORS_ORIGINS="http://localhost:3000" ADMIN_EMAILS="a@example.com" DEFAULT_SPREADSHEET_ID=test \
python -m pytest tests/test_core/test_start_column.py -q
```

Expected: collection error — `ImportError: cannot import name 'get_start_column' from 'app.core.schema'`.

- [ ] **Step 5: Implement `get_start_column`**

In `backend/app/core/schema.py`, immediately after `get_due_column` (which ends `return None` at line 121), insert:

```python
# Words that mark a column as a *planned start*. Deliberately excludes "assigned":
# schema_detect's structural fallback matches start columns with
# ["start", "created", "opened", "assigned"], and "assigned" is a substring of
# "Assigned To" — a people column on most trackers, which would draw every bar from a
# person's name. Also excludes "actual" and "finish", mirroring why _DUE_WORDS excludes
# "completion": when work really began is a different fact from when it was due to.
_START_WORDS = ("start", "begin", "kickoff", "kick-off", "created", "opened", "raised")


def _same_header(a: Optional[str], b: Optional[str]) -> bool:
    """Whitespace- and case-insensitive header comparison."""
    if not a or not b:
        return False
    return str(a).strip().casefold() == str(b).strip().casefold()


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
    mapping gap it actually is.
    """
    dates = tab_schema.get("date_columns") or {}
    value = dates.get("start")
    if value and str(value).strip() and not _same_header(value, exclude):
        return value

    for header in headers or []:
        if not header or _same_header(header, exclude):
            continue
        if any(word in str(header).casefold() for word in _START_WORDS):
            return header

    return None
```

- [ ] **Step 6: Run the test to verify it passes**

```bash
cd /d/TMC/MigrationBot/migrationbot/backend && \
DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/migrationbot_test" REDIS_URL="redis://localhost:6379" DEEPSEEK_API_KEY=mock JWT_SECRET=test-secret-32-characters-long-xx CORS_ORIGINS="http://localhost:3000" ADMIN_EMAILS="a@example.com" DEFAULT_SPREADSHEET_ID=test \
python -m pytest tests/test_core/test_start_column.py -q
```

Expected: `13 passed`.

- [ ] **Step 7: Run the whole local suite and lint**

```bash
cd /d/TMC/MigrationBot/migrationbot/backend && \
DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/migrationbot_test" REDIS_URL="redis://localhost:6379" DEEPSEEK_API_KEY=mock JWT_SECRET=test-secret-32-characters-long-xx CORS_ORIGINS="http://localhost:3000" ADMIN_EMAILS="a@example.com" DEFAULT_SPREADSHEET_ID=test \
python -m pytest tests/test_core tests/test_sheets -q && cd .. && ruff check backend/app
```

Expected: `395 passed` (382 baseline + 13), and `All checks passed!`.

- [ ] **Step 8: Commit**

```bash
cd /d/TMC/MigrationBot/migrationbot && git add backend/app/core/schema.py backend/tests/test_core/test_start_column.py && git commit -m "feat(schema): resolve a tab's start-date column without the null-default trap

get_due_column's mirror. Reads date_columns.start with a truthiness test rather than
dict.get(key, default), because detection writes the key holding a literal null on the
LLM path and a stored None beats the default — the §16.6 defect, on the other end of the
span. Refuses the already-resolved due column, since _DUE_WORDS and _START_WORDS overlap
on 'Planned Start' and a zero-length bar reads as data rather than as a mapping gap.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 2: Row classification and the shared row-number key

**Files:**
- Modify: `backend/app/core/schema.py` (add `ROW_NUMBER_KEY`)
- Modify: `backend/app/api/dashboard.py:86-90` (import the constant instead of defining it)
- Create: `backend/app/core/timeline.py`
- Test: `backend/tests/test_core/test_timeline.py` (create)

**Interfaces:**
- Consumes: `get_start_column` (Task 1), `parse_date` (`core/people.py`), `is_finished_status` (`core/overdue.py`), `resolve_header` (`core/schema.py`)
- Produces:
  - `ROW_NUMBER_KEY: str = "__row_number__"` in `core/schema.py`
  - `classify_row(row: Dict[str, str], start_header: Optional[str], due_header: Optional[str]) -> Tuple[str, Optional[date], Optional[date]]` where the first element is one of `"bar" | "milestone" | "undated" | "unparsed"`. Used by Task 3.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_core/test_timeline.py`:

```python
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

import pytest

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
    kind, _, _ = classify_row(_row("17/0/2026", "09/02/2026"), START, DUE)
    assert kind == "unparsed"


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
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
cd /d/TMC/MigrationBot/migrationbot/backend && \
DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/migrationbot_test" REDIS_URL="redis://localhost:6379" DEEPSEEK_API_KEY=mock JWT_SECRET=test-secret-32-characters-long-xx CORS_ORIGINS="http://localhost:3000" ADMIN_EMAILS="a@example.com" DEFAULT_SPREADSHEET_ID=test \
python -m pytest tests/test_core/test_timeline.py -q
```

Expected: `ModuleNotFoundError: No module named 'app.core.timeline'`.

- [ ] **Step 3: Move `ROW_NUMBER_KEY` into `core/schema.py`**

Append to `backend/app/core/schema.py`:

```python
# The sheet row a row dict came from, attached under a key no header can collide with
# (`_row_dicts` skips empty headers, and a real header is never dunder-wrapped). It lives
# here rather than in api/dashboard.py because core/timeline.py reads it too, and two
# copies of a magic string are how the grid and the timeline end up disagreeing about
# which row the user clicked — which for a duplicated ID is the difference between an
# edit that lands and an edit that lands somewhere else (§16.7).
ROW_NUMBER_KEY = "__row_number__"
```

In `backend/app/api/dashboard.py`, delete the local definition at lines 86-90 and add `ROW_NUMBER_KEY` to the existing `from app.core.schema import (...)` block. Then replace every remaining `_ROW_NUMBER_KEY` reference with `ROW_NUMBER_KEY`:

```bash
cd /d/TMC/MigrationBot/migrationbot/backend && grep -n "_ROW_NUMBER_KEY" app/api/dashboard.py
```

Every hit must become `ROW_NUMBER_KEY` (note: `ROW_NUMBER_KEY` is a suffix of `_ROW_NUMBER_KEY`, so use a word-boundary replacement, not a bare substring one):

```bash
cd /d/TMC/MigrationBot/migrationbot/backend && python - <<'PY'
import pathlib, re
p = pathlib.Path("app/api/dashboard.py")
s = p.read_text(encoding="utf-8")
s = re.sub(r"\b_ROW_NUMBER_KEY\b", "ROW_NUMBER_KEY", s)
p.write_text(s, encoding="utf-8")
print("done")
PY
grep -n "ROW_NUMBER_KEY" app/api/dashboard.py
```

- [ ] **Step 4: Create `core/timeline.py` with `classify_row`**

Create `backend/app/core/timeline.py`:

```python
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

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from app.core.overdue import is_finished_status
from app.core.people import parse_date, split_cell
from app.core.schema import (
    ROW_NUMBER_KEY,
    bind_columns,
    get_due_column,
    get_people_columns,
    get_start_column,
    resolve_header,
)

# Matches ToolResultCard's threshold for switching a bar chart to a table (§14.2): past a
# dozen categories the reader is scanning a list, not comparing magnitudes.
MAX_GROUPS = 12

OTHER_GROUP_LABEL = "Other"
UNGROUPED_LABEL = "All rows"
BLANK_GROUP_LABEL = "(blank)"
UNASSIGNED_GROUP_LABEL = "Unassigned"


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
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
cd /d/TMC/MigrationBot/migrationbot/backend && \
DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/migrationbot_test" REDIS_URL="redis://localhost:6379" DEEPSEEK_API_KEY=mock JWT_SECRET=test-secret-32-characters-long-xx CORS_ORIGINS="http://localhost:3000" ADMIN_EMAILS="a@example.com" DEFAULT_SPREADSHEET_ID=test \
python -m pytest tests/test_core/test_timeline.py -q
```

Expected: `11 passed`.

- [ ] **Step 6: Run the whole local suite and lint**

The `ROW_NUMBER_KEY` move touches the dashboard, so the existing dashboard tests are the regression check here.

```bash
cd /d/TMC/MigrationBot/migrationbot/backend && \
DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/migrationbot_test" REDIS_URL="redis://localhost:6379" DEEPSEEK_API_KEY=mock JWT_SECRET=test-secret-32-characters-long-xx CORS_ORIGINS="http://localhost:3000" ADMIN_EMAILS="a@example.com" DEFAULT_SPREADSHEET_ID=test \
python -m pytest tests/test_core tests/test_sheets -q && cd .. && ruff check backend/app
```

Expected: `406 passed` (395 + 11), `All checks passed!`. Unused imports in `timeline.py` are flagged by ruff's `F` rules — they are consumed in Task 3, so if ruff complains at this point, remove the not-yet-used imports and re-add them in Task 3.

- [ ] **Step 7: Commit**

```bash
cd /d/TMC/MigrationBot/migrationbot && git add backend/app/core/timeline.py backend/app/core/schema.py backend/app/api/dashboard.py backend/tests/test_core/test_timeline.py && git commit -m "feat(timeline): classify every row into exactly one of four dated states

bar / milestone / undated / unparsed, summing to the row total so nothing can vanish from
a chart without a count moving. unparsed is kept distinct from undated for the reason
core/health.py separates unreadable_date from no_deadline: '17/0/2026' is present on the
reference sheet, looks filled in, and evaluates to nothing.

ROW_NUMBER_KEY moves to core/schema.py so the grid and the timeline cannot disagree about
which physical row a click meant (§16.7).

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 3: Grouping, rollup and `build_timeline`

**Files:**
- Modify: `backend/app/core/timeline.py`
- Test: `backend/tests/test_core/test_timeline.py` (append)

**Interfaces:**
- Consumes: `classify_row` (Task 2), `PersonResolver.resolve_cell(raw) -> List[str]` (`core/aliases.py`)
- Produces: `build_timeline(headers, rows, tab_schema, *, group_by=None, resolver=None, today=None) -> Dict[str, Any]` — used by Task 4 and consumed by Tasks 6–7. Full response shape is in Step 3 below.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_core/test_timeline.py`:

```python
# ---------------------------------------------------------------------------
# build_timeline
# ---------------------------------------------------------------------------

from app.core.aliases import PersonResolver
from app.core.timeline import MAX_GROUPS, OTHER_GROUP_LABEL, UNGROUPED_LABEL, build_timeline

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
    rows = [_tracker_row("W-3"), _tracker_row("W-4", due="17/0/2026")]
    result = _build(rows)
    assert result["counts"]["total"] == 2
    assert sum(len(g["items"]) for g in result["groups"]) == 0


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
    rows = [
        _tracker_row(f"W-{i}", module=f"M{i:02d}", start="01/02/2026", due="09/02/2026")
        for i in range(MAX_GROUPS + 5)
    ]
    labels = [g["label"] for g in _build(rows)["groups"]]
    assert len(labels) == MAX_GROUPS + 1
    assert labels[-1] == OTHER_GROUP_LABEL


# --- rollup -------------------------------------------------------------------

def test_a_group_header_spans_the_min_and_max_of_its_children():
    rows = [
        _tracker_row("W-1", start="01/02/2026", due="09/02/2026"),
        _tracker_row("W-2", start="05/02/2026", due="23/02/2026"),
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
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /d/TMC/MigrationBot/migrationbot/backend && \
DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/migrationbot_test" REDIS_URL="redis://localhost:6379" DEEPSEEK_API_KEY=mock JWT_SECRET=test-secret-32-characters-long-xx CORS_ORIGINS="http://localhost:3000" ADMIN_EMAILS="a@example.com" DEFAULT_SPREADSHEET_ID=test \
python -m pytest tests/test_core/test_timeline.py -q
```

Expected: `ImportError: cannot import name 'build_timeline' from 'app.core.timeline'`.

- [ ] **Step 3: Implement `build_timeline`**

Append to `backend/app/core/timeline.py`:

```python
def _iso(value: Optional[date]) -> Optional[str]:
    return value.isoformat() if value else None


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
) -> List[str]:
    """Columns worth offering as a grouping.

    A free-text description column technically groups; it produces one group per row and
    a chart nobody can read. Cardinality decides, except for people columns, which are
    always offered — a 40-person roster is exactly the grouping someone wants even though
    it exceeds the cap and spills into `Other`.
    """
    out: List[str] = []
    for header in headers:
        if not header:
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
) -> Dict[str, Any]:
    """Everything a timeline panel needs, computed from the tab's own vocabulary.

    `today` is a parameter rather than `date.today()` so a timeline is reproducible; one
    that cannot be regenerated identically cannot be checked against what a user saw.
    `core/digest.py:build_digest` takes it for the same reason.

    The due column is resolved first and handed to `get_start_column` as an exclusion,
    because the two word lists overlap and a single-date tab would otherwise resolve one
    header to both ends of every bar.
    """
    today = today or date.today()

    due_header = resolve_header(headers, get_due_column(tab_schema, headers))
    start_header = resolve_header(
        headers, get_start_column(tab_schema, headers, exclude=due_header)
    )

    id_header = resolve_header(headers, tab_schema.get("primary_id_column"))
    desc_header = resolve_header(headers, tab_schema.get("description_column"))
    status_header = resolve_header(headers, tab_schema.get("status_column"))
    people = bind_columns(get_people_columns(tab_schema), headers)
    people_headers = [p["header"] for p in people]

    # Which cells the row dialog may edit. Dates and status join the people columns here;
    # RBAC still gates every one of them field-by-field at dispatch (§6.2), so widening
    # the affordance cannot widen anyone's permissions.
    editable = [h for h in ([start_header, due_header, status_header] + people_headers) if h]

    group_header = resolve_header(
        headers, group_by if group_by else tab_schema.get("module_column")
    )
    is_person_group = group_header in people_headers if group_header else False

    counts = {"total": len(rows), "charted": 0, "milestone_only": 0, "undated": 0, "unparsed": 0}
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    bucket_counts: Dict[str, int] = {}

    for row in rows:
        kind, start, end = classify_row(row, start_header, due_header)

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
            continue

        reversed_dates = bool(start and end and end < start)
        if reversed_dates:
            start, end = end, start

        status = str(row.get(status_header, "")) if status_header else ""
        milestone_of = None
        if kind == "milestone":
            milestone_of = "start" if start_header and str(row.get(start_header, "")).strip() else "due"

        item = {
            "id": str(row.get(id_header, "")) if id_header else "",
            "label": (str(row.get(desc_header, "")).strip() if desc_header else "")
            or (str(row.get(id_header, "")) if id_header else ""),
            "row_number": row.get(ROW_NUMBER_KEY),
            "kind": kind,
            "start": _iso(start),
            "end": _iso(end),
            "milestone_of": milestone_of,
            "reversed": reversed_dates,
            "status": status,
            # Overdue only ever describes a real deadline, so a start-only milestone is
            # never overdue however old it is — nothing was promised for that date.
            "overdue": bool(
                end
                and (kind == "bar" or milestone_of == "due")
                and end < today
                and not is_finished_status(status)
            ),
            "people": [
                {"key": p["key"], "label": p["label"], "header": p["header"],
                 "value": str(row.get(p["header"], ""))}
                for p in people
            ],
            # Raw cell text, not the parsed ISO value. The dialog writes back exactly what
            # it was given unless the user edits it, so a sheet spelling dates "10.05.2026"
            # is not silently rewritten into ISO on every save.
            "values": {h: str(row.get(h, "")) for h in editable},
        }
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
        head.append({
            "label": OTHER_GROUP_LABEL,
            "count": sum(g["count"] for g in tail),
            "start": min(tail_starts) if tail_starts else None,
            "end": max(tail_ends) if tail_ends else None,
            "items": [i for g in tail for i in g["items"]],
            "collapsed_groups": len(tail),
        })
        groups = head

    all_starts = [i["start"] for g in groups for i in g["items"] if i["start"]]
    all_ends = [i["end"] for g in groups for i in g["items"] if i["end"]]

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
        "group_by": group_header,
        "groupable": _groupable_headers(headers, rows, people_headers),
        "editable_headers": editable,
        "people_columns": people,
        "range_start": min(all_starts) if all_starts else None,
        "range_end": max(all_ends) if all_ends else None,
        "today": today.isoformat(),
        "groups": groups,
        "counts": counts,
        "reason": reason,
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /d/TMC/MigrationBot/migrationbot/backend && \
DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/migrationbot_test" REDIS_URL="redis://localhost:6379" DEEPSEEK_API_KEY=mock JWT_SECRET=test-secret-32-characters-long-xx CORS_ORIGINS="http://localhost:3000" ADMIN_EMAILS="a@example.com" DEFAULT_SPREADSHEET_ID=test \
python -m pytest tests/test_core/test_timeline.py -q
```

Expected: `37 passed` (11 from Task 2 + 26 here).

- [ ] **Step 5: Run the whole local suite and lint**

```bash
cd /d/TMC/MigrationBot/migrationbot/backend && \
DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/migrationbot_test" REDIS_URL="redis://localhost:6379" DEEPSEEK_API_KEY=mock JWT_SECRET=test-secret-32-characters-long-xx CORS_ORIGINS="http://localhost:3000" ADMIN_EMAILS="a@example.com" DEFAULT_SPREADSHEET_ID=test \
python -m pytest tests/test_core tests/test_sheets -q && cd .. && ruff check backend/app
```

Expected: `432 passed` (382 baseline + 13 + 11 + 26), `All checks passed!`.

- [ ] **Step 6: Commit**

```bash
cd /d/TMC/MigrationBot/migrationbot && git add backend/app/core/timeline.py backend/tests/test_core/test_timeline.py && git commit -m "feat(timeline): group rows, roll up group spans, and report coverage honestly

Groups by any schema-declared column, defaulting to module_column when the tab has one
and to a single implicit group when it does not — no inline 'Module' literal, which §16.3
already lists as debt. A people column splits and aliases through PersonResolver so the
timeline and the Workload panel cannot disagree; every other column is taken verbatim.

Group spans are derived per read and never written back. A tab with no date column, or
none that parses, returns a reason instead of an empty chart that reads as good news.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 4: The endpoint

**Files:**
- Create: `backend/app/api/timeline.py`
- Modify: `backend/app/main.py` (import at line 16, mount after line 93)
- Test: `backend/tests/test_core/test_timeline_api.py` (create)

**Interfaces:**
- Consumes: `build_timeline` (Task 3); `_resolve_project`, `_tab_for`, `_row_dicts`, `_resolver_for`, `_filter_rows` from `app.api.dashboard`
- Produces: `GET /api/projects/{project_id}/timeline` returning `build_timeline`'s dict plus `tab`, `tabs`, `project_name`, `primary_id_column`, `truncated`. Consumed by Task 7.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_core/test_timeline_api.py`:

```python
"""The timeline endpoint.

Composition only — the arithmetic is tested in test_timeline.py. What matters here is
that this route inherits the read pipeline rather than reimplementing it:

  * the same visibility rule as every other project read, which answers 404 and not 403,
    because confirming a project exists is itself a disclosure;
  * `_filter_rows`, so the chart cannot drift from the grid beside it — the reason that
    helper was extracted for the CSV export in the first place;
  * `_row_dicts` **with** data_start_row, unlike api/digest.py, so each row carries its
    sheet row number and a bar can open an editable row on a tracker with duplicated IDs.

TestClient is constructed without its context manager, matching test_webhooks.py, so
FastAPI's lifespan — and therefore init_db()'s connection attempt — never runs.
"""

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.deps import get_current_user, get_google_auth
from app.db.engine import get_db
from app.main import app

ENDPOINT = "/api/projects/1/timeline"

HEADERS = ["WRICEF No.", "Description", "Module", "Dev Status", "Start Date",
           "Expected Completetion Date", "Developer Name"]

MATRIX_ROWS = [
    ["W-1", "Migrate BOM report", "SD", "In Progress", "01/02/2026", "09/02/2026", "Sara Iqbal"],
    ["W-2", "Rebuild pricing exit", "MM", "Not Started", "", "28/02/2026", "Ahmed Qamar"],
    ["W-3", "Archive old IDocs", "SD", "Completed", "", "", ""],
]

PROJECT = SimpleNamespace(
    id=1,
    project_name="HEDP Tracker",
    spreadsheet_id="sheet-123",
    default_tab="SD",
    schema_config={
        "primary_id_column": "WRICEF No.",
        "description_column": "Description",
        "module_column": "Module",
        "status_column": "Dev Status",
        "date_columns": {"start": "Start Date", "due": "Expected Completetion Date"},
        "people_columns": [{"key": "dev", "label": "Developer", "header": "Developer Name"}],
        "data_start_row": 3,
    },
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def _auth():
    """The route's three injected dependencies, faked. `_resolve_project` is patched per
    test, so the database handle is never actually used."""
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, email="u@x.com")
    app.dependency_overrides[get_google_auth] = lambda: {"access_token": "t", "refresh_token": None}
    app.dependency_overrides[get_db] = lambda: MagicMock()
    yield
    app.dependency_overrides.clear()


def _patches(project=PROJECT, matrix=(HEADERS, MATRIX_ROWS, False)):
    """Everything the handler reaches out to, replaced. Note `_filter_rows` is patched to
    the identity so its own tests stay the single source of truth for filtering."""
    return (
        patch("app.api.timeline._resolve_project", AsyncMock(return_value=project)),
        patch("app.api.timeline.build_sheets_service", MagicMock()),
        patch("app.api.timeline.get_tab_matrix", AsyncMock(return_value=matrix)),
        patch("app.api.timeline._resolver_for", AsyncMock(return_value=None)),
        patch("app.api.timeline._filter_rows",
              AsyncMock(side_effect=lambda db, pid, rows, *a, **k: rows)),
    )


def _get(url=ENDPOINT, **kwargs):
    ps = _patches(**kwargs)
    for p in ps:
        p.start()
    try:
        return client.get(url)
    finally:
        for p in ps:
            p.stop()


# --- the happy path ------------------------------------------------------------

def test_it_returns_groups_and_counts_for_a_tab():
    body = _get().json()
    assert body["counts"]["total"] == 3
    assert body["counts"]["charted"] == 1
    assert body["counts"]["milestone_only"] == 1
    assert body["counts"]["undated"] == 1
    assert {g["label"] for g in body["groups"]} == {"SD", "MM"}


def test_it_reports_the_project_and_tab_it_read():
    body = _get().json()
    assert body["tab"] == "SD"
    assert body["project_name"] == "HEDP Tracker"
    assert body["primary_id_column"] == "WRICEF No."


def test_it_names_the_columns_it_resolved():
    """"No bars" has two very different causes — no dates, or the wrong column — and the
    reader cannot tell them apart without this."""
    body = _get().json()
    assert body["start_header"] == "Start Date"
    assert body["due_header"] == "Expected Completetion Date"


def test_rows_carry_their_sheet_row_number():
    """api/digest.py omits data_start_row here; this route must not, or a bar cannot open
    an editable row on a tracker whose IDs repeat (§16.7)."""
    body = _get().json()
    numbers = [i["row_number"] for g in body["groups"] for i in g["items"]]
    assert 3 in numbers


def test_truncation_is_passed_through_rather_than_swallowed():
    body = _get(matrix=(HEADERS, MATRIX_ROWS, True)).json()
    assert body["truncated"] is True


# --- gating ---------------------------------------------------------------------

def test_a_project_the_caller_cannot_see_is_404_not_403():
    with patch("app.api.timeline._resolve_project",
               AsyncMock(side_effect=HTTPException(status_code=404, detail="Project not found"))):
        response = client.get(ENDPOINT)
    assert response.status_code == 404


# --- filtering ------------------------------------------------------------------

def test_the_grid_filters_are_forwarded_verbatim():
    """Reusing _filter_rows is what stops the chart and the grid disagreeing; this asserts
    the parameters actually arrive."""
    spy = AsyncMock(side_effect=lambda db, pid, rows, *a, **k: rows)
    with patch("app.api.timeline._resolve_project", AsyncMock(return_value=PROJECT)), \
         patch("app.api.timeline.build_sheets_service", MagicMock()), \
         patch("app.api.timeline.get_tab_matrix", AsyncMock(return_value=(HEADERS, MATRIX_ROWS, False))), \
         patch("app.api.timeline._resolver_for", AsyncMock(return_value=None)), \
         patch("app.api.timeline._filter_rows", spy):
        client.get(ENDPOINT + "?q=BOM&person=Sara%20Iqbal&overdue=true&role_key=dev&status=Open")

    kwargs = spy.await_args.kwargs
    assert kwargs["q"] == "BOM"
    assert kwargs["person"] == "Sara Iqbal"
    assert kwargs["overdue"] is True
    assert kwargs["role_key"] == "dev"
    assert kwargs["status"] == "Open"


def test_group_by_is_honoured():
    body = _get(url=ENDPOINT + "?group_by=Dev%20Status").json()
    assert body["group_by"] == "Dev Status"
    assert {g["label"] for g in body["groups"]} == {"In Progress", "Not Started", "Completed"}
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /d/TMC/MigrationBot/migrationbot/backend && \
DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/migrationbot_test" REDIS_URL="redis://localhost:6379" DEEPSEEK_API_KEY=mock JWT_SECRET=test-secret-32-characters-long-xx CORS_ORIGINS="http://localhost:3000" ADMIN_EMAILS="a@example.com" DEFAULT_SPREADSHEET_ID=test \
python -m pytest tests/test_core/test_timeline_api.py -q
```

Expected: `ModuleNotFoundError: No module named 'app.api.timeline'`.

- [ ] **Step 3: Create the router**

Create `backend/app/api/timeline.py`:

```python
"""The timeline read endpoint.

Separate from api/dashboard.py, which is already 687 lines — the same reason api/aliases.py
gives for not being a fourth resource inside api/admin.py, and the reason api/digest.py is
its own file. Everything here is composition: the computation lives in core/timeline.py,
which is pure and testable without a database, a Sheets client or a network.

Three details in the pipeline below are load-bearing:

* **`_filter_rows` is reused, never reimplemented.** It was extracted so the CSV export
  could not drift from the grid; a timeline that filtered differently from the grid beside
  it would be that defect a second time.
* **`_row_dicts` is called with `data_start_row`**, unlike api/digest.py, so every row
  carries its sheet row number. Without it a bar cannot open an editable row on a tracker
  whose IDs repeat — 27 of 412 rows on the reference sheet share one (§16.7).
* **No `limit` or `offset`.** The response is the whole filtered set, for the reason
  rows.csv has no paging: a chart that omits rows the count above it claims is the one
  thing a chart must not do.
"""

import logging
from datetime import date
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dashboard import _filter_rows, _resolve_project, _resolver_for, _row_dicts, _tab_for
from app.core.schema import get_available_tabs, get_tab_schema
from app.core.timeline import build_timeline
from app.db.engine import get_db
from app.deps import get_current_user, get_google_auth
from app.models.user import User
from app.sheets.client import build_sheets_service
from app.sheets.rows_cache import get_tab_matrix

logger = logging.getLogger("timeline")

router = APIRouter()


@router.get("/projects/{project_id}/timeline", response_model=Dict[str, Any])
async def project_timeline(
    project_id: int,
    tab: Optional[str] = None,
    q: Optional[str] = None,
    status: Optional[str] = None,
    person: Optional[str] = None,
    role_key: Optional[str] = None,
    overdue: bool = False,
    group_by: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    google_auth: dict = Depends(get_google_auth),
) -> Dict[str, Any]:
    """The tab's rows placed on a time axis, grouped, with the coverage that produced them.

    Gated as the other project reads are: `_resolve_project` answers 404 rather than 403,
    because telling an unauthorised caller that a project exists is itself a disclosure.
    """
    project = await _resolve_project(db, current_user, project_id)
    active_tab = _tab_for(project, tab)
    tab_schema = get_tab_schema(project.schema_config or {}, active_tab)
    data_start_row = tab_schema.get("data_start_row", 3)

    service = build_sheets_service(
        access_token=google_auth["access_token"],
        refresh_token=google_auth.get("refresh_token"),
    )
    headers, raw_rows, truncated = await get_tab_matrix(
        service, project.spreadsheet_id, active_tab, data_start_row
    )
    rows = _row_dicts(headers, raw_rows, data_start_row)
    resolver = await _resolver_for(db, project_id)

    filtered = await _filter_rows(
        db, project_id, rows, headers, tab_schema,
        q=q, status=status, person=person, role_key=role_key, overdue=overdue,
    )

    result = build_timeline(
        headers, filtered, tab_schema,
        group_by=group_by, resolver=resolver, today=date.today(),
    )
    result["tab"] = active_tab
    result["tabs"] = get_available_tabs(project.schema_config or {})
    result["project_name"] = project.project_name
    result["primary_id_column"] = tab_schema.get("primary_id_column")
    # Surfaced, not swallowed: past _MAX_SCAN_ROWS every count above is a floor.
    result["truncated"] = truncated
    return result
```

- [ ] **Step 4: Mount the router**

In `backend/app/main.py`, add after line 16 (`from app.api.watch import router as watch_router`):

```python
from app.api.timeline import router as timeline_router
```

and after line 93 (`app.include_router(watch_router, prefix="/api")`):

```python
app.include_router(timeline_router, prefix="/api")
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
cd /d/TMC/MigrationBot/migrationbot/backend && \
DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/migrationbot_test" REDIS_URL="redis://localhost:6379" DEEPSEEK_API_KEY=mock JWT_SECRET=test-secret-32-characters-long-xx CORS_ORIGINS="http://localhost:3000" ADMIN_EMAILS="a@example.com" DEFAULT_SPREADSHEET_ID=test \
python -m pytest tests/test_core/test_timeline_api.py -q
```

Expected: `8 passed`.

- [ ] **Step 6: Run the whole local suite and lint**

```bash
cd /d/TMC/MigrationBot/migrationbot/backend && \
DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/migrationbot_test" REDIS_URL="redis://localhost:6379" DEEPSEEK_API_KEY=mock JWT_SECRET=test-secret-32-characters-long-xx CORS_ORIGINS="http://localhost:3000" ADMIN_EMAILS="a@example.com" DEFAULT_SPREADSHEET_ID=test \
python -m pytest tests/test_core tests/test_sheets -q && cd .. && ruff check backend/app
```

Expected: `440 passed`, `All checks passed!`.

- [ ] **Step 7: Commit**

```bash
cd /d/TMC/MigrationBot/migrationbot && git add backend/app/api/timeline.py backend/app/main.py backend/tests/test_core/test_timeline_api.py && git commit -m "feat(api): GET /projects/{id}/timeline

Its own router rather than a sixth read endpoint in dashboard.py, which is already 687
lines — the split api/aliases.py and api/digest.py already established.

Reuses _filter_rows so the chart cannot drift from the grid, and calls _row_dicts WITH
data_start_row (unlike api/digest.py) so each bar carries the physical row number a
duplicated ID needs to be editable. No limit/offset: the whole filtered set, for the same
reason rows.csv has no paging.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 5: Extract the inline-edit machinery (pure refactor, no behaviour change)

**Files:**
- Create: `frontend/src/hooks/useRowEdits.ts`
- Modify: `frontend/src/components/DataDisplay.tsx` (add `EditableCell`)
- Modify: `frontend/src/app/project/[id]/page.tsx` (consume both)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `useRowEdits({ projectId, tab, authHeaders, apiToken, onApplied }) -> { pending, editing, setEditing, saveCell, editKey }`
  - `PendingEdit` interface, exported from the hook
  - `EditableCell` component in `DataDisplay.tsx`

  Both are consumed by Task 7.

**Why this is its own task.** It changes no behaviour and adds no feature, so a reviewer can accept or reject it independently of the timeline. It exists because the editable cell is currently a 62-line closure at `page.tsx:644-705` entangled with four separate pieces of state; reusing it from a dialog without extraction means a second copy of the queued-state machine, and a second copy will drift.

- [ ] **Step 1: Create the hook**

Create `frontend/src/hooks/useRowEdits.ts`:

```ts
"use client"

import { useCallback, useEffect, useState } from "react"

/** An edit that has been queued but not yet confirmed applied by the worker, keyed
 *  `${rowId}::${header}`. Writes are eventually consistent (the worker applies at
 *  1 req/sec), so the cell has to show its own pending state or the edit looks lost. */
export interface PendingEdit {
  jobId: string
  value: string
  state: "queued" | "failed"
  error?: string
}

export function editKey(rowId: string, header: string): string {
  return `${rowId}::${header}`
}

/**
 * One queued-write state machine, shared by every surface that edits a cell.
 *
 * Lifted verbatim out of project/[id]/page.tsx when the timeline panel needed the same
 * cells inside a dialog. Two implementations of this would drift — the argument
 * DataTable's `renderCell` prop already made when the dashboard wanted editable cells
 * rather than its own table.
 *
 * Three resolution paths, and all three are needed:
 *   - the `queue_update` frame the worker publishes on a terminal state, bridged to a DOM
 *     CustomEvent by useWebSocket;
 *   - a GET /api/jobs/{id} poll 8s later, for when that frame was missed (a reload, a
 *     reconnect, a worker restart), which otherwise strands a cell on "queued" forever;
 *   - an immediate local failure, for a 403 or an unreachable server.
 */
export function useRowEdits({
  projectId,
  tab,
  authHeaders,
  apiToken,
  onApplied,
}: {
  projectId: string
  tab?: string
  authHeaders: Record<string, string>
  apiToken?: string
  /** Called when a write has actually landed, so the caller can re-read. */
  onApplied: () => void
}) {
  const [pending, setPending] = useState<Record<string, PendingEdit>>({})
  const [editing, setEditing] = useState<string | null>(null)

  const saveCell = useCallback(
    async (rowId: string, header: string, value: string, rowNumber?: number) => {
      const key = editKey(rowId, header)
      setEditing(null)
      try {
        const res = await fetch(`/api/projects/${projectId}/rows/${encodeURIComponent(rowId)}`, {
          method: "PATCH",
          headers: { ...authHeaders, "Content-Type": "application/json" },
          // `row_number` names the row actually on screen. IDs are not unique on every
          // tracker — 27 of 412 rows on the reference sheet share one — so without it the
          // server can only refuse the edit rather than guess which row was clicked.
          body: JSON.stringify({
            tab,
            updates: [{ field: header, value }],
            row_number: rowNumber,
          }),
        })
        const body = await res.json().catch(() => ({}))
        if (!res.ok) {
          // A 403 here is the RBAC checker refusing the write; show its own wording
          // rather than a generic failure, since it explains what to ask an admin for.
          setPending((prev) => ({
            ...prev,
            [key]: { jobId: "", value, state: "failed", error: body.detail || `Refused (${res.status}).` },
          }))
          return
        }
        setPending((prev) => ({ ...prev, [key]: { jobId: body.job_id, value, state: "queued" } }))
      } catch {
        setPending((prev) => ({
          ...prev,
          [key]: { jobId: "", value, state: "failed", error: "Could not reach the server." },
        }))
      }
    },
    [projectId, authHeaders, tab]
  )

  // Reassignments land through the queue, so the terminal frame is the cue to re-read.
  // Reusing the existing queue_update -> CustomEvent bridge means the page reflects an
  // edit made from chat, too.
  useEffect(() => {
    const onQueueUpdate = (event: Event) => {
      const detail = (event as CustomEvent).detail as
        | { job_id?: string; status?: string; error?: string }
        | undefined

      if (detail?.job_id) {
        setPending((prev) => {
          const key = Object.keys(prev).find((k) => prev[k].jobId === detail.job_id)
          if (!key) return prev
          const next = { ...prev }
          if (detail.status === "completed") {
            // Applied: drop the pending marker and let the reload below show the real
            // cell. Keeping it would leave the UI asserting a value the sheet may have
            // normalised differently.
            delete next[key]
          } else {
            next[key] = { ...next[key], state: "failed", error: detail.error }
          }
          return next
        })
      }
      if (detail?.status === "completed") onApplied()
    }
    window.addEventListener("queue_update", onQueueUpdate)
    return () => window.removeEventListener("queue_update", onQueueUpdate)
  }, [onApplied])

  // A terminal frame can be missed — a reload, a reconnect, a worker restart — which
  // would strand a cell on "queued" forever. GET /api/jobs/{id} is the authoritative
  // fallback for exactly that.
  useEffect(() => {
    const stuck = Object.entries(pending).filter(([, p]) => p.state === "queued")
    if (stuck.length === 0 || !apiToken) return

    const timer = setTimeout(async () => {
      for (const [key, entry] of stuck) {
        try {
          const res = await fetch(`/api/jobs/${entry.jobId}`, { headers: authHeaders })
          if (!res.ok) continue
          const job = await res.json()
          if (job.status === "done") {
            setPending((prev) => {
              const next = { ...prev }
              delete next[key]
              return next
            })
            onApplied()
          } else if (job.status === "error" || job.status === "dead_letter") {
            setPending((prev) => ({
              ...prev,
              [key]: { ...prev[key], state: "failed", error: job.error },
            }))
          }
        } catch {
          // Reconciliation is best-effort; the next poll or a manual refresh will catch it.
        }
      }
    }, 8000)
    return () => clearTimeout(timer)
  }, [pending, apiToken, authHeaders, onApplied])

  return { pending, editing, setEditing, saveCell, editKey }
}
```

- [ ] **Step 2: Add `EditableCell` to `DataDisplay.tsx`**

Append to `frontend/src/components/DataDisplay.tsx` (and add `Pencil` to its imports with `import { Pencil } from "lucide-react"` at the top, after the `"use client"` directive):

```tsx
/**
 * One editable sheet cell, with its own queued/failed state.
 *
 * Extracted from the dashboard grid's `renderCell` closure so the timeline's row dialog
 * shows the identical control rather than a second implementation of it. The dotted
 * underline is the affordance and is not decorative: without it an editable cell is
 * visually identical to a read-only one, so nobody discovers the feature.
 */
export function EditableCell({
  label,
  value,
  edit,
  isEditing,
  onBeginEdit,
  onCancel,
  onSave,
  placeholder = "Unassigned",
}: {
  /** The column's display label, used in the accessible name and the hover title. */
  label: string
  value: string
  edit?: { value: string; state: "queued" | "failed"; error?: string }
  isEditing: boolean
  onBeginEdit: () => void
  onCancel: () => void
  onSave: (next: string) => void
  placeholder?: string
}) {
  if (isEditing) {
    return (
      <input
        autoFocus
        defaultValue={edit ? edit.value : value}
        aria-label={label}
        onBlur={(e) => onSave(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.target as HTMLInputElement).blur()
          if (e.key === "Escape") onCancel()
        }}
        className="w-full rounded border border-brass-500 bg-ink-950 px-1.5 py-0.5 text-[12.5px] text-ink-100 focus:outline-none"
      />
    )
  }

  return (
    <button
      onClick={onBeginEdit}
      title={edit?.error || `Edit ${label}`}
      className="group flex w-full cursor-pointer items-center gap-1 text-left"
    >
      <span
        className={`decoration-dotted underline-offset-4 group-hover:text-brass-300 group-hover:underline ${
          edit?.state === "failed" ? "text-failed" : "underline decoration-ink-600"
        }`}
      >
        {edit ? edit.value : value || <span className="text-ink-600">{placeholder}</span>}
      </span>
      <Pencil className="h-3 w-3 shrink-0 text-ink-600 opacity-0 transition group-hover:opacity-100" />
      {edit?.state === "queued" && <span className="status status-queued ml-1">queued</span>}
      {edit?.state === "failed" && <span className="status status-failed ml-1">failed</span>}
    </button>
  )
}
```

- [ ] **Step 3: Consume both in `page.tsx`**

Three edits, all in `frontend/src/app/project/[id]/page.tsx`:

**(a)** Delete the `PendingEdit` interface (lines 65-73). Add to the imports:

```tsx
import { useRowEdits } from "@/hooks/useRowEdits"
```

and add `EditableCell` to the existing `@/components/DataDisplay` import list.

**(b)** Delete the `pending` and `editing` `useState` declarations (lines 133-134), the `saveCell` callback (lines 336-372), the `queue_update` effect (lines 274-300) and the job-poll effect (lines 302-334). Replace them with, placed immediately after the `load` callback is defined:

```tsx
  const { pending, editing, setEditing, saveCell } = useRowEdits({
    projectId,
    tab: rowsData?.tab,
    authHeaders,
    apiToken,
    onApplied: load,
  })
```

**(c)** Replace the body of the `renderCell` prop (lines 644-705) with:

```tsx
                  renderCell={(label, value, rowIndex) => {
                    const header = headerByLabel[label]
                    const isPerson = personHeaders.has(header)
                    const idHeader = rowsData?.primary_id_column
                    // Editing needs a person column and a way to address the row. Without
                    // an id column the sheet has no stable row key, so the cell stays
                    // read-only rather than guessing at row position, which reorders.
                    if (!isPerson || !idHeader) return undefined

                    const rowId = rowsData?.rows[rowIndex]?.[idHeader]
                    if (!rowId) return undefined

                    // The sheet row this cell was rendered from, so an edit to a
                    // duplicated ID lands on the row the user is looking at.
                    const rowNumber = rowsData?.rows[rowIndex]?.__row_number__
                    const cellId = `${rowId}::${header}::cell`

                    return (
                      <EditableCell
                        label={`${label} for ${rowId}`}
                        value={value}
                        edit={pending[`${rowId}::${header}`]}
                        isEditing={editing === cellId}
                        onBeginEdit={() => setEditing(cellId)}
                        onCancel={() => setEditing(null)}
                        onSave={(next) => saveCell(rowId, header, next, rowNumber)}
                      />
                    )
                  }}
```

- [ ] **Step 4: Verify the frontend compiles, lints and builds**

```bash
cd /d/TMC/MigrationBot/migrationbot/frontend && npx tsc --noEmit && npm run lint && npm run build
```

Expected: `tsc` silent; `npm run lint` reports no *new* errors beyond the ~49 pre-existing ones (`ci.yml` runs lint with `continue-on-error: true` for exactly this reason — compare the count against `main` if unsure); `npm run build` completes with a route list including `/project/[id]`.

- [ ] **Step 5: Confirm no behaviour changed**

This task must be a no-op for the user. Read the diff and confirm every one of these still holds:

```bash
cd /d/TMC/MigrationBot/migrationbot && git diff --stat && git diff frontend/src/app/project/\[id\]/page.tsx | head -120
```

- the PATCH body still carries `tab`, `updates: [{field, value}]` **and `row_number`**;
- a 403 still renders `body.detail`, not a generic message;
- the poll still fires at 8000ms and still calls `load()` on `done`;
- the `queue_update` listener still calls `load()` on `completed`;
- non-person columns still return `undefined` from `renderCell` and stay read-only.

- [ ] **Step 6: Commit**

```bash
cd /d/TMC/MigrationBot/migrationbot && git add frontend/src/hooks/useRowEdits.ts frontend/src/components/DataDisplay.tsx frontend/src/app/project/\[id\]/page.tsx && git commit -m "refactor(dashboard): lift the inline-edit machinery out of the grid closure

No behaviour change. The editable cell was a 62-line closure inside DataTable's renderCell
prop, entangled with pending state, saveCell, the queue_update bridge and the 8s job poll.
The timeline panel needs the same cells inside a dialog, and a second implementation of a
queued-write state machine drifts — the argument renderCell itself already made.

useRowEdits owns the state machine; EditableCell owns the control. page.tsx drops ~110
lines from a 1015-line file.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 6: The `TimelineChart` renderer

**Files:**
- Modify: `frontend/src/components/DataDisplay.tsx`

**Interfaces:**
- Consumes: the `groups` / `range_start` / `range_end` / `today` shape `build_timeline` produces (Task 3).
- Produces: `TimelineChart`, `TimelineGroup`, `TimelineItem` — all exported, consumed by Task 7.

- [ ] **Step 1: Add the types and the chart**

Append to `frontend/src/components/DataDisplay.tsx`. Extend the lucide import to `import { ChevronDown, ChevronRight, Pencil } from "lucide-react"`.

```tsx
export interface TimelineItem {
  id: string
  label: string
  row_number?: number
  kind: "bar" | "milestone"
  start: string | null
  end: string | null
  milestone_of: "start" | "due" | null
  reversed: boolean
  status: string
  overdue: boolean
  people: { key: string; label: string; header: string; value: string }[]
  values: Record<string, string>
}

export interface TimelineGroup {
  label: string
  count: number
  start: string | null
  end: string | null
  items: TimelineItem[]
  collapsed_groups?: number
}

const DAY_MS = 86_400_000
const LABEL_COL = 260

function toUTC(iso: string): number {
  return Date.parse(`${iso}T00:00:00Z`)
}

/** Ticks whose spacing suits the span, rather than a zoom control nobody asked for.
 *  A quarter-long plan wants weeks; a three-year one wants quarters.
 *
 *  Returns absolute timestamps rather than percentages, so the caller positions them with
 *  the same `pct` the bars use. Computing tick positions against their own span is how an
 *  axis ends up drifting a few pixels off the bars it is supposed to label. */
function axisTicks(startMs: number, endMs: number): { ms: number; label: string }[] {
  const days = Math.max(1, Math.round((endMs - startMs) / DAY_MS))
  const step = days <= 45 ? 7 : days <= 200 ? 30 : days <= 800 ? 91 : 365
  const fmt: Intl.DateTimeFormatOptions =
    step <= 7
      ? { day: "2-digit", month: "short" }
      : step <= 30
        ? { month: "short", year: "2-digit" }
        : { year: "numeric" }

  const ticks: { ms: number; label: string }[] = []
  for (let t = startMs; t <= endMs; t += step * DAY_MS) {
    ticks.push({
      ms: t,
      label: new Date(t).toLocaleDateString(undefined, { ...fmt, timeZone: "UTC" }),
    })
  }
  return ticks
}

/**
 * A Gantt-style timeline.
 *
 * Hand-rolled rather than built on Recharts, which has no Gantt primitive — the shapes
 * commonly bolted onto its BarChart to imitate one break as soon as rows are grouped.
 *
 * One hue for every bar. The source template colours each phase differently; that is
 * rejected here because the group header and indentation already encode the grouping, and
 * because the four status colours fail all-pairs CVD separation as a set, which is why
 * they are reserved for icon-plus-label pairings. Overdue is therefore an outline plus a
 * glyph, never a fill: colour alone would reintroduce exactly that problem.
 */
export function TimelineChart({
  groups,
  rangeStart,
  rangeEnd,
  today,
  onSelectItem,
}: {
  groups: TimelineGroup[]
  rangeStart: string
  rangeEnd: string
  today: string
  onSelectItem?: (item: TimelineItem) => void
}) {
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})

  // A single-day span would divide by zero and a one-day plan would render as one pixel,
  // so the axis is padded to a minimum width either way.
  const rawStart = toUTC(rangeStart)
  const rawEnd = toUTC(rangeEnd)
  const pad = Math.max(DAY_MS, (rawEnd - rawStart) * 0.02)
  const startMs = rawStart - pad
  const endMs = rawEnd + pad
  const span = Math.max(1, endMs - startMs)

  const pctFromMs = (ms: number) => ((ms - startMs) / span) * 100
  const pct = (iso: string) => pctFromMs(toUTC(iso))
  const ticks = axisTicks(startMs, endMs)
  const todayPct = pct(today)

  return (
    <div className="rounded-xl border border-[var(--color-rule-strong)] bg-ink-850">
      {/* The time region scrolls inside its own container; the page body never scrolls
          sideways, the rule already applied to markdown tables. */}
      <div className="overflow-x-auto">
        <div className="min-w-[720px]">
          {/* Axis */}
          <div className="sticky top-0 z-20 flex border-b border-[var(--color-rule-strong)] bg-ink-850">
            <div className="shrink-0 px-3 py-2" style={{ width: LABEL_COL }}>
              <span className="label-micro">Task</span>
            </div>
            <div className="relative flex-1 py-2">
              {ticks.map((t) => (
                <span
                  key={t.ms}
                  className="absolute -translate-x-1/2 whitespace-nowrap font-mono text-[10px] text-ink-500"
                  style={{ left: `${pctFromMs(t.ms)}%` }}
                >
                  {t.label}
                </span>
              ))}
            </div>
          </div>

          <div className="relative">
            {/* Today marker, drawn behind the bars and spanning every row. */}
            {todayPct >= 0 && todayPct <= 100 && (
              // Two nested elements, not one calc(): the marker must be a percentage of the
              // TIME region, and `calc(260px + 40% * ...)` cannot express that — mixing % and
              // px that way is invalid CSS and silently drops the whole declaration.
              <div
                className="pointer-events-none absolute inset-y-0 z-10"
                style={{ left: LABEL_COL, right: 0 }}
                aria-hidden
              >
                <div className="absolute inset-y-0 w-px bg-brass-400/60" style={{ left: `${todayPct}%` }} />
              </div>
            )}

            {groups.map((group) => {
              const isCollapsed = collapsed[group.label]
              return (
                <div key={group.label} className="border-b border-[var(--color-rule)] last:border-b-0">
                  {/* Group header — the rollup, derived per read and never written back. */}
                  <button
                    onClick={() => setCollapsed((p) => ({ ...p, [group.label]: !p[group.label] }))}
                    className="flex w-full items-center hover:bg-ink-800/60"
                  >
                    <div
                      className="flex shrink-0 items-center gap-1.5 px-3 py-2 text-left"
                      style={{ width: LABEL_COL }}
                    >
                      {isCollapsed ? (
                        <ChevronRight className="h-3.5 w-3.5 shrink-0 text-ink-500" />
                      ) : (
                        <ChevronDown className="h-3.5 w-3.5 shrink-0 text-ink-500" />
                      )}
                      <span className="truncate text-[12.5px] font-semibold text-ink-200" title={group.label}>
                        {group.label}
                      </span>
                      <span className="shrink-0 font-mono text-[10px] text-ink-500">{group.count}</span>
                    </div>
                    <div className="relative h-8 flex-1">
                      {group.start && group.end && (
                        <div
                          className="absolute top-1/2 h-2.5 -translate-y-1/2 rounded-sm"
                          style={{
                            left: `${pct(group.start)}%`,
                            width: `${Math.max(pct(group.end) - pct(group.start), 0.4)}%`,
                            backgroundColor: SERIES_HUE,
                            opacity: 0.5,
                          }}
                        />
                      )}
                    </div>
                  </button>

                  {!isCollapsed &&
                    group.items.map((item) => {
                      const left = item.start ? pct(item.start) : 0
                      const width = item.start && item.end ? Math.max(pct(item.end) - left, 0.4) : 0
                      return (
                        <button
                          key={`${item.id}-${item.row_number ?? item.label}`}
                          onClick={() => onSelectItem?.(item)}
                          className="flex w-full items-center text-left hover:bg-ink-800/60"
                        >
                          <div className="shrink-0 truncate py-1.5 pl-8 pr-3" style={{ width: LABEL_COL }}>
                            <span className="text-[12px] text-ink-300" title={item.label}>
                              {item.label}
                            </span>
                          </div>
                          <div className="relative h-7 flex-1">
                            {item.kind === "bar" ? (
                              <div
                                className="absolute top-1/2 h-3.5 -translate-y-1/2 rounded-sm"
                                style={{
                                  left: `${left}%`,
                                  width: `${width}%`,
                                  backgroundColor: SERIES_HUE,
                                  outline: item.overdue ? "1px solid var(--color-failed)" : undefined,
                                  outlineOffset: item.overdue ? "1px" : undefined,
                                }}
                                title={`${item.id} · ${item.start} → ${item.end}${
                                  item.overdue ? " · overdue" : ""
                                }${item.reversed ? " · dates are reversed on the sheet" : ""}`}
                              />
                            ) : (
                              <div
                                className="absolute top-1/2 h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rotate-45"
                                style={{
                                  left: `${left}%`,
                                  backgroundColor: SERIES_HUE,
                                  outline: item.overdue ? "1px solid var(--color-failed)" : undefined,
                                  outlineOffset: item.overdue ? "1px" : undefined,
                                }}
                                title={`${item.id} · ${
                                  item.milestone_of === "start" ? "starts" : "due"
                                } ${item.start}${item.overdue ? " · overdue" : ""}`}
                              />
                            )}
                          </div>
                        </button>
                      )
                    })}
                </div>
              )
            })}
          </div>
        </div>
      </div>
    </div>
  )
}
```

Add `useState` to the file's React import — the file currently imports nothing from React, so add at the top, below `"use client"`:

```tsx
import { useState } from "react"
```

- [ ] **Step 2: Verify it compiles, lints and builds**

```bash
cd /d/TMC/MigrationBot/migrationbot/frontend && npx tsc --noEmit && npm run lint && npm run build
```

Expected: `tsc` silent, build succeeds, no new lint errors.

- [ ] **Step 3: Commit**

```bash
cd /d/TMC/MigrationBot/migrationbot && git add frontend/src/components/DataDisplay.tsx && git commit -m "feat(ui): a hand-rolled Gantt renderer beside the shared display primitives

Recharts has no Gantt primitive and the usual BarChart workarounds break on grouped rows,
so this is CSS positioning against a shared axis — no new dependency.

One hue for every bar: the group header and indentation already encode grouping, and the
four status colours fail all-pairs CVD separation as a set. Overdue is an outline plus a
title, never a fill. Tick spacing derives from the span rather than exposing a zoom
control nothing yet establishes is needed.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 7: Wire the fourth panel into the dashboard

**Files:**
- Modify: `frontend/src/app/project/[id]/page.tsx`

**Interfaces:**
- Consumes: `GET /api/projects/{id}/timeline` (Task 4), `TimelineChart` / `TimelineGroup` / `TimelineItem` / `EditableCell` (Tasks 5–6), `useRowEdits` (Task 5).
- Produces: nothing downstream.

- [ ] **Step 1: Add the response type**

In `frontend/src/app/project/[id]/page.tsx`, after the `HealthResponse` interface (which ends at line 117), add:

```tsx
interface TimelineResponse {
  tab: string
  project_name: string
  start_header: string | null
  due_header: string | null
  group_by: string | null
  groupable: string[]
  editable_headers: string[]
  people_columns: RoleColumn[]
  range_start: string | null
  range_end: string | null
  today: string
  groups: TimelineGroup[]
  counts: { total: number; charted: number; milestone_only: number; undated: number; unparsed: number }
  /** Why there is nothing to draw. Present instead of an empty chart, because an empty
   *  chart reads as "no work is late" rather than as "this sheet records no dates". */
  reason: string | null
  primary_id_column: string | null
  truncated: boolean
}
```

Extend the `@/components/DataDisplay` import to include `TimelineChart`, and add `import type { TimelineGroup, TimelineItem } from "@/components/DataDisplay"`. Add `ChartGantt` to the `lucide-react` import.

- [ ] **Step 2: Add state and the fetch**

Change the `view` union at line 126:

```tsx
  const [view, setView] = useState<"grid" | "workload" | "health" | "timeline">("grid")
```

Add beside the other `useState` declarations:

```tsx
  const [timeline, setTimeline] = useState<{ key: string; data: TimelineResponse | null } | null>(null)
  const [groupBy, setGroupBy] = useState("")
  const [selected, setSelected] = useState<TimelineItem | null>(null)
```

Add the fetch effect immediately after the health effect (which ends at line 269). Unlike health it inherits the filter bar, so it is debounced — a timeline scan reads the whole tab, and the 60-second row cache only makes that cheap if it is not fired per keystroke:

```tsx
  // Fetched only while the panel is open. Unlike health it narrows with the filters, so
  // it must re-read when they change — debounced, because a timeline scans the whole tab
  // and the 60s row cache only makes that cheap if it is not requested on every keystroke.
  useEffect(() => {
    if (view !== "timeline" || !apiToken || !projectId) return
    let cancelled = false
    const key = `${tab}|${q}|${person}|${roleKey}|${overdueOnly}|${groupBy}`

    const run = async () => {
      const params = new URLSearchParams()
      if (tab) params.set("tab", tab)
      if (q) params.set("q", q)
      if (person) params.set("person", person)
      if (roleKey) params.set("role_key", roleKey)
      if (overdueOnly) params.set("overdue", "true")
      if (groupBy) params.set("group_by", groupBy)
      try {
        const res = await fetch(`/api/projects/${projectId}/timeline?${params.toString()}`, {
          headers: authHeaders,
        })
        const body = res.ok ? await res.json() : null
        if (!cancelled) setTimeline({ key, data: body })
      } catch {
        if (!cancelled) setTimeline({ key, data: null })
      }
    }

    const timer = setTimeout(run, 300)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [view, tab, q, person, roleKey, overdueOnly, groupBy, apiToken, projectId, authHeaders])
```

- [ ] **Step 3: Add the toggle button**

In the view toggle group, after the Health button (which closes at line 502) and before the closing `</div>`:

```tsx
              <button
                onClick={() => setView("timeline")}
                className={`inline-flex items-center gap-1.5 rounded px-3 py-1.5 text-xs transition cursor-pointer ${
                  view === "timeline" ? "bg-brass-400/15 text-brass-300" : "text-ink-400 hover:text-ink-200"
                }`}
              >
                <ChartGantt className="h-3.5 w-3.5" /> Timeline
              </button>
```

- [ ] **Step 4: Render the panel**

The filter bar's visibility condition already reads `view === "health" ? "hidden" : "flex"`, so the timeline inherits the filters with no change — which is the intended behaviour and the reason health is the only exception.

Add a branch to the view chain. The existing chain is `view === "grid" ? (…) : view === "workload" ? (…) : (…health…)`; insert a `view === "timeline"` arm before the health arm:

```tsx
        ) : view === "timeline" ? (
          <section className="space-y-3">
            {/* The coverage line leads, and is not a footnote. A chart drawn over 41 of
                412 rows reads as "little work" rather than "few dates", which is a false
                statement rather than a missing one. */}
            {timeline?.data && (
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-ink-500">
                <span className="tabular-nums">
                  <span className="text-ink-300">{timeline.data.counts.charted}</span> of{" "}
                  {timeline.data.counts.total} charted
                </span>
                <span className="tabular-nums">{timeline.data.counts.milestone_only} milestone-only</span>
                <span className="tabular-nums">{timeline.data.counts.undated} undated</span>
                {timeline.data.counts.unparsed > 0 && (
                  <button
                    onClick={() => setView("health")}
                    className="cursor-pointer text-failed underline decoration-dotted underline-offset-2 tabular-nums"
                    title="These cells hold something that is not a readable date. Open the health panel to see which."
                  >
                    {timeline.data.counts.unparsed} unreadable
                  </button>
                )}
                {timeline.data.start_header && (
                  <span className="stamp stamp-muted">{timeline.data.start_header} → {timeline.data.due_header}</span>
                )}
                {timeline.data.truncated && (
                  <span className="text-failed">Partial scan — every count is a floor.</span>
                )}
              </div>
            )}

            {timeline?.data && timeline.data.groupable.length > 0 && (
              <div className="flex items-center gap-2">
                <label className="label-micro" htmlFor="tl-group">Group by</label>
                <select
                  id="tl-group"
                  value={groupBy || timeline.data.group_by || ""}
                  onChange={(e) => setGroupBy(e.target.value)}
                  className="cursor-pointer rounded-lg border border-[var(--color-rule-strong)] bg-ink-950 px-2.5 py-1.5 text-xs text-ink-100 focus:border-brass-500 focus:outline-none"
                >
                  <option value="">No grouping</option>
                  {timeline.data.groupable.map((h) => (
                    <option key={h} value={h}>{h}</option>
                  ))}
                </select>
              </div>
            )}

            {!timeline || timeline.key !== `${tab}|${q}|${person}|${roleKey}|${overdueOnly}|${groupBy}` ? (
              <div className="well rounded-xl p-8 text-center text-sm text-ink-500">Reading the sheet…</div>
            ) : !timeline.data ? (
              <div className="well rounded-xl p-8 text-center text-sm text-ink-500">
                The timeline could not be loaded.
              </div>
            ) : timeline.data.reason ? (
              // Say why, rather than draw an empty axis. An empty chart reads as good news.
              <div className="well rounded-xl p-8 text-center text-sm text-ink-400">
                {timeline.data.reason}
                <div className="mt-2 text-xs text-ink-500">
                  Map a start or deadline column in Admin → Projects to place these rows on a timeline.
                </div>
              </div>
            ) : (
              <TimelineChart
                groups={timeline.data.groups}
                rangeStart={timeline.data.range_start!}
                rangeEnd={timeline.data.range_end!}
                today={timeline.data.today}
                onSelectItem={setSelected}
              />
            )}
          </section>
        ) : (
```

- [ ] **Step 5: Add the row dialog**

Immediately before the page component's final closing `</div>` (the outermost wrapper), add:

```tsx
      {/* Clicking a bar opens the row rather than editing it in place: dragging a bar is
          a genuine interaction design problem and the drawing should be proven first.
          The cells are the grid's own, through the shared hook, so the two surfaces
          cannot disagree about what "queued" means. */}
      <Modal
        open={selected !== null}
        onClose={() => setSelected(null)}
        title={selected?.id || "Row"}
        description={selected?.label}
        size="lg"
      >
        {selected && (
          <div className="space-y-3">
            {timeline?.data?.editable_headers.map((header) => {
              const cellId = `${selected.id}::${header}::modal`
              return (
                <div key={header} className="flex items-baseline gap-3">
                  <div className="w-40 shrink-0">
                    <span className="label-micro">{header}</span>
                  </div>
                  <div className="min-w-0 flex-1 text-[12.5px] text-ink-300">
                    <EditableCell
                      label={`${header} for ${selected.id}`}
                      value={selected.values[header] ?? ""}
                      edit={pending[`${selected.id}::${header}`]}
                      isEditing={editing === cellId}
                      onBeginEdit={() => setEditing(cellId)}
                      onCancel={() => setEditing(null)}
                      onSave={(next) => saveCell(selected.id, header, next, selected.row_number)}
                      placeholder="—"
                    />
                  </div>
                </div>
              )
            })}
            <p className="pt-2 text-[11px] leading-relaxed text-ink-500">
              Edits are queued and applied to the spreadsheet a moment later. Dates are written
              exactly as typed, so keep the format this sheet already uses.
            </p>
          </div>
        )}
      </Modal>
```

Add `import Modal from "@/components/Modal"` to the imports.

- [ ] **Step 6: Verify it compiles, lints and builds**

```bash
cd /d/TMC/MigrationBot/migrationbot/frontend && npx tsc --noEmit && npm run lint && npm run build
```

Expected: `tsc` silent, build succeeds, no new lint errors.

- [ ] **Step 7: Run the backend suite once more**

Nothing here touches the backend, but the branch must stay green end to end.

```bash
cd /d/TMC/MigrationBot/migrationbot/backend && \
DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/migrationbot_test" REDIS_URL="redis://localhost:6379" DEEPSEEK_API_KEY=mock JWT_SECRET=test-secret-32-characters-long-xx CORS_ORIGINS="http://localhost:3000" ADMIN_EMAILS="a@example.com" DEFAULT_SPREADSHEET_ID=test \
python -m pytest tests/test_core tests/test_sheets -q
```

Expected: `440 passed`.

- [ ] **Step 8: Commit**

```bash
cd /d/TMC/MigrationBot/migrationbot && git add frontend/src/app/project/\[id\]/page.tsx && git commit -m "feat(ui): a timeline panel beside Grid, Workload and Health

Inherits the filter bar — health is the only panel that hides it, and its reason ('362
rows have no deadline' is a fact about the tab) does not transfer to a chart someone wants
narrowed to one person.

The coverage line leads rather than footnotes: charted / milestone-only / undated /
unreadable, with unreadable linking to the health panel, and the resolved column names
stamped beside them so 'no bars' can be told apart from 'wrong column'. A tab with no date
column states why instead of drawing an empty axis.

Clicking a bar opens the row in the shared Modal with the grid's own editable cells.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 8: Document in TDD.md, verify, and open the PR

**Files:**
- Modify: `TDD.md`

**Interfaces:** none — this task produces documentation and a PR.

**Read this first.** TDD.md is the authoritative architecture doc and it has twice recorded itself asserting code that never existed (§11.1 records both instances). Write these sections **from the merged diff**, not from this plan. If the implementation diverged from the plan, the diff is right and the plan is wrong.

- [ ] **Step 1: Re-read what was actually built**

```bash
cd /d/TMC/MigrationBot/migrationbot && git diff main...HEAD --stat && git diff main...HEAD -- backend/app/core/timeline.py | head -80
```

- [ ] **Step 2: Add §10.9 to TDD.md**

Insert after §10.8 ("Drive watch control"), immediately before the `---` that precedes `## 11. Google Sheets Integration`:

```markdown
### 10.9 Timeline

`GET /api/projects/{id}/timeline?tab=&group_by=` (`api/timeline.py:project_timeline`) places a
tab's rows on a time axis. Its own router rather than a sixth endpoint in `api/dashboard.py`,
which is 687 lines — the split `api/aliases.py` and `api/digest.py` already made.

All computation is in `core/timeline.py:build_timeline`, which is pure, does no I/O, and takes
`today` as a parameter, matching `core/digest.py:build_digest`: a timeline that cannot be
regenerated identically cannot be checked against what a reader saw.

**Every row lands in exactly one of four buckets, and they sum to the row total** — `bar`
(both dates readable), `milestone` (one), `undated` (neither cell has content), `unparsed`
(a cell has content that will not parse). The last two are separate for the reason
`core/health.py` separates `no_deadline` from `unreadable_date`: `17/0/2026` is real, present
on the reference tracker, looks filled in, and evaluates to nothing. `unparsed` deliberately
beats a readable partner — a row with a good deadline and a junk start date is reported as
unreadable rather than drawn as a milestone, because drawing it hides the only thing that
state exists to surface.

The bar's right edge is `schema.py:get_due_column`, already shared with `summarize(overdue)`
and the dashboard's overdue filter, so the three surfaces cannot disagree about a deadline.
The left edge is `schema.py:get_start_column`, added here and mirroring it — including the
truthiness test on `date_columns.start` rather than `dict.get(key, default)`, since detection
writes that key holding a literal `null` on the LLM path and a stored `None` beats the
default (§16.6). It takes the resolved due header as an `exclude`: `_DUE_WORDS` and
`_START_WORDS` both match `"Planned Start"`, and a single-date tab would otherwise resolve one
header to both ends of every bar, drawing a zero-length span that reads as data rather than as
a mapping gap. Its word list omits `"assigned"` — which `schema_detect`'s own start matcher
includes — because that is a substring of `"Assigned To"`, and matching it would draw every
bar from a person's name.

Grouping is by any schema-declared column, defaulting to `module_column` when the tab has one
and to a single implicit group when it does not; no inline `"Module"` literal is introduced
(§16.3). A people column splits and resolves through `PersonResolver`, so a shared cell
(`Ahmed Qamar/Asif`) contributes to both people exactly as the Workload panel counts it, while
every other column is taken verbatim — `Sales & Distribution` is one module. Group header bars
are a `min`/`max` rollup **derived per read and never written back**, the posture `days_source`
takes toward effort. Past `MAX_GROUPS` (12, matching §14.2's bar-chart-to-table threshold) the
tail folds into one `Other` bucket carrying its count.

The pipeline is `digest.py:preview_digest`'s with two deliberate differences: `_row_dicts` is
called **with** `data_start_row`, so every item carries `ROW_NUMBER_KEY` and a bar can open an
editable row on a tracker with duplicated IDs (§16.7); and `_filter_rows` is reused, so the
chart cannot drift from the grid — the reason that helper was extracted for the CSV export.
There is no `limit`/`offset`, for the reason `rows.csv` has none.

A tab with no date column returns a `reason`, not an empty `groups` list dressed as a result.
`ROW_NUMBER_KEY` moved from `api/dashboard.py` to `core/schema.py` in this change so `core/`
and `api/` share one literal.
```

- [ ] **Step 3: Add §14.12 to TDD.md**

Insert after §14.11 ("Live sync control"), before the `---` preceding `## 15`:

```markdown
### 14.12 The timeline panel

A fourth view on `/project/[id]`, backed by §10.9. **It inherits the filter bar**; §14.8's
health panel is the only view that hides it, and that argument does not transfer — "362 rows
have no deadline" is a fact about the tab, while a timeline narrowed to one person is exactly
what a reader wants.

`components/DataDisplay.tsx:TimelineChart` is hand-rolled CSS positioning against a shared
axis. Recharts is already a dependency and has no Gantt primitive; the shapes usually bolted
onto its `BarChart` to imitate one break as soon as rows are grouped. Tick spacing derives
from the data span rather than exposing a zoom control, and the time region scrolls inside its
own container so the page body never scrolls sideways (§14.1's rule for wide tables).

**One hue for every bar** — `SERIES_HUE`, already validated against this ground. The source
template colours each phase differently; grouping is already carried by the group header and
indentation, so hue would be redundant, and §14.2 records that the four status colours fail
all-pairs CVD separation as a set. Overdue is therefore a `failed`-token **outline plus a
title**, never a fill.

**The coverage line leads.** `charted / milestone-only / undated / unreadable`, with the
unreadable count linking to the health panel and the resolved column names stamped beside it —
"no bars" has two very different causes, no dates recorded or the wrong column resolved, and
nothing else on screen distinguishes them. This is §14.5's finding applied before it could
recur: a chart over 41 of 412 rows reads as *little work* rather than *few dates*.

Clicking a bar opens the row in `components/Modal.tsx` with editable cells rather than
supporting drag-to-reschedule. That required extracting the grid's edit machinery, which had
been a 62-line closure inside `DataTable`'s `renderCell` prop entangled with `pending` state,
`saveCell`, the `queue_update` bridge and the 8-second job poll: `hooks/useRowEdits.ts` now
owns the state machine and `DataDisplay.tsx:EditableCell` owns the control, so the grid and the
dialog cannot disagree about what "queued" means. The dialog offers the start, due, status and
people columns; RBAC still gates each field at dispatch (§6.2), so a wider affordance is not a
wider permission. **Cells carry the sheet's raw text, never the parsed ISO date**, so editing a
tracker that spells dates `10.05.2026` does not silently rewrite the column into ISO.

Deferred deliberately: drag-to-reschedule (available through the existing write path — a bar
drag is an `update_cell` — but drag UX is where the bugs live, and the drawing should be proven
against a real tracker first), and WBS/hierarchy detection.
```

- [ ] **Step 4: Note the resolved item in §16.3**

In §16.3's bullet list of what is still not generic, append to the `module_column` bullet:

```markdown
  `core/timeline.py` reads it the same way `schema.py:default_critical_headers` does —
  `tab_schema.get("module_column")` with no inline default — so the timeline did not add a
  fourth copy of the literal.
```

- [ ] **Step 5: Full verification before the commit**

```bash
cd /d/TMC/MigrationBot/migrationbot/backend && \
DATABASE_URL="postgresql+asyncpg://u:p@localhost:5432/migrationbot_test" REDIS_URL="redis://localhost:6379" DEEPSEEK_API_KEY=mock JWT_SECRET=test-secret-32-characters-long-xx CORS_ORIGINS="http://localhost:3000" ADMIN_EMAILS="a@example.com" DEFAULT_SPREADSHEET_ID=test \
python -m pytest tests/test_core tests/test_sheets -q && \
cd .. && ruff check backend/app && \
cd frontend && npx tsc --noEmit && npm run lint; npm run build
```

All must pass: `440 passed`, `All checks passed!`, `tsc` silent, build succeeds. Record the actual numbers — do not claim them from this plan.

- [ ] **Step 6: Confirm no invariant was broken**

```bash
cd /d/TMC/MigrationBot/migrationbot && git diff main...HEAD --stat
```

Confirm the diff touches **none** of: `backend/app/queue/`, `backend/app/sheets/write.py`, `backend/app/sheets/format.py`, `backend/app/core/permissions.py`, `backend/app/models/`, `backend/migrations/`, `backend/requirements.txt`, `frontend/package.json`, `frontend/src/auth.ts`. If any appears, stop — the change has left its stated scope.

- [ ] **Step 7: Commit the documentation**

```bash
cd /d/TMC/MigrationBot/migrationbot && git add TDD.md && git commit -m "docs(tdd): record the timeline panel (§10.9, §14.12) and its §16.3 consequence

Written from the merged diff rather than from the plan, since TDD.md has twice recorded
itself asserting code that was never written.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

- [ ] **Step 8: Push and open the PR**

```bash
cd /d/TMC/MigrationBot/migrationbot && git push -u origin feat/timeline-panel && gh pr create --base main --title "feat: timeline panel over an existing tracker" --body "$(cat <<'EOF'
## What

A fourth read-only panel on \`/project/[id]\`, beside Grid / Workload / Health: each row drawn as a bar (both dates) or a milestone (one), grouped by a column the sheet already has.

Spec: \`docs/superpowers/specs/2026-08-20-timeline-panel-design.md\`
Plan: \`docs/superpowers/plans/2026-08-20-timeline-panel.md\`

## What it does not do

No spreadsheet creation, no \`.xlsx\` import, no column add/delete, no drag-to-reschedule, no new agent tool. **No new dependency, no new OAuth scope, no new table, no migration.** Every write path, cache tier and RBAC rule is untouched.

## The decisions worth reviewing

- **Four row states, not three.** \`undated\` and \`unparsed\` are counted separately, because \`17/0/2026\` on the reference tracker looks filled in and evaluates to nothing. \`unparsed\` beats a readable partner deliberately — see the docstring on \`classify_row\`.
- **\`get_start_column\` repeats \`get_due_column\`'s shape rather than sharing an abstraction.** The two word lists encode opposite judgements about the same headers.
- **The coverage line leads the panel.** A chart over 41 of 412 rows reads as little work rather than few dates — §14.5's finding, applied before it could recur.
- **One refactor rode along:** the grid's inline-edit closure became \`useRowEdits\` + \`EditableCell\` (Task 5, its own commit, no behaviour change) so the timeline's dialog is not a second queued-write state machine.

## Verification

Backend: 440 passed locally (\`tests/test_core tests/test_sheets\`), ruff clean.
Frontend: \`tsc --noEmit\` clean, \`next build\` succeeds, no new lint errors.

**Not yet verified against a deploy.** Per TDD §15.4 a green frontend build proves the code compiles, not that it renders — three of the four defects that section catalogues were invisible without a running backend. The rendering needs a look on migrationbot.duckdns.org before this is called done.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 9: Verify against the deploy**

Once CI is green and the branch is deployed, open `/project/{id}`, switch to Timeline, and check:

1. bars appear and align with the axis dates;
2. the coverage line's four numbers sum to the row total shown on the Grid;
3. switching the grouping column re-groups without a full page reload;
4. the filter bar narrows the timeline the same way it narrows the grid;
5. clicking a bar opens the dialog, and an edit shows `queued` then clears;
6. the overdue outline appears on a row whose deadline has passed and whose status is not finished;
7. the panel is usable at a laptop width, not only at desktop width.

Only after this is the feature done.

---

## Notes for the executor

- **Model split, per the requester:** dispatch Sonnet for code discovery and shell execution; keep design, implementation, debugging and review on Opus.
- **Never run bare `pytest`.** `tests/test_db.py` and `tests/integration/` need Postgres, which is not available on this machine. Always scope to `tests/test_core tests/test_sheets`, always with the seven env vars.
- **If a test count differs from this plan**, trust the run and adjust the expectation — do not adjust the test to hit a number.
- **If `_filter_rows`'s signature has changed** since this plan was written, Task 4's call must follow the real one; the spy test in Task 4 Step 1 will catch a mismatch immediately.
