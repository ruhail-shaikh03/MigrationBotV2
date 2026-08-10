---
name: migrationbot-sheets-testing
description: Write or debug pytest tests for MigrationBot's Google Sheets layer (backend/app/sheets/ — read.py, write.py, format.py, meta.py, retry.py, sync.py) and the sheet_records Postgres cache. Use when adding tests for sheet reads/writes, mocking the googleapiclient service object, testing retry/backoff on 429/500/503, testing sync_sheet_to_db cache behavior, or when pytest fails to import app.* modules.
---

# Testing the MigrationBot Sheets Layer

## Run tests from `backend/`, never repo root

`backend/pyproject.toml` sets `pythonpath = ["."]` and `testpaths = ["tests"]`. Running
`pytest backend/tests` from the repo root fails to import `app.*`.

```bash
cd backend && pytest -v --tb=short
cd backend && pytest tests/test_sheets/ -v
```

## Tests need a real PostgreSQL

`tests/conftest.py` imports the app's actual engine from `app.db.engine` — there is no
SQLite fallback and no testcontainers. CI provides a `postgres:16` service container. Locally
you need Postgres reachable at `DATABASE_URL`, or the whole suite errors at fixture setup.

Three fixtures exist, and that is all:

| Fixture | Scope | Behavior |
|---|---|---|
| `dispose_engine` | session, autouse | Disposes the engine at session end |
| `setup_test_db` | function | `drop_db()` → `init_db()`, then drops again on teardown |
| `db_session` | function | Yields `AsyncSessionLocal()` — **no transaction rollback wrapper** |

`db_session` does not isolate. Tests mutate shared state. If your test writes rows, depend on
`setup_test_db` too, or assert against values you uniquely created.

`asyncio_mode = "auto"` is set — write `async def test_...` with no `@pytest.mark.asyncio`.

## Mock the Sheets service, never hit the network

The `service` object is a `googleapiclient` discovery resource. Fake it with a `MagicMock`
and mirror the exact call chain used in the code under test:

```python
from unittest.mock import MagicMock

service = MagicMock()
service.spreadsheets().values().get().execute.return_value = {
    "values": [
        ["", "RICEFW ID", "Module", "Dev Status"],   # header row (row 2)
        ["", "SD-045", "SD", "In Progress"],          # data starts row 3
    ]
}
```

Match the real chain per module: `read.py` uses `values().get()`, `write.py` uses
`values().batchUpdate()`, `format.py` uses `batchUpdate()` with `repeatCell`, `meta.py` uses
`spreadsheets().get()` for tab metadata.

## Testing `_with_retry()` backoff

`sheets/retry.py:_with_retry()` retries on `{429, 500, 503}`, max 4 attempts, base delay 1.0s,
and runs `fn` through a `ThreadPoolExecutor` via `run_in_executor`. Simulate failures with a
real `HttpError` carrying a mocked status:

```python
from googleapiclient.errors import HttpError
from unittest.mock import MagicMock

resp = MagicMock()
resp.status = 429
err = HttpError(resp=resp, content=b"rate limited")
```

Patch the sleep to keep the test fast; assert attempt counts rather than wall-clock timing.
Because `fn` runs in an executor, a `MagicMock` side effect is called from a worker thread —
keep it thread-safe (no shared mutable state without a lock).

## The `sheet_records` cache changes what "a read" means

`get_row`, `search_rows`, `summarize`, and `run_data_quality_check` read from the
`sheet_records` Postgres table, not live Sheets. `read.py:_ensure_sheet_synced()` calls
`sheets/sync.py:sync_sheet_to_db()` only on a cache miss.

So a read test needs **either** a seeded `SheetRecord` row (`models/sheet_record.py`:
`spreadsheet_id`, `tab_name`, `ricefw_id`, `data` JSON) **or** a mocked service for the
sync path to consume. A passing read test with an unused service mock usually means the
cache was already warm — assert on which path ran, not just the return value.

`tests/test_sheets/test_sync.py` is the existing model for cache tests.

## Schema arguments are not optional in practice

Read/write functions take `schema_config` and `column_map`. Build them to match what
`_get_tab_schema()` expects — `{"tabs": {"SD": {...}}}` for multi-tab, or a flat dict for
legacy. Defaults if you omit keys: `data_start_row` = 3, `primary_id_position` = `"B"`.
Passing `column_map={}` silently degrades to static `COLUMN_ALIASES` — that is exactly the
live bug in `get_bulk_rows_raw()` (TDD §20.2), so don't accidentally reproduce it in a test
and call it correct behavior.

## Known coverage gaps — likely why you're here

No direct tests exist for `write.py`, `format.py`, `meta.py`, or `client.py`. The write-flow
integration test patches `app.sheets.write.find_row_num` and calls `process_job` directly,
so real write logic is never exercised. New write/format tests are net-new, not additions to
an existing pattern.
