# Timeline Panel — Design

**Status:** approved in brainstorming, 2026-08-20
**Scope:** a fourth read-only panel on `/project/[id]`, beside Grid / Workload / Health.

---

## 1. What this is, and what it is not

A timeline (Gantt-style) view of a tracker tab: one bar per row, bars grouped by a column
the sheet already has, drawn against a shared time axis.

The originating request was broader — "upload an Excel sheet, or create one, add or delete
columns, the whole project management rundown." That request decomposes into four
independent subsystems, and only the first is specified here:

| Subsystem | Status |
|---|---|
| Visualise the dates already in a tracker | **this spec** |
| Import an `.xlsx` and onboard it | deferred — Google Drive already converts `.xlsx` natively; a user can drop a file in Drive and point the existing onboarding flow at it |
| Author a new plan from a template | deferred — changes what the product *is*, from a read layer over the user's sheet to a producer of sheets |
| Structural editing (add/delete columns, indent levels) | deferred — see §7 |

**Explicit non-goals for this spec.** No spreadsheet creation. No file upload. No column or
row insertion/deletion. No drag-to-reschedule. No new agent tool. No new Postgres table. No
new OAuth scope. No new runtime dependency in `requirements.txt` or `package.json`.

## 2. Why this slice first

Every input the panel needs already exists and is already resolved by shared code:

- `schema_config.date_columns` carries `start`, `due`, `go_live`, `completion`, `signoff`.
- `core/schema.py:get_due_column` already resolves the deadline, and is already shared with
  the agent's `summarize(report_type="overdue")` and the dashboard's overdue filter — so
  chat, the grid and the timeline cannot disagree about what a deadline is (TDD §16.6).
- `core/people.py:parse_date` already reads the dotted `10.05.2026` form and already
  returns `None` for the malformed `17/0/2026` present on the reference tracker.
- `core/overdue.py:is_finished_status` already decides what "finished" means.
- `sheets/rows_cache.py:get_tab_matrix` already serves the rows through three cache tiers.
- `api/dashboard.py:_filter_rows` is already extracted and shared by the grid and the CSV
  export.

The panel therefore adds one pure module, one endpoint, one schema helper, and a renderer.
It touches no write path, no cache tier, no RBAC rule and no `schema_config` producer.

## 3. Backend

### 3.1 `core/timeline.py` — pure, no I/O

```
build_timeline(headers, rows, tab_schema, *, group_by=None, resolver=None, today=None)
    -> Dict[str, Any]
```

Modelled on `core/health.py:assess_tab` and `core/digest.py:build_digest`: no I/O, no
database, no Sheets client, and **`today` is a parameter** rather than `date.today()`. A
timeline that cannot be regenerated identically cannot be checked against what a user saw —
the same reason `build_digest` takes it.

No header string is spelled in this module. Every column arrives via a schema role,
resolved against the tab's real headers through `resolve_header` / `bind_columns`.

### 3.2 One new schema helper: `get_start_column`

`core/schema.py:get_start_column(tab_schema, headers) -> Optional[str]`, mirroring the
existing `get_due_column` exactly:

- reads `date_columns` keys in priority order, testing `if value and str(value).strip()`
  rather than `dict.get(key, default)`;
- falls back to scanning real headers for start-ish wording;
- returns `None` rather than settling for a wrong column.

**The `.get()` spelling is the point.** TDD §16.6's first defect was
`date_columns.get("go_live", "Go-Live Date")` returning a *stored `null`* in preference to
its default, which broke the overdue report on every registered project. Detection writes
the `start` key by the same mechanism, so the left edge of every bar has the identical bug
waiting on it. This helper is that fix applied to the other end of the span.

Its word list must exclude `"completion"`, `"actual"` and `"finish"` for the mirror of the
reason `_DUE_WORDS` excludes them: a column recording when work *actually began* is a
different fact from when it was *planned* to begin, and conflating them silently rewrites
history.

**The two word lists overlap, and that must be handled explicitly.** The existing
`_DUE_WORDS` is `("due", "deadline", "target", "expected", "planned", "go-live", "go live",
"golive", "eta")`. A header reading `Planned Start` contains `"planned"` and would be
claimed by the *due* scan as well as the start scan. Since `get_due_column` runs the
schema-key pass before its header scan, and this ordering is load-bearing, `get_start_column`
must accept the already-resolved due header and **refuse to return it** — otherwise a sheet
with one date column produces a zero-length bar on every row, which looks like data rather
than like a mapping error.

### 3.3 Four row states, not three

Each row is classified into exactly one:

| State | Condition | Drawn as |
|---|---|---|
| `bar` | both dates present and parse | a span |
| `milestone` | exactly one present and parses | a diamond at that date |
| `undated` | neither cell has content | not drawn; counted |
| `unparsed` | a cell has content that will not parse | not drawn; counted **separately** |

Splitting `undated` from `unparsed` is deliberate and non-negotiable. `core/health.py`
already distinguishes `no_deadline` from `unreadable_date` for exactly this reason: a value
like `17/0/2026` looks filled in to a human scanning the sheet and evaluates to nothing in
every calculation, so the row is silently dropped from the reports it appears to belong to.
Folding the two counts together would hide the defect the health panel exists to surface.

### 3.4 Grouping

`group_by` names a column.

- Default: the tab schema's `module_column` **when the schema declares one**. If it does
  not, there is one implicit group containing every row. The inline `"Module"` literal is
  **not** to be used as a fallback — TDD §16.3 lists it as live technical debt, and adding
  a fourth copy of it makes that debt harder to remove.
- Any column in `_column_descriptors` may be selected by the client.
- Grouping by a people column routes each cell through `PersonResolver.resolve_cell`, so a
  merged spelling groups the way the Workload panel counts it, and a shared cell
  (`Ahmed Qamar/Asif`) contributes to both people's groups. Grouping by any other column
  uses the cell value verbatim — an ampersand in `Sales & Distribution` is not two groups,
  the same distinction `summarize` already makes.
- Groups are capped. Past the cap, remaining groups are folded into one `Other` bucket
  carrying a count, rather than rendering hundreds of headers.

### 3.5 Rollup

A group's header bar spans `min(child start) → max(child end)` over children that have
usable dates, tagged so the client can render it distinctly. It is **derived at read time
and never written back to the sheet** — the same posture `days_source` takes toward effort.
A group in which nothing is dated renders collapsed with its row count and no bar, rather
than a zero-width bar at an arbitrary position.

### 3.6 `api/timeline.py` — its own router

`GET /api/projects/{project_id}/timeline`

A new module rather than a sixth read endpoint inside `api/dashboard.py`, which is already
687 lines. This is the precedent `api/aliases.py` set explicitly ("separate from
api/admin.py, which is already long enough that adding a fourth resource to it would make
the file hard to hold in context") and `api/digest.py` followed.

The handler is modelled on `digest.py:preview_digest`:

```
_resolve_project → _tab_for → get_tab_schema → build_sheets_service
    → get_tab_matrix → _row_dicts → _filter_rows → build_timeline
```

Four requirements on that pipeline:

1. **`_row_dicts` must be called with `data_start_row`**, unlike `preview_digest`, so each
   row carries `ROW_NUMBER_KEY` (`core/schema.py`). Without it a bar cannot open an editable row: the inline
   edit path needs the physical row number to pin a duplicated ID (TDD §16.7), and 27 of
   412 rows on the reference tracker share an ID.
2. **`_filter_rows` must be reused, not reimplemented.** It was extracted precisely so the
   CSV export could not drift from the grid (TDD §10.6); a timeline that filtered
   differently from the grid beside it would be the same defect.
3. **No `limit` or `offset`.** The response is the whole filtered set, for the reason
   `rows.csv` has no paging: a chart that omits rows the count above it claims is the one
   thing a chart must not do.
4. **Gating is identical to the other project reads** — `_resolve_project`, which returns
   **404, not 403**, on a permission miss. Confirming a project exists is itself a
   disclosure.

`truncated` echoes through from the scan, as everywhere else: past `_MAX_SCAN_ROWS` every
count is a floor.

### 3.7 Response shape

The response carries, alongside `groups`:

- `counts` — `{total, charted, milestone_only, undated, unparsed}`, the four states of §3.3
  plus their denominator, asserted to sum to `total`;
- `start_header`, `due_header` — the *resolved* column names, so the panel can state which
  columns it read rather than only what it found;
- `group_by`, `groupable` — the active grouping column and the columns offered;
- `range_start`, `range_end` — the axis bounds;
- `truncated`.

## 4. Frontend

### 4.1 The panel

A fourth `view` state on `/project/[id]`, alongside `"grid" | "workload" | "health"`.

**The filter bar stays visible and applies.** Health is the only panel that hides it, and
its reason does not transfer: "362 rows have no deadline" is a fact about the tab that a
search would falsify, whereas a timeline narrowed to one person is exactly what a reader
wants. Grid, Workload and Timeline all narrow together.

### 4.2 Renderer

CSS-grid layout with absolutely-positioned bars against a shared time axis. **No new
dependency.** Recharts, already in the project, has no Gantt primitive, and the shapes
commonly bolted onto its `BarChart` to imitate one break on grouped rows.

It belongs in `components/DataDisplay.tsx` beside `ChartFrame` / `DataTable` / `HoverTip`,
not inside the page. Those primitives were extracted so chat and dashboard could not "drift
into looking like different products while showing the same numbers from the same sheet"
(TDD §14.4), and a timeline is a plausible future chat tool result.

Layout requirements:

- a **sticky left label column** (row ID + primary description) and a horizontally
  scrolling time region;
- wide content scrolls **inside its own container**, never the page body — the rule §14.1
  already applies to markdown tables;
- **time bucket derived from the data span** (day / week / month), not exposed as a zoom
  control. A zoom control is a real feature and nothing yet establishes it is needed;
- a **today marker** on the axis;
- collapsible groups.

### 4.3 Colour

**One hue for bars** — `SERIES_HUE`, already validated against this ground by the data-viz
palette validator (TDD §14.2) — with the group header bar carrying more visual weight than
its children. The source template colours every phase differently; that is rejected here
because grouping is already encoded by the group header and indentation, making hue
redundant, and because the four status colours **fail all-pairs CVD separation as a set**,
which is why §14.2 reserves them for icon-plus-label pairings.

Overdue bars are flagged using the existing `_is_overdue` logic, as a **1px outline in the
`failed` token plus a glyph — never as the fill**. Colour alone would reintroduce the CVD
problem the palette rule exists to prevent.

### 4.4 The coverage line leads

Above the chart, not in a footnote:

```
41 of 412 charted · 283 milestone-only · 71 undated · 17 unreadable
```

`unreadable` links to the Health panel. TDD §14.5's finding was that a chart drawn over 40
of 412 rows reads as *no work* rather than *no dates*, and the correction there was making
the denominator inescapable — "525 days across 41 of 412 rows, never a bare total."

The panel must also name the columns it read (`start_header`, `due_header`), because "no
bars" has two very different causes — no dates recorded, or the wrong column resolved — and
the reader cannot distinguish them otherwise.

### 4.5 Click a bar → the row, editable

Clicking a bar opens `components/Modal.tsx` — the only dialog shell, portalled for the
reason §14.0.1 documents at length — showing that row's fields, with the same editable
cells the grid offers.

**This requires extracting the edit machinery.** As it stands the editable cell is not a
component: it is a 62-line closure (`page.tsx:644-705`) passed as `DataTable`'s
`renderCell` prop, entangled with `pending` / `editing` state, `saveCell`
(`page.tsx:336-372`), the `queue_update` CustomEvent bridge (`page.tsx:274-300`) and the
8-second `GET /api/jobs/{id}` reconciliation (`page.tsx:302-334`). Reusing it in a modal
without extraction means a second implementation of the queued-state machine, the
403-message rendering and the job poll — and a second one **will** drift. This is the
argument §14.4 already made when it added `renderCell` rather than letting the dashboard
grow its own table.

So the extraction is part of this work, not a follow-up:

- an `EditableCell` component, and
- a `useRowEdits(projectId, tab)` hook owning `pending`, `editing`, `saveCell`, the
  `queue_update` listener and the job poll.

The write function is named **`saveCell`**, not `submitEdit`; there is no `submitEdit` in
the codebase. The hook must preserve `saveCell`'s existing `row_number` argument, which is
the §16.7 pin.

Grid and modal then share one queued-state machine. This also takes a meaningful bite out
of a 1015-line page file, which is the right kind of improvement to make in code one is
already opening.

### 4.6 Fetching

Lazily — only while the panel is open, matching how Health fetches. Unlike Health it
inherits the filter bar, so it must refetch on filter change, **debounced** on the same
terms as the grid: a timeline scan reads the whole tab, and the 60-second row cache makes
that cheap only if it is not fired on every keystroke.

## 5. Testing

`core/timeline.py` is pure, so it is tested with no Postgres and no Redis — which matters,
because there is no Docker on the maintainer's machine and only `tests/test_core` and
`tests/test_sheets` run locally.

Required cases:

- a row with both dates → `bar`; one date → `milestone`; neither → `undated`;
- `17/0/2026` → `unparsed`, **not** `undated` (the §3.3 distinction, asserted directly);
- a group rollup spanning its children's min and max;
- a group in which nothing is dated → no bar, count preserved;
- grouping by a people column routes through the resolver, so `Ahmed Qamar/Asif`
  contributes to two groups while `Sales & Distribution` stays one;
- `get_start_column` returns `None` — not a default — when `date_columns.start` is a stored
  `null`, which is the §16.6 regression;
- header whitespace: a schema declaring `"Start Date "` resolves against a stripped row
  dict key (the §7.2 asymmetry);
- counts sum to `total_rows`, so no row can be silently dropped.

The endpoint's own test follows whichever harness the existing endpoint tests use.
Note that `test_dashboard_rbac.py` is **not** that precedent despite its name — it
constructs no app and no client, and unit-tests `PermissionChecker.can_execute` directly.

**Standing caveat (TDD §15.4).** `tsc --noEmit`, `next build` and `npm run lint` will all
pass on a timeline that renders wrong; three of the four defects §15.4 catalogues were
invisible without a running backend. The rendering must be checked against a deploy before
this is called done.

## 6. Files

| | |
|---|---|
| Create | `backend/app/core/timeline.py`, `backend/app/api/timeline.py`, `backend/tests/test_core/test_timeline.py`, `frontend/src/hooks/useRowEdits.ts` |
| Modify | `backend/app/core/schema.py` (add `get_start_column`), `backend/app/main.py` (mount router), `frontend/src/components/DataDisplay.tsx` (add `TimelineChart`, `EditableCell`), `frontend/src/app/project/[id]/page.tsx` (fourth view, extraction) |
| Unchanged | every write path, the Redis queue, RBAC, `schema_config` and its producers, all three cache tiers, every existing endpoint's contract |

## 7. Deferred, with reasons

- **Drag-to-reschedule.** Available through the existing write path — a bar drag is an
  `update_cell` on a date column, which would flow through the same producer, worker,
  audit rows and undo. Deferred because drag UX is where the bugs live and the drawing
  should be proven against a real tracker first.
- **Hierarchy detection.** Some trackers carry a WBS or phase column, or indentation in the
  task name. Adding a schema role for it is the natural follow-up (the shape `people_columns`
  established), but detection misfires and this spec's grouping already covers the flat case.
- **A `timeline` report type for the agent.** The 8-iteration cap makes a large structured
  result expensive, and nothing yet establishes the model needs one.
- **Structural editing and sheet authoring.** See §1. Both invert the invariant every other
  part of the system rests on — that Google Sheets is the record of truth and this
  application is a read-and-annotate layer over it. In particular, deleting a column is not
  reversible by the §10.5 undo mechanism, which restores cell values and has no concept of
  a column that no longer exists.
