# MigrationBot Enterprise Portal — Technical Design Document

> **Provenance.** Written from a full read of the `main` branch. Every factual claim carries a
> `file:symbol` citation (function, method, class, or constant name — not a line number) verified
> against that code. Symbol names survive refactors that shuffle line numbers; when a citation
> needs to point at one specific statement inside a larger function, the surrounding prose
> describes it rather than pinning a line. Descriptive sections state only what the code does;
> opinions — bugs, risks, smells — are confined to blockquoted **⚠ callouts** and to §16 *Known
> Issues*. Findings marked **(verified)** were reproduced by executing code, not only by reading it.
>
> Out of scope: `_legacy/` (dead Streamlit prototype; nothing under `backend/` imports it).

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Persistence Model](#2-persistence-model)
3. [Configuration & Startup](#3-configuration--startup)
4. [Authentication & Session Resolution](#4-authentication--session-resolution)
5. [Data Flow Traces](#5-data-flow-traces)
6. [RBAC Model](#6-rbac-model)
7. [Schema & Column Resolution](#7-schema--column-resolution)
8. [The Agentic Loop](#8-the-agentic-loop)
9. [WebSocket Protocol Reference](#9-websocket-protocol-reference)
10. [REST Surface](#10-rest-surface)
11. [Google Sheets Integration](#11-google-sheets-integration)
12. [Audit Logging](#12-audit-logging)
13. [Data Quality Engine](#13-data-quality-engine)
14. [Frontend Application](#14-frontend-application)
15. [Deployment, CI/CD & Tests](#15-deployment-cicd--tests)
16. [Known Issues & Technical Debt](#16-known-issues--technical-debt)
17. [Appendix — Environment Variables](#17-appendix--environment-variables)

---

## 1. System Overview

MigrationBot is a conversational interface over S/4HANA WRICEF migration tracker spreadsheets in
Google Sheets. Users sign in with Google, open a WebSocket to a FastAPI backend, and issue
natural-language requests. An agentic LLM loop turns those into calls against a fixed nine-tool
catalogue.

**Reads go straight to the Google Sheets API on every request — there is no cache.** Writes are
queued through Redis to a separate worker container, which performs the mutation, writes the audit
row, and publishes the outcome back to the API over Redis pub/sub for delivery to the originating
client.

### 1.1 Component map

```
                        ┌──────────────────────────────────────────┐
   Browser              │  caddy  (:80/:443)                       │
     │                  │  route-ordered reverse proxy             │
     │  HTTPS / WSS     │  Caddyfile (route block)                 │
     └─────────────────▶│  /api/me     ──▶ backend:8000            │
                        │  /api/auth/* ──▶ frontend:3000 (NextAuth)│
                        │  /api/*      ──▶ backend:8000            │
                        │  /ws*        ──▶ backend:8000            │
                        │  *           ──▶ frontend:3000           │
                        └──────────┬───────────────────┬───────────┘
                                   │                   │
                  ┌────────────────▼──────┐   ┌────────▼─────────────────────┐
                  │ frontend  (Next.js)   │   │ backend  (FastAPI, uvicorn)  │
                  │ standalone output     │   │  GET  /api/health            │
                  │                       │   │  GET  /api/me                │
                  │ NextAuth v5 mints     │   │  GET  /api/projects          │
                  │ HS256 apiToken        │   │  /api/admin/*                │
                  │ auth.ts (session      │   │  WS   /ws                    │
                  │ callback)             │   └───┬──────────┬──────────┬────┘
                  └───────────────────────┘       │          │          │
                                                  │          │          │
                                        RBAC/audit│    writes│          │ LLM
                                                  │          │          │
                             ┌────────────────────▼──┐  ┌────▼──────┐   │
                             │ postgres:16           │  │ redis:7   │   │
                             │ users, projects,      │  │ write     │   │
                             │ permissions, sessions,│  │ queue +   │   │
                             │ audit_logs            │  │ pub/sub   │   │
                             └───────────▲───────────┘  └──┬─────▲──┘   │
                                         │        BLPOP    │     │      │
                                   audit │           ┌─────▼─────┴──┐   │
                                         └───────────┤ worker       │   │
                                                     │ (separate    │   │
                                                     │  container)  │   │
                                                     └──────┬───────┘   │
                                                            │           │
                          reads (direct) ───────────────────┼──────▶ Google Sheets API v4
                                                            │           │
                                                            ▼           ▼
                                                        writes      DeepSeek
```

**Two process boundaries matter.** `backend` and `worker` are separate containers from the same
image (`docker-compose.yml`, `backend`/`worker` services), so the worker cannot touch the API's
sockets — job outcomes travel back over a per-user Redis pub/sub channel (§5.1). And every read is
a synchronous round trip to Google, so read latency and Sheets quota are directly user-facing.

### 1.2 Technology inventory

| Layer | Technology | Citation |
|---|---|---|
| Frontend | Next.js 16.2.9, React 19.2.4, NextAuth 5.0.0-beta.31, Zustand 5, Tailwind 4, Recharts | `frontend/package.json` (`dependencies`) |
| Backend | FastAPI, uvicorn, SQLAlchemy 2.0 + asyncpg, Pydantic 2, python-jose | `backend/requirements.txt` |
| Data | PostgreSQL 16 — RBAC/audit only, **not** sheet data | `docker-compose.yml` (`postgres` service) |
| Queue | Redis 7 — list-backed FIFO **and** pub/sub event bus | `producer.py:enqueue_write_job`, `events.py:publish_queue_update` |
| LLM | `AsyncOpenAI` against `https://api.deepseek.com/v1` | `api/chat.py:llm_client` |
| Sheets | `google-api-python-client`, per-user OAuth `Credentials` | `sheets/client.py:build_sheets_service` |
| Proxy | Caddy 2-alpine, auto-TLS | `docker-compose.yml` (`caddy` service) |

---

## 2. Persistence Model

Five tables, all SQLAlchemy 2.0 `Mapped[...]` / `mapped_column(...)` on a shared `DeclarativeBase`
(`db/engine.py:Base`). Created at API startup by `init_db()` in the lifespan hook
(`main.py:lifespan`); failure is logged and swallowed. No migration tool exists — schema evolution
relies on `metadata.create_all` (`db/engine.py:init_db`).

| Table | Model | Notable columns |
|---|---|---|
| `users` | `models/user.py:User` | unique indexed `email`; nullable `google_sub`; `last_login` |
| `projects` | `models/project.py:Project` | unique `spreadsheet_id`; `default_tab`; `company_prefix`; `schema_config` JSONB default `{}` |
| `permissions` | `models/permission.py:Permission` | `role` default `"editor"`; `allowed_fields` default `["*"]`; `denied_operations` default `[]`; unique `(user_id, project_id)`; CHECK `role IN ('admin','editor','viewer')` |
| `sessions` | `models/session.py:Session` | UUID PK; `active_tab`; `project_id` `ON DELETE SET NULL` |
| `audit_logs` | `models/audit_log.py:AuditLog` | `old_value`/`new_value` Text; `args_json` JSONB; `result_ok`; generated `created_month` |

**Postgres holds no spreadsheet data.** Every WRICEF row is read live from Google Sheets. The
database exists for identity, authorisation, session state, and the audit trail only.

---

## 3. Configuration & Startup

`Settings` (`config.py:Settings`) reads `("../.env", ".env")` relative to the process working
directory and ignores unknown keys.

**Three variables are required with no default** — `DEFAULT_SPREADSHEET_ID`, `ADMIN_EMAILS`,
`CORS_ORIGINS` (all declared on `config.py:Settings`). A `ValidationError` on any of them is
converted to a `RuntimeError` naming the missing keys, at the `settings = Settings()`
module-level instantiation in `config.py`. This is a deliberate hardening change: these previously
fell back to hardcoded production values.

Secrets still carrying defaults: `DEEPSEEK_API_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
and `JWT_SECRET` (all on `config.py:Settings`).

> **⚠ This exact gap caused a live outage.** On 2026-08-06 the deployed `.env` had no
> `CORS_ORIGINS`, and because `settings` is built at module import (`config.py`, the
> `settings = Settings()` line), both `backend` and `worker` crash-looped — `docker ps` showed
> `Restarting (1)` on ~20-second cycles for roughly 21 hours before it was noticed, with
> `frontend`, `caddy`, `postgres`, and `redis` all healthy throughout, giving no visible signal
> that anything was wrong short of checking container status directly. Fixed operationally (the
> var added to the server's `.env`) and structurally: `.env.example` now documents all three
> required variables and ships a working `CORS_ORIGINS` value instead of the `*` it had before (a
> literal `*` also silently breaks credentialed cross-origin requests once combined with
> `allow_credentials=True` — see §16.1), and `docker-compose.yml`'s `backend` service now has a
> `healthcheck` against `GET /api/ready` (§10) so this class of failure shows as `unhealthy` in
> `docker compose ps` rather than requiring a log read to discover.

---

## 4. Authentication & Session Resolution

### 4.1 Token chain

1. **Google OAuth** — NextAuth requests `openid email profile
   https://www.googleapis.com/auth/spreadsheets` with `access_type: "offline"` and
   `prompt: "consent"` (`frontend/src/auth.ts`, `GoogleProvider` config).
2. **JWT callback** — first sign-in captures the Google access token, absolute expiry, and refresh
   token (`auth.ts:callbacks.jwt`). Later calls treat the token as fresh while
   `Date.now() < expiresAt - 5*60*1000`; otherwise `refreshGoogleAccessToken()` performs a refresh
   grant, preserving the existing refresh token when Google returns none.
3. **Backend token** — the `session` callback signs a fresh HS256 JWT on every session read
   (`auth.ts:callbacks.session`) carrying `email`, `name`, `picture`, `sub`,
   `google_access_token`, and `exp` = now + 24 h, exposed as `session.apiToken`.

> **⚠ Lifetime mismatch.** The backend JWT has a 24-hour `exp` (`auth.ts:callbacks.session`) but
> embeds a Google access token that lives about an hour. The WebSocket extracts that Google token
> exactly once at connect time (`chat.py:authenticate_ws_user`) and holds it for the connection's
> life, so a long-lived socket will present an expired Google credential — surfacing as a 401 from
> Google, not from FastAPI.

> **⚠ Refresh token never reaches the backend.** `auth.ts:callbacks.session` includes only
> `google_access_token`. Every Sheets client built on the WebSocket path is therefore constructed
> with `refresh_token=None` (`tool_dispatch.py:dispatch_tool`, `sheets/client.py:build_sheets_service`)
> and cannot self-refresh. Only the admin REST path passes one, from the `X-Google-Refresh-Token`
> header (`deps.py:get_google_auth`, and the `admin.py` routes that depend on it).

### 4.2 HTTP authentication

`get_current_user` (`deps.py:get_current_user`) decodes the bearer token with HS256 and requires
an `email` claim. On `JWTError` it falls back to a developer mode accepting any token beginning
`mock-` or containing `@`; otherwise 401. Unknown emails are **auto-provisioned**.

> **⚠ Production-reachable auth bypass.** The `mock-`/`@` fallback (`deps.py:get_current_user`,
> mirrored at `chat.py:authenticate_ws_user`) is not gated by any environment flag. Any string
> containing `@` that fails signature verification is accepted as that identity, and the account
> is created on the spot. Since admin status is decided purely by email membership
> (`admin.py:require_admin`), presenting the admin address as a bearer token reaches
> `require_admin`. `JWT_SECRET` also still has a shipped default (`config.py:Settings`) matching
> the frontend fallback (`auth.ts`, the `JWT_SECRET` constant), so a deployment that omits it signs
> with a publicly known key.

### 4.3 WebSocket authentication and project resolution

`authenticate_ws_user` (`chat.py:authenticate_ws_user`) mirrors the HTTP decoder but pulls
`google_access_token` from the payload and does **not** auto-provision — unknown email returns
`None` and the socket closes.

The socket is accepted *before* authentication, then closed with `1008` on failure (both in
`chat.py:websocket_chat_endpoint`). Project resolution is a three-step fallback, all in the same
function: the `project_id` query parameter; else the user's most recently active session's
project; else the first `is_active` project. All three failing closes the socket.

A `sessions` row is loaded or created with `active_tab` = `project.default_tab` or `"SD"`
(`chat.py:websocket_chat_endpoint`). Conversation history is **in-process only**, initialised
empty per connection and reassigned each turn — nothing persists chat history.

---

## 5. Data Flow Traces

### 5.1 Write path — `update_cell`, end to end

| # | Function | Location | Action |
|---|---|---|---|
| 1 | `websocket_chat_endpoint` | `chat.py:websocket_chat_endpoint` | receives frame, extracts `content` |
| 2 | `get_user_permissions` | `chat.py:websocket_chat_endpoint` → `permissions.py:get_user_permissions` | builds `PermissionChecker` |
| 3 | `run_agentic_loop` | `chat.py:websocket_chat_endpoint` → `agentic_loop.py:run_agentic_loop` | enters the LLM loop |
| 4 | LLM call | `agentic_loop.py:run_agentic_loop` | model returns `tool_calls` |
| 5 | `send_websocket_msg` | `agentic_loop.py:run_agentic_loop` | emits `tool_start` |
| 6 | `can_execute` | `agentic_loop.py:run_agentic_loop` → `permissions.py:PermissionChecker.can_execute` | RBAC gate; denial → `error` frame |
| 7 | `dispatch_tool` | `agentic_loop.py:run_agentic_loop` → `tool_dispatch.py:dispatch_tool` | routes by tool name |
| 8 | `build_sheets_service` | `tool_dispatch.py:dispatch_tool` | client built **with no refresh token** |
| 9 | `get_row_raw` | `tool_dispatch.py:dispatch_tool` → `read.py:get_row_raw` | pre-reads old values **live from Sheets** for the audit trail |
| 10 | `enqueue_write_job` | `tool_dispatch.py:dispatch_tool` → `producer.py:enqueue_write_job` | `RPUSH` onto `migrationbot:write_queue` |
| 11 | return | `tool_dispatch.py:dispatch_tool` | `{ok: true, status: "queued", job_id}` — **optimistic**, before Sheets is touched |
| 12 | `send_websocket_msg` | `agentic_loop.py:run_agentic_loop` | emits `tool_result` with that optimistic payload |
| — | *process boundary* | | |
| 13 | `start_worker` | `worker.py:start_worker` | `BLPOP`, 10 s timeout |
| 14 | `process_job` | `worker.py:process_job` | rehydrates `WriteJobPayload` |
| 15 | project lookup | `worker.py:process_job` | re-reads `schema_config` from Postgres |
| 16 | `build_sheets_service` | `worker.py:process_job` | rebuilds client from the token in the job |
| 17 | `update_cell` | `worker.py:process_job` → `write.py:update_cell` | the mutation |
| 18 | `find_row_num` | `write.py:update_cell` → `read.py:find_row_num` | scans the ID column live from Sheets |
| 19 | `get_header_row` | `write.py:update_cell` → `meta.py:get_header_row` | fetches headers |
| 20 | `resolve_column` | `write.py:update_cell` → `column_mapper.py:resolve_column` | maps field name to a real header |
| 21 | `_with_retry(batchUpdate)` | `write.py:update_cell` → `retry.py:_with_retry` | one `values().batchUpdate` |
| 22 | `_write_audit_record` | `worker.py:process_job` (`update_cell` branch) | one `audit_logs` row **per field** |
| 23 | `publish_queue_update` | `worker.py:process_job` → `events.py:publish_queue_update` | publishes terminal state to `migrationbot:queue_events:<email>` |
| 24 | `forward_queue_updates` | `chat.py:forward_queue_updates` | API's per-connection subscriber relays it as a `queue_update` frame |
| 25 | throttle | `worker.py:start_worker` | `asyncio.sleep(1.0)` before the next job |

Steps 23–24 are the completion-feedback path. `publish_queue_update` is called on **every**
terminal path in `process_job` — including the unsupported-tool branch (which sets `error_msg`)
and the exception handler — and a publish failure is swallowed so a successful write is never
failed by a notification problem (`events.py:publish_queue_update`). The API subscribes per
connection (`chat.py:websocket_chat_endpoint`, which spawns `forward_queue_updates` as a task) and
tears the task down in `finally`. Because the agentic loop and the relay both write to one socket,
sends are serialised behind an `asyncio.Lock` (`chat.py:websocket_chat_endpoint`, the `send_msg`
helper).

`update_cell` returns `{"ok": True, ...}` unconditionally once `batchUpdate` returns
(`write.py:update_cell`) — the API response body is not inspected.

### 5.2 Read path — `search_rows`

| # | Function | Location | Action |
|---|---|---|---|
| 1–7 | as the write path, through `dispatch_tool` | | |
| 8 | read branch | `tool_dispatch.py:dispatch_tool` | membership test against the 5-name read tuple |
| 9 | `search_rows` | `tool_dispatch.py:dispatch_tool` → `read.py:search_rows` | filters, `return_fields`, `limit` (default 20) |
| 10 | `get_header_row` | `read.py:search_rows` → `meta.py:get_header_row` | **API call 1** — headers, each cell `.strip()`ed |
| 11 | `col_idx` build | `read.py:search_rows` | `{h.lower().strip(): i}` — normalised on both sides, fixed in Phase 1 (§16 history) |
| 12 | `resolve_column` | `read.py:search_rows` | maps each filter term to a canonical name |
| 13 | mapping guard | `read.py:search_rows` | normalised canonical not in `col_idx` → returns `{"ok": false, "error": ...}` |
| 14 | bulk fetch | `read.py:search_rows` | **API call 2** — `{tab}!{start}:{start+2000}` |
| 15 | matching | `read.py:search_rows` | AND across filters; `blank` / `contains` / `exact` |
| 16 | truncation | `read.py:search_rows` | stops at `limit`; reports `capped` |

Two Sheets API calls per search, and no cache — every `search_rows`, `summarize`, and
`data_quality` invocation re-reads up to 2001 rows from Google (`read.py:search_rows`,
`read.py:summarize`, `read.py:run_data_quality_check` — each has its own bulk-range fetch).

---

## 6. RBAC Model

### 6.1 Two authorisation systems

- **Config admin** governs `/api/admin/*`: `require_admin` compares the caller's email against
  `settings.admin_emails_list` (`admin.py:require_admin`), re-read from `os.environ` per access
  (`config.py:Settings.admin_emails_list`).
- **Row role** governs tool execution: `permissions.role` ∈ `admin`/`editor`/`viewer`
  (`models/permission.py:Permission`).

They bridge one way only — a config admin short-circuits to `role="admin"` before any DB lookup
(`permissions.py:get_user_permissions`). A row-level `admin` gets no REST access.

### 6.2 Enforcement

Exactly one point: `checker.can_execute(...)` in the agentic loop before every dispatch
(`agentic_loop.py:run_agentic_loop`). `dispatch_tool` performs no check of its own
(`tool_dispatch.py:dispatch_tool`). Frontend admin gating is cosmetic (`chat/page.tsx` fetches
`/api/me`'s `is_admin`).

```python
READ_ONLY_TOOLS = {"get_row", "search_rows", "summarize", "switch_module", "data_quality"}
WRITE_TOOLS     = {"update_cell", "bulk_update", "format_row", "add_row"}
```
— `permissions.py:READ_ONLY_TOOLS`, `permissions.py:WRITE_TOOLS`.

| Role | Reads | Writes | Field limits | `denied_operations` |
|---|---|---|---|---|
| **Config admin** | all | all | none | not consulted |
| **Row admin** | all | all | none — short-circuits in `PermissionChecker.can_execute` | **not consulted** |
| **Editor** (incl. default) | all | all not in `denied_ops` | `update_cell`, `bulk_update` only, both in `PermissionChecker.can_execute` | honoured |
| **Viewer** | all | none (`PermissionChecker.can_execute`) | n/a | **not consulted** |

Field enforcement is skipped entirely when `allowed_fields == ["*"]` (`permissions.py:PermissionChecker.can_execute`),
and covers only those two tools — `add_row`'s free-form `fields` dict and `format_row` are ungated.

> **⚠ RBAC is fail-open.** `get_user_permissions` returns **editor with `["*"]`** when no
> `project_id` is given, when no `users` row matches, and when no `permissions` row exists (all in
> `permissions.py:get_user_permissions`). Combined with the WebSocket's "first active project"
> fallback (`chat.py:websocket_chat_endpoint`), a user who was never granted anything lands on an
> arbitrary project as an editor.

> **⚠ `WRITE_TOOLS` is never read.** Defined at `permissions.py:WRITE_TOOLS` but `can_execute` only
> tests `READ_ONLY_TOOLS` (`permissions.py:PermissionChecker.can_execute`). Classification is by
> exclusion, so a new read tool omitted from that set is silently denied to viewers.

---

## 7. Schema & Column Resolution

### 7.1 `schema_config` shape

JSONB defaulting to `{}` (`models/project.py:Project`), in one of two shapes distinguished by a
top-level `"tabs"` key: multi-tab (`{"tabs": {...}, "global": {...}}`, what `detect_all_tabs`
produces — `schema_detect.py:detect_all_tabs`) or flat. The disambiguation is reimplemented in
`read.py:_get_tab_schema`, `write.py:update_cell`, `write.py:bulk_update`, `write.py:add_row`,
`worker.py:process_job`, `chat.py:websocket_chat_endpoint`, and `agentic_loop.py:run_agentic_loop`
— seven copies, one of them (`read.py`) a private `_get_tab_schema` function the others don't share.

Per-tab defaults are applied inline at each use: `data_start_row` → `3`, `primary_id_position` →
`"B"`, `primary_id_column` → `"RICEFW ID"`, `assignee_column` → `"Technical Resource "` (with
trailing space), `critical_fields` → a six-name list (`read.py:search_rows`, the default list
literal). `header_row_num` is always `data_start_row - 1`.

### 7.2 Column-name resolution and the whitespace asymmetry

`resolve_column(term, column_map)` (`column_mapper.py:resolve_column`) resolves in three stages:
exact match on canonical keys, alias-list match (both compared case-insensitively and stripped),
then `difflib.get_close_matches` at `cutoff=0.6`. It returns the **unstripped** canonical key.

`COLUMN_ALIASES` (`column_mapper.py:COLUMN_ALIASES`) deliberately preserves the tracker's real
header typos and trailing spaces — `"Technical Resource "`, `"Functinal Resource "`, `"Color "`,
`"Programe Name"`.

`get_header_row` strips every header cell (`meta.py:get_header_row`). Both read and write paths
now treat that the same way: build `{h.lower().strip(): i}` and look up
`canonical.lower().strip()`.

- **Write path**: `write.py:update_cell`, `write.py:bulk_update`.
- **Read path**: `read.py:search_rows` and `read.py:summarize` plus its `_col()` helper (used by
  all four report branches). Previously these built `{h: i}` and tested `canonical in col_idx`
  verbatim, which missed any canonical name carrying trailing whitespace — fixed in the Phase 1
  remediation; regression tests in `test_sheets_logic.py` cover both `search_rows` and
  `summarize.count_by_field` against a header with a real trailing space.

### 7.3 Auto-detection

`detect_all_tabs` (`schema_detect.py:detect_all_tabs`) enumerates tabs, reads `A1:Z10` from each,
and asks `deepseek-chat` at `temperature=0.1` to classify the tab and map columns. Non-tracker tabs
are skipped; `data_start_row` = `header_row_index + 2`. On LLM failure a hard-coded fallback is
returned with `is_tracker_sheet: True` — so an outage registers every tab, including cover pages,
as a tracker.

`build_column_map` (`column_mapper.py:build_column_map`), a two-pass LLM alias generator, is never
called from anywhere in `backend/`; `detect_all_tabs` writes no `column_map` key. Every deployment
therefore runs on the static `COLUMN_ALIASES` unless an admin hand-edits `schema_config`.

---

## 8. The Agentic Loop

`run_agentic_loop` (`agentic_loop.py:run_agentic_loop`) drives at most **8** iterations
(`max_iterations` parameter).

- **Model routing** — `select_model` returns `deepseek-reasoner` only on iteration 0 and only when
  the latest user message contains a conditional keyword
  (`llm_router.py:select_model` → `llm_router.py:has_conditional_logic`); everything else uses
  `deepseek-chat`.
- **Prompt swapping** — iteration 0 sends the full prompt with the serialised column map; from
  iteration 1 the system message is replaced in place with a compact variant
  (`tool_schemas.py:get_system_prompt_compact`).
- **DSML leakage guard** — content containing `<｜｜DSML｜｜>` triggers one retry against
  `deepseek-chat`, inline in `agentic_loop.py:run_agentic_loop`.
- **CoT suppression** — `reasoning_content` is logged, never forwarded.
- **Self-repair** — a failed tool result gets a `[System Recovery Note]` appended.
- **Honest queued-write note** — a result with `status: "queued"` gets a `[System Note]` telling
  the model not to claim the write is done — added in Phase 3, since it has to live in the tool
  message content, not just the system prompt, to survive the iteration-1+ prompt swap above.
- **Termination** — no `tool_calls` → emit `assistant` with `done: true`, break; any exception →
  emit `error`, break; iteration cap exhausted without either → the `for` loop's `else:` clause
  emits `error` — added in Phase 1 — previously this path sent nothing, leaving the client's
  pre-seeded empty assistant bubble spinning indefinitely. All four cases live in
  `agentic_loop.py:run_agentic_loop`.

### 8.1 Tool catalogue

Nine tools in `core/tool_schemas.py:TOOLS`: `get_row`, `update_cell`, `format_row`, `add_row`,
`bulk_update`, `search_rows`, `summarize`, `switch_module`, `data_quality`.

---

## 9. WebSocket Protocol Reference

Endpoint `WS /ws?token=<apiToken>[&project_id=<int>]` (`chat.py:websocket_chat_endpoint`). The
client derives the URL from `window.location` unless `NEXT_PUBLIC_WS_URL` is set
(`useWebSocket.ts:connect`); `docker-compose.yml` (`frontend` service env) leaves it empty, so the
window-derived value is used.

### 9.1 Client → server

The server routes on the frame's declared `type` (`chat.py:websocket_chat_endpoint`) rather than
only ever reading `content` — a non-JSON or untyped frame is normalized to
`{"type": "message", "content": <raw text>}` before dispatch.

| `type` | Payload | Sent by | Server handling |
|---|---|---|---|
| `message` | `{type, content}` | `useWebSocket.ts:sendMessage` | drives `run_agentic_loop`, both in `chat.py:websocket_chat_endpoint` |
| `ping` | `{type: "ping"}` | `useWebSocket.ts:connect` (heartbeat interval), every 30 s | replies `pong`, in `chat.py:websocket_chat_endpoint` |
| `switch_tab` | `{type, tab_name}` | `useWebSocket.ts:switchTab`, on tab-button click | RBAC-checked (`can_execute("switch_module", {})`), then `switch_module` verifies the tab exists live against Sheets and updates `sessions.active_tab` — both in `chat.py:websocket_chat_endpoint` → `meta.py:switch_module` |

Any other `type` is ignored rather than misread as chat text (`chat.py:websocket_chat_endpoint`).

### 9.2 Server → client

| `type` | Payload | Emitted by | Client consumer | Effect |
|---|---|---|---|---|
| `connection_ok` | `{type, user_email, project_name, active_tab}` | `chat.py:websocket_chat_endpoint` | `useWebSocket.ts:connect` (`onmessage`) | `setSessionInfo` applies all three fields |
| `assistant` | `{type, content, done: true}` | `agentic_loop.py:run_agentic_loop` | `useWebSocket.ts:connect` (`onmessage`) | appends to the most recent assistant message |
| `tool_start` | `{type, tool, args}` | `agentic_loop.py:run_agentic_loop` | `useWebSocket.ts:connect` (`onmessage`) | pushes `{name, args, status:"running"}` |
| `tool_result` | `{type, tool, result}` | `agentic_loop.py:run_agentic_loop` | `useWebSocket.ts:connect` (`onmessage`) | marks the matching running entry `"completed"` or `"failed"` per `result.ok` |
| `tab_switched` | `{type, active_tab}` | `chat.py:websocket_chat_endpoint` | `useWebSocket.ts:connect` (`onmessage`) | `setActiveTab(active_tab)` — the client no longer sets it optimistically (§14) |
| `error` | `{type, message}` | `chat.py:authenticate_ws_user`, `chat.py:websocket_chat_endpoint` (multiple sites incl. the `switch_tab` handler and the top-level exception handler); `agentic_loop.py:run_agentic_loop` (RBAC denial, exception, iteration-cap exhaustion) | `useWebSocket.ts:connect` (`onmessage`) | appends a `system` message, deduped against an identical predecessor |
| `pong` | `{type: "pong"}` | `chat.py:websocket_chat_endpoint` | `useWebSocket.ts:connect` (`onmessage`) | no-op |
| `queue_update` | `{type, job_id, status, tool_name, args, session_id, error}` | `events.py:publish_queue_update` via `worker.py:process_job`, relayed by `chat.py:forward_queue_updates` | `useWebSocket.ts:connect` (`onmessage`) → DOM `CustomEvent` → `chat/page.tsx` (`queue_update` listener) | toast keyed on `status` |

`status` is `"completed"` or `"failed"` (`worker.py:process_job`); the frontend also handles a
generic "other" branch (`chat/page.tsx`, the `queue_update` listener), which nothing currently
emits.

`updateLastMessage` targets by explicit `targetId` or most-recent-role rather than always the final
element (`useChatStore.ts:updateLastMessage`), so interleaved `assistant` and `tool_*` frames no
longer corrupt each other.

### 9.3 Lifecycle

Reconnect after 3 s for any close code other than `1008` or `1000` (`useWebSocket.ts:connect`,
`onclose` handler). Heartbeat every 30 s (`useWebSocket.ts:connect`, `onopen` handler). No
server-side idle handling — `chat.py:websocket_chat_endpoint`'s `receive_text()` blocks
indefinitely. `connect` tracks the live socket in a ref (`wsRef`, `useWebSocket.ts`) rather than
the Zustand `ws` state, so a reconnect always closes the actual current socket regardless of which
render's closure is running (§14).

---

## 10. REST Surface

Admin routes (all gated by `require_admin`, `admin.py:require_admin`):

| Method | Path | Purpose | Citation |
|---|---|---|---|
| GET | `/admin/projects` | list all projects | `admin.py:list_projects` |
| POST | `/admin/projects/detect-metadata` | LLM tab detection, no persist | `admin.py:detect_project_metadata` |
| POST | `/admin/projects` | create; auto-detects `schema_config` when omitted | `admin.py:create_project` |
| PUT | `/admin/projects/{id}` | patch fields incl. raw `schema_config` | `admin.py:update_project` |
| DELETE | `/admin/projects/{id}` | delete, cascades to permissions | `admin.py:delete_project` |
| PATCH | `/admin/projects/{id}/fields` | toggle a `critical_fields` entry | `admin.py:toggle_project_field` |
| GET/POST/DELETE | `/admin/permissions[/{id}]` | RBAC CRUD; POST creates the user if absent | `admin.py:list_permissions`, `admin.py:upsert_permission`, `admin.py:delete_permission` |
| GET | `/admin/audits` | filter by user/tool/RICEFW ID, limit 100 | `admin.py:list_audits` |
| GET | `/admin/analytics/summary` | counts and failure totals | `admin.py:get_analytics_summary` |

Non-admin: `GET /api/health` (`health.py:health_check`, liveness — static literal, no dependency
probing), `GET /api/ready` (`health.py:readiness_check`, added in Phase 0 of the remediation plan
— live `SELECT 1` against Postgres and `PING` against the shared `producer.redis_client`, `503`
with a per-service `detail` dict when either fails; verified against unreachable dependencies),
`GET /api/me` (`api/auth.py:get_current_profile`, mounted directly in `main.py`), and
`GET /api/projects` (`chat.py:list_user_projects`).

`docker-compose.yml`'s `backend` service now runs a `healthcheck` against `/api/ready` (interval
30 s, 3 retries, `start_period` 15 s) — see §3.

> **⚠ `/api/projects` performs no authorisation filtering.** Every authenticated caller receives
> all active projects including `spreadsheet_id` and full `schema_config`
> (`chat.py:list_user_projects`). With the fail-open default (§6.2) and the verbatim `project_id`
> query parameter (`chat.py:websocket_chat_endpoint`), a user can select any project from that
> list and operate on it as an editor.

---

## 11. Google Sheets Integration

`build_sheets_service(access_token, refresh_token=None)` (`sheets/client.py:build_sheets_service`)
builds `Credentials` with the process client ID/secret and disables discovery caching. There is no
service account — every call is made as the signed-in user, which is what makes audit attribution
real.

`_with_retry(fn)` (`retry.py:_with_retry`) is the sole sync bridge: it dispatches onto a
module-level `ThreadPoolExecutor(max_workers=4)` (`retry.py:_executor`) and retries with
exponential backoff (base 1 s, doubling) on HTTP `{429, 500, 503}` for up to 4 attempts. Other
errors propagate.

| Function | File | Sheets calls |
|---|---|---|
| `get_header_row` | `meta.py:get_header_row` | 1 — strips every cell |
| `get_sheet_id` | `meta.py:get_sheet_id` | 1 |
| `find_row_num` | `read.py:find_row_num` | 1 — full ID-column scan |
| `get_row` | `read.py:get_row` | 3 — `find_row_num` + row fetch + headers |
| `get_row_raw` | `read.py:get_row_raw` | 3 — same shape |
| `search_rows` | `read.py:search_rows` | 2 — headers + ≤2001 rows |
| `summarize` | `read.py:summarize` | 2 |
| `run_data_quality_check` | `read.py:run_data_quality_check` | 2 (+1 or +2 for `consistency`/`stale` DB reads) |
| `update_cell` | `write.py:update_cell` | 3 — `find_row_num` + headers + one `batchUpdate` |
| `bulk_update` | `write.py:bulk_update` | 2 + **one `find_row_num` per target ID** |
| `add_row` | `write.py:add_row` | 1 + headers, `values().append` |
| `format_row` | `format.py:format_row` | `spreadsheets().batchUpdate` `repeatCell` |

`next_ricefw_id` (`meta.py:next_ricefw_id`) computes max+1 over parsed IDs, formatted `%03d`;
`switch_module` (`meta.py:switch_module`) optionally verifies the tab exists then updates
`sessions.active_tab`.

> **⚠ Compounding read cost on bulk paths.** `get_bulk_rows_raw` (`read.py:get_bulk_rows_raw`)
> calls `get_row_raw` once per target ID, and each of those is three Sheets calls — so the audit
> pre-read alone costs ~3N calls, before `bulk_update` then spends another N on `find_row_num`
> (`write.py:bulk_update`). A 50-row bulk update is on the order of 200 API calls against a
> quota-limited endpoint, throttled at one job/second by the worker.

---

## 12. Audit Logging

`audit_logs` rows are written only by `_write_audit_record` (`audit.py:_write_audit_record`),
which opens its own `AsyncSessionLocal` rather than joining the caller's transaction and swallows
every exception so an audit failure cannot fail a mutation. Its only caller is the worker
(`worker.py:process_job`).

| Tool | Rows | Citation |
|---|---|---|
| `update_cell` | one per updated field | `worker.py:process_job` (`update_cell` branch) |
| `bulk_update` | one per succeeded **and** per failed ID | `worker.py:process_job` (`bulk_update` branch, two loops) |
| `format_row` | one, `field="Color"` | `worker.py:process_job` (`format_row` branch) |
| `add_row` | one, `field="ID"` | `worker.py:process_job` (`add_row` branch — ID always server-computed via `meta.py:next_ricefw_id`, `prefix=None`; the schema exposes no `ricefw_id`/`prefix` arg) |
| exception | one, `field="Mutation"` | `worker.py:process_job` (top-level `except`) |

`old_value` comes from the live pre-read at dispatch time (`tool_dispatch.py:dispatch_tool`).
Read-only tools produce no audit rows.

`log_audit` (`audit.py:log_audit`), a fire-and-forget `create_task` wrapper, is never called.

---

## 13. Data Quality Engine

`DataQualityChecker` (`core/data_quality.py:DataQualityChecker`) takes headers, a row matrix, and
a tab schema, building a case-insensitive header index at construction and resolving schema-named
columns through `_get_col_idx`.

| Method | Behaviour |
|---|---|
| `blank_field_counts` | blank count per named column; missing column counts as fully blank |
| `stale_items` | latest mutation per ID from audit rows, excluding done statuses; no history → `"Never (no logs)"`, `days_inactive: 999` |
| `consistency_checks` | four rules — completed-without-signoff, completed-without-completion-date, required-with-blank-status, assignee-not-in-known-emails |
| `completeness_score` | fill rate across `critical_fields`; `100.0` when no rows or no resolvable columns |

All four are methods on `core/data_quality.py:DataQualityChecker`.

`run_data_quality_check` (`read.py:run_data_quality_check`) reads headers and ≤2001 rows live,
then dispatches on the **required** `check_type` arg (`blank_fields` / `consistency` / `stale` /
`completeness_score` / `all`) rather than always running every check — each branch only does the
work (and, for `consistency`/`stale`, the DB queries) its check needs. `scope_module` filters
`rows` to one module before the checker is built; `blank_fields` reads `fields` from args (default:
the same six-column list `search_rows` defaults to) and calls the previously-dead
`DataQualityChecker.blank_field_counts`. `consistency` sources "known emails" from
`SELECT DISTINCT audit_logs.user_email` (`read.py:run_data_quality_check`); `stale` pulls audit
timestamps scoped to the spreadsheet and tab, also in `read.py:run_data_quality_check`, using
`threshold_days` (the schema's actual arg name — the code previously read a
`stale_threshold_days` key the schema never sends).

> **⚠ "Registered users" is drawn from the audit log**, not `users` or `permissions`
> (`read.py:run_data_quality_check`) — a legitimate user who has never performed a write is
> reported as unregistered.

---

## 14. Frontend Application

Next.js 16 App Router, `output: "standalone"` (`next.config.ts`), `SessionProvider` at the root
layout.

| Route | File | Purpose |
|---|---|---|
| `/` | `app/page.tsx` | landing, `signIn` |
| `/chat` | `app/chat/page.tsx` | main chat UI |
| `/admin` | `app/admin/page.tsx` | dashboard; four parallel fetches, Recharts |
| `/admin/projects` | `app/admin/projects/page.tsx` | project CRUD + tab detection; sends Google token headers |
| `/admin/users` | `app/admin/users/page.tsx` | permission upsert/delete |
| `/admin/audit` | `app/admin/audit/page.tsx` | filtered audit browser |

State lives in one Zustand store (`useChatStore.ts:useChatStore`) holding `projects`,
`activeProject`, `activeTab`, `isConnected`, `messages`, `ws`, and session metadata.

Tab switching is an explicit control frame, not prompt-driven (`chat/page.tsx:handleTabChange`):
the client sends `{type: "switch_tab", tab_name}` and only applies the new `activeTab` once the
server confirms with `tab_switched` (§9.1–9.2) — it no longer sets `activeTab` optimistically
before the switch is known to have succeeded.

`connect` tracks the live socket in a ref (`wsRef`, `useWebSocket.ts:useWebSocket`) rather than
reading the Zustand `ws` state through its own closure, so a reconnect from `onclose`'s
`setTimeout` or an effect re-run always closes the actual current socket — not a value captured at
whatever render created that particular `connect` closure.

---

## 15. Deployment, CI/CD & Tests

### 15.1 Compose topology

| Service | Build | Command | Ports |
|---|---|---|---|
| `postgres` | `postgres:16` | default | `5433:5432` |
| `redis` | `redis:7-alpine` | default | `6379:6379` |
| `backend` | `./backend` | `uvicorn app.main:app --host 0.0.0.0 --port 8000` | `8000:8000` |
| `worker` | `./backend` | `python -m app.queue.worker` | none |
| `frontend` | `./frontend` | `node server.js` | `3000:3000` |
| `caddy` | `caddy:2-alpine` | default | `80:80`, `443:443` |

`backend` and `worker` both load `.env` via `env_file` (`docker-compose.yml`, both services) with
`DATABASE_URL`/`REDIS_URL` overridden to compose service names. Volumes: `pgdata`, `caddy_data`,
`caddy_config` — **Redis has none**, so the write queue is memory-only.

Caddy routes in order (`Caddyfile`, the `route` block): `/api/me` → backend (carved out before the
NextAuth wildcard), `/api/auth/*` → frontend, `/api/*` → backend, `/ws*` → backend, `*` → frontend.

### 15.2 CI/CD

`ci.yml` runs two jobs on push/PR to `main`/`develop`: `lint-and-test` (Postgres + Redis service
containers, `ruff check backend/app` — scoped to `E9,F` in `backend/pyproject.toml`'s
`[tool.ruff.lint]` table, since a first-time broad rule set surfaced ~300 pre-existing findings
unrelated to any given change — then `pytest -v` from `backend/` with coverage reported via
`pytest-cov`, `DATABASE_URL` pointed at `migrationbot_test`, and
`CORS_ORIGINS`/`ADMIN_EMAILS`/`DEFAULT_SPREADSHEET_ID` supplied) and `frontend-build` (`npm ci`,
`npm run lint` — advisory only, `continue-on-error: true`, since the frontend carries ~49
pre-existing lint errors of its own — then `npm run build`). No coverage floor is enforced yet;
the first run's own numbers are the honest baseline to ratchet from.

`deploy.yml` no longer triggers on push directly — it's a `workflow_run` gated on `ci.yml`
("CI Pipeline") completing with `conclusion == 'success'` on `main`, so a push straight to main
that bypasses a PR (or that CI would have failed) can no longer deploy. The SSH script then
`git fetch origin main && git reset --hard origin/main`, `docker compose build` (no `--no-cache`
— `requirements.txt` has been exact-pinned since Phase 0, so a cached build is just as
reproducible), `up -d`, then verifies the deploy: checks `docker compose ps` for any container
stuck `Restarting`, polls `GET /api/ready` for up to 75 s, and `exit 1`s with the last 80 log
lines on either failure — closing the exact gap that let the 2026-08-06 CORS_ORIGINS outage run
unnoticed for ~21 hours (§3). `set -e` is now on the whole remote script, so a failed `git reset`
no longer silently continues into deploying stale code.

> **⚠ Both an SSH key and a password are still passed** (`deploy.yml`, the `key` and `password`
> inputs to `appleboy/ssh-action`) where one should do. Left alone here rather than guessed at,
> since removing the wrong one would break the next deploy with no local way to verify which
> credential is actually configured in this repo's secrets.

`.env` itself survives redeploys — it is gitignored (`.gitignore`) and `git reset --hard` does not
touch ignored files — but any *other* untracked file in the repo directory is discarded silently.

### 15.3 Tests

28 tests across `tests/test_db.py` (4), `tests/test_core/test_core_logic.py` (8),
`tests/test_sheets/test_sheets_logic.py` (12), `tests/integration/test_integration_logic.py` (4).
`test_db.py` and the integration suite each define their own `setup_*` and `db_session` fixtures
locally, both autouse.

**Both are now gated (Phase 0).** `require_test_database()` (`tests/conftest.py:require_test_database`)
parses the database name out of `settings.DATABASE_URL` and raises `RuntimeError` unless it
contains `"test"` (case-insensitive) — the convention `ci.yml` already uses (`migrationbot_test`).
It is called from `test_db.py:setup_test_db` and `test_integration_logic.py:setup_integration_db`
before either touches `drop_db()`. Verified directly: pointing `DATABASE_URL` at
`.../migrationbot` — the exact shape of this repo's real `.env` — is refused with a clear error
rather than attempting a connection; pointing it at `.../migrationbot_test` collects and runs
normally.

`requirements.txt` is now pinned to exact versions (Phase 0) rather than `>=` floors, and `pandas`
— unused anywhere in `backend/` — was dropped.

---

## 16. Known Issues & Technical Debt

Ordered by severity. Items marked **(verified)** were reproduced by executing code. Items resolved
by the remediation plan are removed from this list, not annotated — see git history for what
changed and when.

### 16.1 CORS wildcard with credentials
**Severity: medium.** `main.py` (the `CORSMiddleware` registration) sets `allow_credentials=True`
with origins split from `CORS_ORIGINS`. `.env.example` ships `CORS_ORIGINS=*`. Starlette does not
expand a literal `"*"` here, so credentialed cross-origin requests fail rather than being
permitted — and the example file steers deployments toward exactly that value.

### 16.2 `/api/projects` performs no authorisation filtering
**Severity: medium.** `chat.py:list_user_projects` — see §10.

### 16.3 RBAC is fail-open
**Severity: medium.** `permissions.py:get_user_permissions` — see §6.2.

### 16.4 Auth bypass and default `JWT_SECRET`
**Severity: medium.** `deps.py:get_current_user`, `chat.py:authenticate_ws_user`,
`config.py:Settings` — see §4.2.

### 16.5 Queue has no durability, retry, or dead-letter path
**Severity: medium.** A plain Redis list (`producer.py:enqueue_write_job`) on a volume-less
container (`docker-compose.yml`, `redis` service). `BLPOP` removes the job before processing
(`worker.py:start_worker`), so a crash mid-`process_job` loses the write with no record beyond an
audit row that is only written if the exception was caught (`worker.py:process_job`, the
top-level `except`), not if the process dies. No retry, no dead-letter list, and no job-state key
— the `job_id` returned to the user (`tool_dispatch.py:dispatch_tool`) is stored nowhere and
cannot be queried after the fact.

### 16.6 OAuth access tokens are serialised into the queue
**Severity: medium.** `WriteJobPayload` (`queue/schemas.py:WriteJobPayload`) carries
`google_access_token` as a plain field, JSON-serialised into the Redis entry
(`producer.py:enqueue_write_job`) so the worker can rebuild a client
(`worker.py:process_job`). Live user credentials sit in a Redis instance with no auth and port
6379 published to the host (`docker-compose.yml`, `redis` service) for as long as the job is
queued. Inherent to the OAuth-only design plus the queue boundary; noted, not solved.

### 16.7 Silent 2001-row ceiling on every scan
**Severity: low-medium.** `read.py:search_rows`, `read.py:summarize`, and
`read.py:run_data_quality_check` each request `{start}:{start+2000}`. A tracker larger than that
is silently truncated — `search_rows` reports fewer matches, `summarize` percentages are computed
over a partial denominator, and `data_quality` scores a subset, all with no indication to the
user.

### 16.8 Seven copies of the schema-shape branch
**Severity: low (maintenance).** `read.py:_get_tab_schema`, `write.py:update_cell`,
`write.py:bulk_update`, `write.py:add_row`, `worker.py:process_job`,
`chat.py:websocket_chat_endpoint`, `agentic_loop.py:run_agentic_loop`. Per-key defaults are
similarly scattered.

### 16.9 Dead code
**Severity: low.** `core/planner.py` and `core/memory.py` (never imported);
`column_mapper.py:build_column_map` and `column_mapper.py:get_column_map_json` (never called —
§7.3); `audit.py:log_audit`; `permissions.py:WRITE_TOOLS`; `permissions.py:PermissionChecker.is_admin`;
`meta.py:_detect_header_row` (defined, never called from anywhere — the one unused import of it,
in `read.py`, was itself dead and has been removed); `models/audit_log.py:AuditLog.created_month`
(no partitioning exists).

### 16.10 Startup `init_db()` failure is non-fatal, and readiness doesn't fully cover it
**Severity: low.** `init_db()` failures are caught and execution continues
(`main.py:lifespan`). `GET /api/ready` (`health.py:readiness_check`, added in Phase 0) narrows
this but does not close it: it runs a live `SELECT 1`, which succeeds against a reachable Postgres
regardless of whether `init_db()` ever created the application's tables — `SELECT 1` needs no
table. So the probe catches "Postgres is down" (which is what caused the 2026-08-06 outage — see
§3) but not "Postgres is up with an empty schema". Closing that gap needs `main.py:lifespan` to
record `init_db()`'s outcome and have `health.py:readiness_check` report unready when it failed.

---

## 17. Appendix — Environment Variables

| Variable | Consumed by | Default |
|---|---|---|
| `DATABASE_URL` | `db/engine.py` (module-level engine creation) | `…@localhost:5433/migrationbot` (`config.py:Settings`) |
| `REDIS_URL` | `producer.py:enqueue_write_job`, `events.py:publish_queue_update`, `worker.py:start_worker`, `chat.py:forward_queue_updates` | `redis://localhost:6379` (`config.py:Settings`) |
| `DEEPSEEK_API_KEY` | `chat.py:llm_client` | `"mock-deepseek-key"` (`config.py:Settings`) |
| `GOOGLE_CLIENT_ID` / `_SECRET` | `sheets/client.py:build_sheets_service`, `auth.ts` (`GoogleProvider` config) | mock values (`config.py:Settings`) |
| `JWT_SECRET` | `deps.py:get_current_user`, `chat.py:authenticate_ws_user`, `auth.ts` (the `JWT_SECRET` constant) | `"mock-jwt-secret-…"` (`config.py:Settings`) |
| **`CORS_ORIGINS`** | `main.py` (`CORSMiddleware` registration) | **required — no default** (`config.py:Settings`) |
| **`ADMIN_EMAILS`** | `config.py:Settings.admin_emails_list` | **required — no default** (`config.py:Settings`) |
| **`DEFAULT_SPREADSHEET_ID`** | declared `config.py:Settings` | **required — no default**; referenced nowhere in `backend/app/` |
| `DEFAULT_SHEET_TAB` / `_LABEL` | declared `config.py:Settings` | defaults present; referenced nowhere in `backend/app/` |
| `NEXTAUTH_SECRET` / `NEXTAUTH_URL` | `auth.ts`, `docker-compose.yml` (`frontend` service env) | secret falls back to `JWT_SECRET` |
| `DB_PASSWORD` | `docker-compose.yml` (`postgres`/`backend`/`worker` services) | none — compose only |
| `NEXT_PUBLIC_WS_URL` | `useWebSocket.ts:connect` | empty in compose (`docker-compose.yml`, `frontend` service env) |
| `VPS_HOST` / `VPS_USER` / `VPS_SSH_KEY` / `VPS_PASSWORD` | `deploy.yml` (`appleboy/ssh-action` inputs) | GitHub secrets |
