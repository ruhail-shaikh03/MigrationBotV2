---
name: migrationbot-rbac-audit
description: Audit or modify MigrationBot's RBAC and audit-logging path — core/permissions.py (PermissionChecker.can_execute, get_user_permissions, READ_ONLY_TOOLS/WRITE_TOOLS), admin/editor/viewer roles, field-level allowed_fields enforcement, and audit_logs writes from queue/worker.py. Use when adding a tool that needs permission coverage, investigating why a user was allowed or denied, reviewing the fail-open default policy, or verifying mutations are audited.
---

# Auditing MigrationBot RBAC & Audit Trail

## Enforcement happens once, at dispatch

`PermissionChecker.can_execute(tool_name, args) -> (allowed: bool, reason: str)` is called
from `agentic_loop.py` before every tool execution. Frontend gating (`GET /api/me` →
`is_admin`) is cosmetic only. **A tool that reaches `dispatch_tool` without passing the
checker is a privilege-escalation bug.**

## Resolution chain — `get_user_permissions(db, email, project_id)`

*(TDD.md §2 calls this `resolve_user_permissions()`; the real name is `get_user_permissions()`.)*

1. `email in settings.admin_emails_list` → `admin`, `allowed_fields=["*"]`, no denied ops.
2. `project_id is None` → **default editor with `["*"]`**.
3. No `User` row for the email → **default editor with `["*"]`**.
4. No `Permission` row for `(user_id, project_id)` → **default editor with `["*"]`**.
5. Otherwise → role, `allowed_fields`, `denied_operations` from the `permissions` row.

**This default is fail-open.** Three separate paths grant full write access to everything when
data is merely *missing*. An unregistered user, or any user on a project with no permission
row, is an editor with `["*"]`. Treat any change here as security-relevant, and never add a
fourth silent fallback.

## Role semantics (`can_execute`)

- **admin** — returns `True` immediately; no field or denied-op checks apply.
- **viewer** — allowed only if `tool_name in READ_ONLY_TOOLS`; everything else denied.
- **editor** (and any other role string) — denied if `tool_name in denied_ops`, then
  field-level checks:
  - `update_cell`: every `args["updates"][i]["field"]` must be in `allowed_fields`
  - `bulk_update`: `args["set_field"]` must be in `allowed_fields`
  - skipped entirely when `allowed_fields == ["*"]`

`format_row` and `add_row` have **no field-level enforcement** — an editor restricted to two
columns can still recolor any row and append new ones. Intentional or not, know it before
citing `allowed_fields` as a containment boundary.

Role strings are lowercased/stripped in `__init__`, but an unrecognized role silently takes
the **editor** branch — there is no "unknown role → deny". Validate role values at write time.

## The two tool lists that must stay in sync

```python
# core/permissions.py:8-9
READ_ONLY_TOOLS = {"get_row", "search_rows", "summarize", "switch_module", "data_quality"}
WRITE_TOOLS     = {"update_cell", "bulk_update", "format_row", "add_row"}
```

`core/tool_dispatch.py` re-declares the same split as **inline tuples** (read at line ~26,
write at line ~64) rather than importing these sets. Adding a tool to dispatch but not to
`permissions.py` makes it invisible to `READ_ONLY_TOOLS`, so **viewers are denied a legitimate
read tool** — precisely the `data_quality` bug that was fixed. Always change both.

Note `WRITE_TOOLS` is defined but not consulted by `can_execute()`: editors are gated by
`denied_ops` and field checks, viewers by absence from `READ_ONLY_TOOLS`. A new write tool is
therefore permitted for editors by default the moment it exists.

## Audit trail

Mutations are audited by the **worker**, not the request path.
`queue/worker.py:process_job()` calls `_write_audit_record()` — per field for `update_cell`,
per RICEFW ID for `bulk_update` — writing to `audit_logs` (`user_email`, `session_id`,
`tool_name`, `spreadsheet_id`, `sheet_tab`, `ricefw_id`, `field`, `old_value`, `new_value`,
`args_json`, `result_ok`, `error`, `created_month`).

`old_values` are pre-read in `tool_dispatch.py` **before** enqueueing and travel on the job
payload. If the pre-read fails it's swallowed with a warning and the job proceeds — the audit
row then has a null `old_value`. A missing `old_value` means the pre-read failed, not that
the field was empty.

`core/audit.py:log_audit()` (fire-and-forget via `asyncio.create_task`) exists for non-worker
paths. The `log_update_cell` / `log_bulk_update` / `log_format_row` / `log_add_row` wrappers
that older docs describe **do not exist** in the codebase.

## Audit checklist for a new or changed tool

1. Listed in `READ_ONLY_TOOLS` or `WRITE_TOOLS`?
2. Dispatch tuple in `tool_dispatch.py` updated to match?
3. Write tool → branch in `worker.py:process_job()`, and does it audit?
4. Mutates specific fields → does `can_execute()` field-check it, or is it another
   `format_row`-style hole?
5. Verify with a **viewer** and a **field-restricted editor**, not just an admin — admin
   short-circuits every check and will pass regardless.

Existing coverage: `test_core/test_core_logic.py::test_rbac_interception` unit-tests
`PermissionChecker` directly. **No test verifies enforcement through `run_agentic_loop`**, so
loop-level regressions will not be caught.
