---
name: migrationbot-schema-validator
description: Validate, debug, or extend MigrationBot project schema_config JSON — the per-project tab/column mapping produced by core/schema_detect.py (detect_schema_config, detect_all_tabs) and consumed by _get_tab_schema() and core/column_mapper.py:resolve_column(). Use when columns resolve to the wrong field, headers have trailing spaces, a new tracker spreadsheet needs onboarding, or auto-detection returns an unusable config.
---

# Validating MigrationBot `schema_config`

`schema_config` is JSONB on `projects.schema_config`. Every column lookup in the read, write,
and format paths depends on it. A wrong schema does not raise — it silently reads or writes
the wrong column.

## Two shapes, both live

`sheets/read.py:_get_tab_schema()` accepts either:

```python
def _get_tab_schema(schema_config: dict, active_tab: str) -> dict:
    if "tabs" in schema_config:
        return schema_config.get("tabs", {}).get(active_tab, {})
    return schema_config          # legacy flat single-tab config
```

Current format is `{"tabs": {"SD": {...}, "MM": {...}}, "global": {...}}`. Legacy flat configs
still exist in older projects. Code touching schemas must handle both — assuming `"tabs"`
exists will break older projects, and assuming it doesn't will silently return `{}` for
multi-tab ones.

## Per-tab keys and their silent defaults

| Key | Default if absent | Consequence of being wrong |
|---|---|---|
| `primary_id_column` | `"RICEFW ID"` | ID lookups miss |
| `primary_id_position` | `"B"` | Scans the wrong column; `find_row_num`/`get_all_ids` also default to `"B"` |
| `data_start_row` | `3` | Off-by-one reads the header as data |
| `status_column`, `module_column`, `assignee_column`, `description_column`, `type_column` | none | Summaries/quality checks return empty |
| `date_columns` | `{}` | `overdue`/`completion_rate` degrade silently |
| `critical_fields` | `[]` | `completeness_score()` becomes meaningless |
| `valid_modules`, `valid_types` | `[]` | Consistency rules skip |
| `column_map` | `{}` | **Falls back to static `COLUMN_ALIASES` only** — LLM aliases lost |

Defaults are the danger: a project with an empty `schema_config` still "works" while reading
column B of row 3 of whatever tab it lands on.

## Trailing whitespace in headers is real and load-bearing

Detected configs legitimately contain values like `"assignee_column": "Technical Resource "`.
That trailing space is the actual spreadsheet header text. **Never `.strip()` a header when
using it as a dict key or match target** — resolve through
`core/column_mapper.py:resolve_column()`, whose 3-tier pipeline handles it:

1. exact match (case-insensitive, stripped)
2. alias list match against `COLUMN_ALIASES` (72 entries) or the project `column_map`
3. fuzzy match via `difflib.get_close_matches`, cutoff 0.6

Tier 3 is why a bad schema misfires quietly instead of erroring: a wrong-but-similar header
name still resolves to *something*.

## Auto-detection

`core/schema_detect.py`:
- `detect_schema_config()` — one LLM call per tab, given headers (first 5 rows) + 3 sample data rows.
- `detect_all_tabs()` — iterates all tabs, returns `{tabs: {...}, global: {...}}`.

Entry points: `POST /api/admin/projects/detect-metadata` (the Auto-Detect Wizard), and
`admin.py:create_project()`, which now auto-runs `detect_all_tabs()` when the caller omits
`schema_config` **and** a Google token is present. Without a token it falls back to `{}` —
API-created projects are the usual source of empty schemas.

## Validating a config before trusting it

1. Every tab in the sheet has a key under `tabs`.
2. `primary_id_position` matches the column actually holding IDs — verify against a real row,
   not the header name.
3. `data_start_row` points at data, not the header.
4. `critical_fields` entries exist verbatim (whitespace included) in the header row.
5. `column_map` is non-empty — if it is empty, LLM aliasing was lost and only the 72 static
   aliases apply.
6. `valid_modules` / `valid_types` match `data_quality`'s consistency rules
   (`core/data_quality.py:consistency_checks()`).

## Known bug to avoid propagating

`sheets/read.py:get_bulk_rows_raw()` hardcodes `column_map={}` when resolving `filter_by`
targets, ignoring the project's real map (TDD §20.2). Bulk-read filters therefore see static
aliases only, while the equivalent write path was fixed. Don't mirror this pattern; fix it if
you're already in that function.

There are no tests for `schema_detect.py` or `column_mapper.py` — validate changes manually
against a real tracker sheet.
