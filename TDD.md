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
| Frontend | Next.js 16.2.9, React 19.2.4, NextAuth 5.0.0-beta.31, Zustand 5, Tailwind 4, Recharts, react-markdown 10 + remark-gfm 4 | `frontend/package.json` (`dependencies`) |
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

**Four variables are required with no default** — `DEFAULT_SPREADSHEET_ID`, `ADMIN_EMAILS`,
`CORS_ORIGINS`, and (since Phase 4) `JWT_SECRET` (all declared on `config.py:Settings`). A
`ValidationError` on any of them is converted to a `RuntimeError` naming the missing keys, at the
`settings = Settings()` module-level instantiation in `config.py`. This is a deliberate hardening
change: these previously fell back to hardcoded production values — `JWT_SECRET`'s fallback was
the specific problem, since `frontend/src/auth.ts` independently fell back to the identical
hardcoded string, so a deployment that forgot to set it on either side still verified tokens
correctly using the shared public default (§4.2).

Secrets still carrying defaults: `DEEPSEEK_API_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`
(all on `config.py:Settings`).

`main.py` also refuses to boot if `CORS_ORIGINS` contains a literal `"*"`, since that's
incompatible with the `allow_credentials=True` the app needs for cookie/Authorization-header
cross-origin requests — Starlette silently fails those instead of permitting them, so this is
now a startup error instead of a runtime surprise (§16 history).

> **⚠ This exact gap caused a live outage.** On 2026-08-06 the deployed `.env` had no
> `CORS_ORIGINS`, and because `settings` is built at module import (`config.py`, the
> `settings = Settings()` line), both `backend` and `worker` crash-looped — `docker ps` showed
> `Restarting (1)` on ~20-second cycles for roughly 21 hours before it was noticed, with
> `frontend`, `caddy`, `postgres`, and `redis` all healthy throughout, giving no visible signal
> that anything was wrong short of checking container status directly. Fixed operationally (the
> var added to the server's `.env`) and structurally: `.env.example` now documents all required
> variables and ships a working `CORS_ORIGINS` value instead of the `*` it had before (now also
> rejected outright at startup, above), and `docker-compose.yml`'s `backend` service now has a
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
3. **Backend token** — `withApiToken()` (`auth.ts:withApiToken`) mints an HS256 JWT carrying
   `email`, `name`, `picture`, `sub`, `google_access_token`, `google_refresh_token`, and
   `exp` = now + 24 h, and caches it on the NextAuth token; the `session` callback
   (`auth.ts:callbacks.session`) exposes that cached value as `session.apiToken`.

Minting lives in the `jwt` callback rather than `session` deliberately. `session` runs on **every**
session read and `exp` was computed from `Date.now()`, so each read produced a different `exp` → a
different signature → a different string. `SessionProvider` refetches on window focus and
`session.apiToken` feeds the chat socket (`useWebSocket.ts:useWebSocket`), so alt-tabbing back into
the tab rebuilt the connection and silently discarded the per-connection `message_history` (§4.3).
The cache has to sit on the `jwt` callback's return value because only that is re-encoded into the
session cookie — a field set in `session` would not survive to the next request. `withApiToken`
re-mints only when there is no cached token, when it is within an hour of its 24 h expiry, or when
the embedded Google access token has rotated (detected by comparing `apiTokenIssuedFor` against
`googleAccessTokenExpires`, which `refreshGoogleAccessToken()` rewrites on every success — using
that number as a generation marker avoids stashing a third copy of the access token in the cookie).
A Google rotation therefore still costs one reconnect, roughly hourly, since the backend should
receive a live access token; that replaces a reconnect on every window focus.

The backend JWT has a 24-hour `exp` (`auth.ts:withApiToken`) but embeds a Google access token
that lives about an hour. The WebSocket extracts both tokens once at connect time
(`chat.py:authenticate_ws_user`) and holds them for the connection's life — but as of Phase 4,
`auth.ts:withApiToken` signs `google_refresh_token` into the payload alongside
`google_access_token`, threaded through `chat.py:websocket_chat_endpoint` →
`agentic_loop.py:run_agentic_loop` → `tool_dispatch.py:dispatch_tool` →
`sheets/client.py:build_sheets_service`, and into `queue/schemas.py:WriteJobPayload` for queued
writes the worker picks up later. `Credentials(refresh_token=...)` self-refreshes on an expired
access token, so a long-lived socket or a delayed queued write no longer needs the original,
possibly-stale `google_access_token` to still be valid. Previously this path was always built with
`refresh_token=None` — only the admin REST path (`deps.py:get_google_auth`, via the
`X-Google-Refresh-Token` header) had one.

### 4.2 HTTP authentication

`get_current_user` (`deps.py:get_current_user`) decodes the bearer token with HS256 and requires
an `email` claim. On `JWTError` it falls back to a developer mode accepting any token beginning
`mock-` or containing `@`; otherwise 401. Unknown emails are **auto-provisioned**.

As of Phase 4, that fallback (`deps.py:get_current_user`, mirrored at
`chat.py:authenticate_ws_user`) only fires when `settings.ALLOW_DEV_AUTH` is true
(`config.py:Settings`, default `False`) — previously it was reachable in any deployment, so any
string containing `@` that failed signature verification was accepted as that identity, including
the admin address (`admin.py:require_admin` decides admin status purely by email membership).
`JWT_SECRET` is now required with no default on both sides (`config.py:Settings`;
`auth.ts:getJwtSecret`, which throws if unset) — previously both fell back to the same
publicly-known string independently, so a deployment that forgot to set it on either side still
verified tokens correctly using the shared default.

That check is deliberately **lazy** — evaluated inside `getJwtSecret()` at request time, not at
module scope. `next build` imports every route handler during "Collecting page data" to assemble
the route manifest, and `app/api/auth/[...nextauth]/route.ts` re-exports `handlers` from `auth.ts`,
so a module-scope `throw` failed the Docker builder stage, which has no env vars set. CI stayed
green throughout because `frontend-build` supplies a dummy `JWT_SECRET` (§15.2), which is why this
only ever surfaced at image build. Note that the `secret:` option in the NextAuth config reads
`process.env` directly instead of going through `getJwtSecret()`: that object literal is evaluated
at module scope, so routing it through the validating getter would reintroduce the exact build-time
throw. Passing `undefined` is safe — `setEnvDefaults()` only assigns defaults at import, while the
`MissingSecret` check runs per request inside `Auth()`, so a genuinely unset secret in production
still fails, at the first auth request rather than at import.

### 4.3 WebSocket authentication and project resolution

`authenticate_ws_user` (`chat.py:authenticate_ws_user`) mirrors the HTTP decoder but pulls
`google_access_token` and `google_refresh_token` from the payload and does **not**
auto-provision — unknown email returns `None` and the socket closes.

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
| 10 | `enqueue_write_job` | `tool_dispatch.py:dispatch_tool` → `producer.py:enqueue_write_job` | `RPUSH` onto `migrationbot:write_queue`, then sets a job-state key (below) |
| 11 | return | `tool_dispatch.py:dispatch_tool` | `{ok: true, status: "queued", job_id}` — **optimistic**, before Sheets is touched |
| 12 | `send_websocket_msg` | `agentic_loop.py:run_agentic_loop` | emits `tool_result` with that optimistic payload |
| — | *process boundary* | | |
| 13 | `start_worker` | `worker.py:start_worker` | `BLMOVE write_queue → write_queue:processing`, 10 s timeout (§5.3) |
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

### 5.2 Queue durability and job-state

Added in Phase 5, closing the gap where `BLPOP` removed a job from the queue on pickup with
nothing recording it existed if the worker died before finishing — a crash mid-`process_job` lost
the write silently, leaving only an audit row if the exception happened to be caught.

- **Atomic pickup**: `worker.py:start_worker` uses `BLMOVE` instead of `BLPOP` — it blocks like
  `BLPOP` but atomically relocates the job into `migrationbot:write_queue:processing`
  (`worker.py:PROCESSING_KEY`) rather than just removing it. A clean pass through `process_job`
  (success **or** a recorded business failure — both are terminal, correctly-audited outcomes)
  removes it from there via `LREM`. Anything left in `PROCESSING_KEY` didn't reach a clean
  completion.
- **Recovery on startup**: `worker.py:recover_stale_jobs` runs once before the main loop starts.
  Anything still in `PROCESSING_KEY` — left by a crash, OOM kill, or forced container restart —
  gets its `attempt` counter incremented and is re-queued onto `migrationbot:write_queue`, up to
  `worker.py:MAX_ATTEMPTS` (3). This lines up with the deployment model: `worker` has
  `restart: unless-stopped` (`docker-compose.yml`), so a crash triggers a container restart, which
  triggers this recovery scan automatically.
- **Dead-letter path**: past `MAX_ATTEMPTS`, the job goes to `migrationbot:write_queue:dead_letter`
  (`worker.py:DEAD_LETTER_KEY`) instead of being retried forever, and `events.py:publish_queue_update`
  fires a `"failed"` `queue_update` so the user learns the write didn't go through rather than
  waiting on it indefinitely.
- **Redis fault tolerance**: `start_worker`'s `BLMOVE` originally guarded only `TimeoutError`, so
  any other Redis fault killed the loop and let `restart: unless-stopped` bring the container back.
  In production that surfaced as six consecutive
  `ReadOnlyError: You can't write against a read only replica` crashes — and because each restart
  re-ran `recover_stale_jobs`, each one spent a `MAX_ATTEMPTS` budget on jobs that had done nothing
  wrong, so a long enough Redis outage would dead-letter perfectly valid writes. `ReadOnlyError` is
  a `ResponseError`, **not** a `ConnectionError`, so catching `ConnectionError` would never have
  covered it; `worker.py:REDIS_TRANSIENT_ERRORS` names it explicitly. Redis-level faults now back
  off in place (1 s doubling to 60 s, `REDIS_BACKOFF_INITIAL_SECONDS`/`_MAX_SECONDS`), the backoff
  resets on the first successful `BLMOVE` (including the idle no-job return), startup recovery
  failing no longer prevents the worker reaching its main loop, and a failed post-write `LREM` is
  logged rather than fatal — the write is already applied and audited by then, so a later replay
  beats losing the worker.
- **Job-state key**: `migrationbot:job_state:<job_id>` (`queue/schemas.py:JOB_STATE_PREFIX`,
  7-day TTL) is set to `"queued"` at enqueue time (`producer.py:enqueue_write_job`) and updated
  through `"processing"` → `"done"` (or `"error"`, if picked up but not cleanly finished — see
  above) by `worker.py:_set_job_state`. `GET /api/jobs/{job_id}` (`api/jobs.py:get_job_status`,
  §10) makes the `job_id` already returned to the caller (`tool_dispatch.py:dispatch_tool`)
  queryable after the fact — previously it was returned once and then went nowhere. Scoped to the
  job's own `user_email` or a config admin; a mismatch 404s rather than 403s, so a job's existence
  isn't confirmable to a caller who doesn't own it.

### 5.3 Read path — `search_rows`

| # | Function | Location | Action |
|---|---|---|---|
| 1–7 | as the write path, through `dispatch_tool` | | |
| 8 | read branch | `tool_dispatch.py:dispatch_tool` | membership test against the 5-name read tuple |
| 9 | `search_rows` | `tool_dispatch.py:dispatch_tool` → `read.py:search_rows` | filters, `return_fields`, `limit` (default 20) |
| 10 | `get_header_row` | `read.py:search_rows` → `meta.py:get_header_row` | **API call 1** — headers, each cell `.strip()`ed |
| 11 | `col_idx` build | `read.py:search_rows` | `{h.lower().strip(): i}` — normalised on both sides, fixed in Phase 1 (§16 history) |
| 12 | `resolve_column` | `read.py:search_rows` | maps each filter term to a canonical name |
| 13 | mapping guard | `read.py:search_rows` | normalised canonical not in `col_idx` → returns `{"ok": false, "error": ...}` |
| 14 | bulk fetch | `read.py:search_rows` → `read.py:_fetch_all_rows` | **API calls 2+** — paginated `page_size`-row (default 2000) chunks until a short page ends the scan (§11) |
| 15 | matching | `read.py:search_rows` | AND across filters; `blank` / `contains` / `exact` |
| 16 | truncation | `read.py:search_rows` | stops at `limit`; reports `capped` and (separately) `truncated` |

At least two Sheets API calls per search, and no cache — every `search_rows`, `summarize`, and
`data_quality` invocation re-reads the whole tracker from Google on every request
(`read.py:search_rows`, `read.py:summarize`, `read.py:run_data_quality_check` — each calls
`read.py:_fetch_all_rows`, §11).

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
| **Editor** | all | all not in `denied_ops` | all four write tools, in `PermissionChecker.can_execute` | honoured |
| **Viewer** (default, §16 history) | all | none (`PermissionChecker.can_execute`) | n/a | **not consulted** |

Field enforcement is skipped entirely when `allowed_fields == ["*"]` (`permissions.py:PermissionChecker.can_execute`).
When it isn't, all four write tools are covered: `update_cell` and `bulk_update` check the field(s)
named in `args`; `add_row` checks the keys of its free-form `fields` dict against `allowed_fields`
(the `module`/`type`/`description`/`assigned_to` base params are structural — required to create
any row at all — and aren't gated, only the escape-hatch extra columns a caller can attach via
`fields` are); `format_row` checks that the literal string `"Color"` is in `allowed_fields`, since it
always writes the sheet's Color/highlight column regardless of `scope` — `worker.py:process_job`
records every `format_row` audit row with `field="Color"`, so it's gated the same way a single-field
write would be. Previously only `update_cell`/`bulk_update` were checked — a user restricted to one
column could still write to any column via `add_row`'s `fields` dict, or recolor any row via
`format_row` (§16 history).

**RBAC is fail-closed.** `get_user_permissions` returns `settings.DEFAULT_ROLE` (`config.py:Settings`,
default `"viewer"`) with `["*"]` when no `project_id` is given, when no `users` row matches, and
when no `permissions` row exists (all in `permissions.py:get_user_permissions`) — previously this
fell back to `"editor"`, so any user the checker couldn't place landed on an arbitrary project
(via the WebSocket's "first active project" fallback, `chat.py:websocket_chat_endpoint`) with full
write access. Flipping the default required a one-time migration
(`backend/scripts/seed_permissions.py`) to grant every existing user explicit `editor` rows on
every active project *before* the new code deployed — otherwise everyone relying on the old
fail-open default would have lost write access the moment it shipped. `admin.py:bulk_grant_permissions`
is the ongoing equivalent for onboarding a user base onto a newly created project.

`can_execute` (`permissions.py:PermissionChecker.can_execute`) classifies explicitly: any
non-admin caller requesting a `tool_name` in neither `READ_ONLY_TOOLS` nor `WRITE_TOOLS` is denied
with a "not a recognized tool" error before role logic runs at all — previously `WRITE_TOOLS` was
defined but never consulted, so classification was by exclusion from `READ_ONLY_TOOLS` alone: an
editor could reach the bottom-of-function `return True, ""` for any unclassified tool name, and a
legitimate new read tool omitted from `READ_ONLY_TOOLS` would be silently denied to viewers with a
misleading "read-only access" message rather than a "not recognized" one (§16 history).

---

## 7. Schema & Column Resolution

### 7.1 `schema_config` shape

JSONB defaulting to `{}` (`models/project.py:Project`), in one of two shapes distinguished by a
top-level `"tabs"` key: multi-tab (`{"tabs": {...}, "global": {...}}`, what `detect_all_tabs`
produces — `schema_detect.py:detect_all_tabs`) or flat. The disambiguation has one implementation,
`core/schema.py:get_tab_schema` (plus `core/schema.py:get_available_tabs` for the system-prompt tab
list) — previously it was copy-pasted at seven call sites (`read.py`, `write.py` ×3,
`worker.py`, `chat.py`, `agentic_loop.py`), one of them a private `_get_tab_schema` the others
didn't share (§16 history).

`get_available_tabs` replaced `get_valid_modules`, and the rename carries the fix. That function
always returned **tab names**, but `tool_schemas.py` injected them into the prompt as
"Valid modules: …" — an allowlist of legal identifier prefixes. Tabs are a navigation concept for
`switch_module`; nothing derives legal IDs from them, and the multi-tab branch no longer reads
`global.valid_modules` at all (see §7.3 and §8). A flat, single-tab config yields `[]`, meaning
"nowhere to switch to" rather than "no identifiers are valid".

Per-tab defaults are applied inline at each use: `data_start_row` → `3`, `primary_id_position` →
`"B"`, `primary_id_column` → `"RICEFW ID"`, `assignee_column` → `"Technical Resource "` (with
trailing space), `critical_fields` → a six-name list (`read.py:search_rows`, the default list
literal). `header_row_num` is always `data_start_row - 1`. **Those literal fallbacks are still one
customer's column names** — they only bite when `schema_config` omits the key, but on a non-SAP
sheet that is exactly when they are wrong (§16.2).

### 7.2 Column-name resolution and the whitespace asymmetry

`resolve_column(term, column_map)` (`column_mapper.py:resolve_column`) resolves in three stages:
exact match on canonical keys, alias-list match (both compared case-insensitively and stripped),
then `difflib.get_close_matches` at `cutoff=0.6`. It returns the **unstripped** canonical key.

`COLUMN_ALIASES` (`column_mapper.py:COLUMN_ALIASES`) deliberately preserves the tracker's real
header typos and trailing spaces — `"Technical Resource "`, `"Functinal Resource "`, `"Color "`,
`"Programe Name"`. It is explicitly **not** a general default: those headers exist on exactly one
customer's sheet, so resolving against them elsewhere maps user terms onto columns that are not
there. It is now reached only when nothing whatsoever is known about a sheet's columns — no stored
`column_map` and no header row to read. Whenever real headers exist, `build_column_map` falls back
to `column_mapper.py:_identity_map` (each real header aliased to its own lowercased form) instead,
which keeps `resolve_column`'s exact and fuzzy passes working without inventing vocabulary.

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
are skipped; `data_start_row` = `header_row_index + 2`. On LLM failure a fallback is returned with
`is_tracker_sheet: True` — so an outage still registers every tab, including cover pages, as a
tracker.

That fallback is now `schema_detect.py:_structural_fallback`, derived from the sheet's own header
row: it picks the header row as the earliest of the first ten rows with the most non-empty cells
(trackers routinely carry a title or spacer above the real header), takes the first non-empty
header as `primary_id_column`, converts its index to an A1 letter, and matches the status / module /
type / assignee / description and date columns by generic keyword (`status`, `owner`, `due`,
`category`, …) via `schema_detect.py:_match_header`. It deliberately emits **no** `valid_modules` or
`valid_types`: an unverified vocabulary is later enforced as a constraint, and no vocabulary is the
honest default.

It previously returned a verbatim copy of one customer's WRICEF schema — `"RICEFW ID"`,
`"Dev Status"`, `"Technical Resource "`, the twelve SAP module codes and the WRICEF type letters.
On any other sheet every one of those columns is absent, so reads and writes silently addressed
columns that did not exist, and the invented `valid_modules` became an allowlist that rejected
legitimate rows. The detection prompt itself was also teaching the model to emit those lists via a
WRICEF-flavoured example; the example is now neutral and instructs it not to emit permitted-value
lists at all.

`global.valid_modules` is likewise gone from the produced config, replaced by an informational
`global.detected_tabs` — consumers read tab names from `tabs` directly (§7.1). A legacy stored
config may still carry the old key; it is inert, and `test_core_logic.py` has a regression asserting
so.

`build_column_map` (`column_mapper.py:build_column_map`), a two-pass LLM alias generator, is now
called from `schema_detect.py:detect_all_tabs` for every tracker tab it detects — it generates an
alias map from that tab's own real headers and attaches it as `column_map` in the tab's schema,
rather than every deployment silently running on the static `COLUMN_ALIASES` fallback (hardcoded to
one customer's sheet — §16 history) unless an admin hand-edited `schema_config`. `build_column_map`
falls back to `_identity_map` over the real headers on any LLM failure (§7.2), so this can't make
detection worse than before. Its two alias-generation prompts are also domain-neutral now: they
used to instruct the model to produce SAP terms ("BADI, enhancement exit, Z-table, tcode, RICEF,
transport request") for *every* sheet, which actively poisoned the map on a non-SAP tracker; they
now infer the domain from the headers themselves and the pass-2 review removes aliases borrowed
from a domain the sheet is not about. `get_column_map_json`
(`column_mapper.py:get_column_map_json`) is the system-prompt serializer for whichever map ends up
active, called from `agentic_loop.py:run_agentic_loop`.

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
  (`tool_schemas.py:get_system_prompt_compact`). Both builders take the **available tab list**
  (§7.1) and render an empty list as `(single tab — switch_module is not applicable)` via
  `tool_schemas.py:_format_tabs`. Neither substitutes a vocabulary: they used to fall back to the
  literal `FI,MM,SD,PM,QM,PP,TRM,HCM,IM,CO,FM,PS` whenever the list was empty and present it as
  "Valid modules", so a single-tab sheet — which legitimately has no tabs — was told one customer's
  SAP module codes were the only legal identifier prefixes, and refused anything else.
- **Domain-neutral prompt** — the system prompt describes a generic tracking spreadsheet and defers
  to the injected column reference guide as the sole authority on what exists. Rule 1 instructs the
  model to pass row IDs through verbatim and never reject one for looking unfamiliar; it previously
  asserted a fixed `MODULE-NNN` shape alongside the module allowlist.
- **DSML leakage guard** — content containing `<｜｜DSML｜｜>` triggers one retry against
  `deepseek-chat`, inline in `agentic_loop.py:run_agentic_loop`.
- **CoT suppression** — `reasoning_content` is logged, never forwarded.
- **Self-repair** — a failed tool result gets a `[System Note]` appended, with guidance chosen for
  that failure's class (§8.2). Only argument-level failures invite a retry.
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

### 8.2 Failure classification

`dispatch_tool` used to return `{"ok": False, "error": str(e)}` for every failure, and the loop
appended the same *"if this was due to column alias or RICEFW ID mismatch, formulate a corrected
tool call"* note to all of them. Both halves of that were wrong for anything that wasn't an
argument error, and the 2026-08 incident showed the cost: a `ReadOnlyError` from Redis reached the
model as an undifferentiated string, so it kept rewriting a call that could not succeed, and
reached the user as *"this is a read-only replica"* — which they reasonably read as a permissions
problem with their own spreadsheet.

`core/errors.py:classify_error` maps an exception to one of eight kinds:

| Kind | Raised by | Model told to retry? |
|---|---|---|
| `invalid_request` | `KeyError`/`ValueError`/`TypeError`/`IndexError`, Google 4xx | yes, once, with corrected arguments |
| `not_found` | Google 404 | yes — locate the record with `search_rows` first |
| `permission` | RBAC denial (`agentic_loop.py`), Google 403 | **no** |
| `auth` | `RefreshError`/`GoogleAuthError`, Google 401 | **no** — user must re-authenticate |
| `rate_limit` | Google 429, 403 with a quota reason | **no** |
| `infrastructure` | `redis.ReadOnlyError`/`ConnectionError`/`TimeoutError`, SQLAlchemy `OperationalError`/`InterfaceError`/`DBAPIError`, `OSError` | **no** |
| `upstream` | Google 5xx | **no** |
| `unknown` | anything else | **no** — report rather than repeat |

A failure result now carries three things instead of one: `error` (raw, for logs and the audit
row), `error_kind`, and `user_message` — the last written without infrastructure nouns, since
"replica" and "asyncpg" mean nothing to someone editing a tracker.
`core/errors.py:failure_note` builds the per-kind `[System Note]`, and only the first two rows
above invite a retry; the rest say *do not retry* explicitly, because a retry there merely burns
iterations against the 8-iteration cap before the user hears anything.

`worker.py:process_job` sends the classified `user_message` to the `queue_update` event while the
audit row keeps the raw exception, and `chat/page.tsx`'s toast renders that reason instead of
discarding it behind "encountered an error" (§14).
These are OpenAI function-calling schemas, so a `pattern` or `enum` in them is a **hard constraint
the model cannot emit around** — a stricter gate than any prompt wording, and one that fails
silently. Four such constraints encoded one customer's taxonomy and have been removed:

| Was | Where | Effect |
|---|---|---|
| `"pattern": "^([A-Z]+-)?[A-Z]{2,3}-[0-9]{3}$"` | `get_row.ricefw_id` | rejected `SLCM-0586` twice over — 4-letter prefix, 4-digit number |
| `enum` of the 12 SAP module codes | `add_row.module`, `bulk_update.filter_by.module`, `summarize.scope_module` | no non-SAP category could be expressed |
| `enum: ["R","I","C","E","F","W"]` | `add_row.type` | WRICEF type letters only |

Parameter descriptions now refer to "this sheet's own vocabulary" and point at the column reference
guide rather than naming `RICEFW ID` / `Dev Status` / `Go-Live Date`. `switch_module`'s description
additionally states that an unfamiliar ID prefix is *not* evidence a tab switch is needed — prefixes
are not tab names. The parameter key is still `ricefw_id` across `tool_dispatch.py`, `read.py`,
`write.py` and `worker.py`; renaming it is a wider change and was not attempted (§16.2).

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
| `queue_update` | `{type, job_id, status, tool_name, args, session_id, error}` | `events.py:publish_queue_update` via `worker.py:process_job`, relayed by `chat.py:forward_queue_updates` | `useWebSocket.ts:connect` (`onmessage`) → DOM `CustomEvent` → `chat/page.tsx` (`queue_update` listener) | toast keyed on `status`; a `failed` toast appends `error`, which the worker fills with the classified `user_message` (§8.2) |

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

Every deliberate close goes through `useWebSocket.ts:teardownSocket`, which nulls all four handlers
*before* calling `close(1000, …)`. Without that, replacing a socket left the old one's `onclose`
attached, and a bare `close()` reports `1005` ("no status received") — which passes the retry guard
above. The socket closed on purpose therefore scheduled a reconnect 3 s later, which closed the
healthy socket that had just replaced it, which scheduled another: a self-sustaining flap with a
period of exactly 3000 ms. It began on **every** page load, production included, because
`useWebSocket` is called with `activeProject?.id || null` (`chat/page.tsx`), so `projectId` flips
`null` → id once the projects fetch resolves and the effect re-runs. Each cycle cleared
`isConnected` — which disables the composer — and reset the per-connection `message_history` (§4.3),
so the assistant silently lost conversation context every 3 seconds. `onopen`/`onclose` additionally
guard on `wsRef.current !== socket`, since `onclose` fires asynchronously and could otherwise land
after the replacement's `onopen` and stomp `isConnected` back to false over a live socket. The
effect cleanup tears the socket down as well; it previously cleared only the timers, leaking a live
connection when navigating away from `/chat`.

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
| POST | `/admin/permissions/bulk-grant` | grant one role to many (or all) users on one project at once, upserting per user | `admin.py:bulk_grant_permissions` |
| GET | `/admin/audits` | filter by user/tool/RICEFW ID, limit 100 | `admin.py:list_audits` |
| GET | `/admin/analytics/summary` | counts and failure totals | `admin.py:get_analytics_summary` |

Non-admin: `GET /api/health` (`health.py:health_check`, liveness — static literal, no dependency
probing), `GET /api/ready` (`health.py:readiness_check`, added in Phase 0 of the remediation plan
— live `SELECT 1` against Postgres, `PING` against the shared `producer.redis_client`, and a check
of `request.app.state.db_initialized` (set in `main.py:lifespan` from `init_db()`'s real outcome);
`503` with a per-service `detail` dict when any of the three fails; verified against unreachable
dependencies and against a startup where `init_db()` failed but Postgres itself stayed reachable —
a live `SELECT 1` alone can't tell "Postgres is down" apart from "Postgres is up with no tables
ever created" (§16 history)),
`GET /api/me` (`api/auth.py:get_current_profile`, mounted directly in `main.py`),
`GET /api/projects` (`chat.py:list_user_projects`), and `GET /api/jobs/{job_id}`
(`api/jobs.py:get_job_status`, added Phase 5 — queries the job-state key a write job's `job_id`
maps to, §5.2; scoped to the job's own `user_email` or a config admin, 404 rather than 403 on a
mismatch).

`docker-compose.yml`'s `backend` service now runs a `healthcheck` against `/api/ready` (interval
30 s, 3 retries, `start_period` 15 s) — see §3.

`chat.py:list_user_projects` filters to projects the caller has an explicit `permissions` row for
— config admins see every active project; everyone else only sees ones actually granted to them.
Previously it returned every active project, including `spreadsheet_id` and full `schema_config`,
to any authenticated caller regardless of whether they had a permissions row for it.

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
| `get_id_row_map` | `meta.py:get_id_row_map` | 1 — full ID-column scan, resolves every ID in one pass |
| `get_row` | `read.py:get_row` | 3 — `find_row_num` + row fetch + headers |
| `get_row_raw` | `read.py:get_row_raw` | 3 — same shape (still per-ID; only ever called for a single ID) |
| `get_bulk_rows_raw` | `read.py:get_bulk_rows_raw` | 2 + 1 `batchGet` — headers, one `get_id_row_map` scan, one `batchGet` covering every resolved row |
| `search_rows` | `read.py:search_rows` | 1 + ⌈rows / 2000⌉ — headers + paginated (`read.py:_fetch_all_rows`) |
| `summarize` | `read.py:summarize` | same shape as `search_rows` |
| `run_data_quality_check` | `read.py:run_data_quality_check` | same shape (+1 or +2 for `consistency`/`stale` DB reads) |
| `update_cell` | `write.py:update_cell` | 3 — `find_row_num` + headers + one `batchUpdate` |
| `bulk_update` | `write.py:bulk_update` | 2 + one `get_id_row_map` scan (not one `find_row_num` per target ID) |
| `add_row` | `write.py:add_row` | 1 + headers, `values().append` |
| `format_row` | `format.py:format_row` | `spreadsheets().batchUpdate` `repeatCell` |

`next_ricefw_id` (`meta.py:next_ricefw_id`) computes max+1 over parsed IDs, formatted `%03d`;
`switch_module` (`meta.py:switch_module`) optionally verifies the tab exists then updates
`sessions.active_tab`.

`get_bulk_rows_raw` (`read.py:get_bulk_rows_raw`) previously called `get_row_raw` once per target
ID — three Sheets calls apiece (a full ID-column scan via `find_row_num`, the row fetch, a header
re-fetch) — so the audit pre-read alone cost ~3N calls, and `bulk_update`
(`write.py:bulk_update`) separately spent another N on its own per-ID `find_row_num`. A 50-row
bulk update was on the order of 200 API calls against a quota-limited endpoint. Both paths now
share `meta.py:get_id_row_map`, which scans the ID column once and resolves every target ID from
that one pass; `get_bulk_rows_raw` fetches all resolved rows in a single `batchGet` rather than one
`get()` per row. The same 50-row bulk update is now on the order of 5 calls total, regardless of N.

`search_rows`/`summarize`/`run_data_quality_check` no longer request a single fixed
`{start}:{start+2000}` window — `read.py:_fetch_all_rows` pages through `page_size`-row (default
2000) chunks until a short page signals the end of data, so a tracker larger than one page is no
longer silently truncated (§16 history). A `_MAX_SCAN_ROWS` (`read.py:_MAX_SCAN_ROWS`, 20000)
safety valve stops pagination and sets a `truncated: true` field in the response if a
misconfigured schema somehow never yields a short page — no real tracker should reach it.

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

`run_data_quality_check` (`read.py:run_data_quality_check`) reads headers and the whole tracker
live (paginated, §11), then dispatches on the **required** `check_type` arg (`blank_fields` /
`consistency` / `stale` /
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
| `/` | `app/page.tsx` | landing + `signIn`; leads with a worked transcript (question → answer → ledger line) rather than feature cards |
| `/chat` | `app/chat/page.tsx` | main chat UI; feed, sheet tabs, composer, write ledger |
| `/admin` | `app/admin/page.tsx` | overview; four parallel fetches, Recharts |
| `/admin/projects` | `app/admin/projects/page.tsx` | project CRUD + tab detection; sends Google token headers |
| `/admin/users` | `app/admin/users/page.tsx` | permission upsert/delete |
| `/admin/audit` | `app/admin/audit/page.tsx` | filtered audit browser |

Shared components:

| Component | Purpose |
|---|---|
| `components/Modal.tsx` | the only dialog shell; portalled, focus-trapped, scroll-correct (§14.0.1) |
| `components/WriteLedger.tsx` | persistent record of queued/applied/failed writes (§14.3) |
| `components/MarkdownMessage.tsx` | assistant text as GFM (§14.1) |
| `components/ToolResultCard.tsx` | charts, meters and tables for read-tool payloads (§14.2) |

State lives in one Zustand store (`useChatStore.ts:useChatStore`) holding `projects`,
`activeProject`, `activeTab`, `isConnected`, `messages`, `ws`, and session metadata. The write
ledger is deliberately **not** in the store — it is derived per-render in `chat/page.tsx` from
`messages` plus a `job_id`-keyed map of terminal outcomes (§14.3).

Admin copy is written from the reader's side of the screen and carries no SAP vocabulary: the
headings are "Connected sheets", "Who can do what" and "Change history", not "Projects
Configuration Manager" / "User Security & RBAC Policies" / "Security Audits & History Log", and
the auto-detect failure no longer instructs people to look for a "RICEFW ID" column on a product
that reads any tracker (§16.3 covers what remains SAP-shaped *below* the UI).

### 14.0 Design system — "Ledger"

Defined entirely in `app/globals.css` (Tailwind v4 `@theme` tokens plus a small set of component
classes) and `app/layout.tsx` (fonts). It replaced a look that read as a template — near-black
ground, indigo→purple gradient headings, animated blur blobs, glassmorphism — and that contradicted
the product, since the page metadata still described an "SAP S/4HANA WRICEF Assistant" long after
§7.3 made schema detection generic.

- **Ground** is a green-grey ink (`--color-ink-950: #0a0e0d` … `--color-ink-100`), derived from
  ledger paper rather than from a void. `ink-500` sits at ~4.9:1 on the ground; it was `#5c6b68`
  (~3.6:1), which was under AA for the 13px secondary text it carries.
- **`--color-brass-400` is the only interactive accent.** The sign-in CTA is deliberately *not*
  brass (`.btn-invert`): Google's mark is multicolour and clashed on it, and reserving brass for
  the coordinate stamp makes the accent mean one thing product-wide.
- **Status is a semantic four-state system** — `--color-queued` / `applied` / `failed` / `denied`,
  surfaced as `.status.status-*`. These map to the states a write can be in (§5.1–5.2) and are used
  wherever an outcome is reported and nowhere else. The admin metric tiles previously carried four
  decorative gradients, which made four unrelated counts look like four categories of one thing;
  only a non-zero failure count is coloured now.
- **Type** is IBM Plex Sans + Plex Mono, **self-hosted via `@fontsource`**, not `next/font/google`.
  That is a deployment constraint, not a preference: `next/font/google` downloads the woff2 files
  from `fonts.gstatic.com` during `next build`, so the image build depends on outbound network
  access. It broke the Docker build — the font requests 404'd in the builder and Turbopack's
  fallback collapsed into a cascade of `Can't resolve
  '@vercel/turbopack-next/internal/font/google/font'` — while passing locally, where the fetch
  succeeded and Next cached it. `@fontsource` ships the woff2 files inside the npm package, so
  `npm ci` is the only network the build needs. Latin subset, six faces, 128 KB total.
  Chosen over Inter/Geist because Plex was
  drawn for enterprise data tooling and its tabular figures suit a product that is mostly numbers in
  columns — `font-variant-numeric: tabular-nums` is on globally. `.label-micro` (mono, uppercase,
  wide tracking) is the field-label and eyebrow voice throughout.
- **`.stamp` is the signature.** Every fact stated about a sheet is marked with where it came from
  in the sheet's own A1 vocabulary (`SD!B7`, a row ID, a tab). Unlike decorative `01/02/03`
  numbering it carries information the reader needs, and it reuses the coordinate system the
  backend already speaks (§7.1).
- **Component classes live in `@layer components`, and must.** Unlayered CSS beats *any* layered
  rule in the cascade, so while `.field` / `.btn` / `.panel` sat unlayered they silently defeated
  every Tailwind utility written alongside them: `field pl-9` computed the 12px from `.field`'s
  `padding` shorthand instead of 36px (the audit filter's search icon rendered on top of its own
  placeholder), and `px-5 py-3` on the sign-in button and `py-1.5` on the sheet selector were
  discarded outright. Putting a class in the layer is what opts it into being overridable, which is
  the entire point of having component classes rather than repeating utility strings.
- Glassmorphism was removed rather than restyled: `backdrop-filter` under a dense table costs real
  legibility. `.panel` / `.card` / `.well` are flat layered surfaces with hairline `--color-rule`
  borders. `:focus-visible` now paints a ring — almost every control sets `focus:outline-none` for
  a custom border and nothing had replaced it, so keyboard focus was invisible — and
  `prefers-reduced-motion` is honoured.

### 14.0.1 The dialog is portalled, and that is load-bearing

`components/Modal.tsx` renders through `createPortal(…, document.body)`. This is not tidiness.
`position: fixed` resolves against the viewport **only** while no ancestor establishes a containing
block, and any ancestor with a `transform`, `filter`, `backdrop-filter`, `perspective`, `contain` or
`will-change` establishes one.

Measured on the deployed `/admin/projects`: every admin page root carries the entrance animation,
whose fill mode (`forwards` originally, `both` after the redesign) left `transform: translateY(0)`
applied permanently once it finished. So `fixed inset-0` resolved against that div — the overlay
rendered **206 px tall inside a 620 px viewport**, and the dialog's pinned footer, Save button
included, fell past the bottom edge with no way to scroll to it.

This is the real cause of the original "modals not appearing properly" report, and it predates the
redesign. Two independent fixes, because either alone leaves a trap for the next component:

- the overlay is portalled outside every possible transformed ancestor, and
- `.animate-rise` carries **no fill mode**, so the transform is gone once the 220 ms entrance ends.

Verified by reproducing the exact condition (an `animate-rise` ancestor wrapping a modal with an
over-tall body): overlay height 671 px against a 670 px viewport, footer fully visible, body
scrolling internally. Initial focus targets the first control inside `[data-modal-body]` rather
than the close button, which precedes the body in DOM order and would otherwise win.

### 14.1 Message rendering

Assistant text renders as GitHub-flavoured Markdown through
`components/MarkdownMessage.tsx` (`react-markdown` + `remark-gfm`). The feed previously printed
`msg.content` into a `<p>` with `whitespace-pre-wrap`, so every `**bold**`, `###`, `---` and
pipe-delimited table the model emitted appeared as literal punctuation — the model had been writing
Markdown all along and nothing was reading it. Tables get their own `overflow-x` container so a wide
table never scrolls the chat column sideways. User messages stay literal; they are whatever was
typed.

Raw HTML is deliberately **not** enabled (no `rehype-raw`): this text is an LLM summarising
third-party spreadsheet content, so treating it as markup would be an injection path.
`react-markdown` escapes HTML by default.

### 14.2 Structured tool results

`components/ToolResultCard.tsx` draws the payloads the read tools already returned (§5.3,
`sheets/read.py`) but which the UI had been discarding — the chip only ever showed a
`JSON.stringify` of the *arguments*. It renders under a tool call once `status === "completed"`,
keyed on the result's own discriminant:

| Result | Form | Why |
|---|---|---|
| `summarize` / `count_by_field` | horizontal bar, one hue, direct labels, "Show data" table toggle | magnitude comparison; bar length carries the value, so hue must not vary. Horizontal because category labels run long |
| `summarize` / `completion_rate` | hero figure + meter + Complete/Remaining/Blank tiles | a single ratio against a limit — deliberately not a two-slice donut |
| `summarize` / `blank_fields` | stat tiles + affected-ID chips | one headline number, not a one-bar chart |
| `search_rows` | data table | header is the **union** of keys across rows, since blank columns are omitted per row |
| `data_quality` | collapsible raw JSON | no bespoke view yet |

Above 12 categories `count_by_field` defaults to the table rather than a wall of stubby bars, and
axis ticks longer than 24 characters truncate with the full label carried in the hover tooltip.

The bar hue (`#3987e5`) was validated with the data-viz palette validator against the *previous*
chat surface (`#030014` page + 5% white bubble ≈ `#100d20`): lightness band, chroma floor and ≥ 3:1
contrast all passed. The §14.0 ground (`#0a0e0d`) is marginally lighter, so contrast is essentially
unchanged and the hue was kept — it is also none of the four status colours and not the brass
accent, so a bar can never be mistaken for a state. Status colours were considered for chart marks
and rejected: as a set they fail all-pairs CVD separation, which is why they stay reserved for
icon + label pairings. Re-run the validator before substituting a themed accent.

### 14.3 The write ledger

`components/WriteLedger.tsx`, docked directly above the composer. It replaces the transient toast
that used to announce `queue_update` frames (§9.2).

A toast was the wrong primitive for this write path. Writes are eventually consistent — the model
enqueues, `queue/worker.py` applies at 1 req/sec, and the outcome arrives seconds later over the
socket (§5.1, steps 23–24) — but the toast expired after five seconds and took the only record of
the change with it, so anyone who looked away could not tell whether their edit had landed. Worse,
the handler discarded `data.error` entirely, leaving "encountered an error" as the only signal.

Each write is a ledger line keyed on `job_id` and upserted, so a job transitions
`queued → applied`/`failed` **in place** rather than emitting two unrelated toasts.

The two ends of that transition come from different sources, which is not obvious and was got wrong
first time. `worker.py` publishes a `queue_update` **only on terminal states** — there is no
in-flight frame on the socket at all, so a real write verified against production appeared already
`applied` and the queued state was unreachable. The queued row is therefore seeded client-side from
the enqueue result itself, which already carries `job_id` and `status: "queued"`
(`tool_dispatch.py`, §5.1 step 12); the terminal frame then resolves that same row by `job_id`.
Without it a slow or stuck write would display nothing while it waited, which is precisely the case
the ledger exists for.

The line is stamped with its tab and row ID (§14.0), shows `field → value` where the tool reported
them, and renders the classified `user_message` from `core/errors.py` (§8.2) inline on failure.
Entries persist for the session until dismissed; dismissal hides by `job_id` rather than emptying a
list, because the rows are derived rather than stored. The `queue_update` listener reads the active
tab through a ref so the subscription is not torn down and re-established on every tab change.

Verified against production with a real `update_cell` on a live sheet, sampling the DOM every
400 ms: the row appeared at ~5 s as `queued` and became `applied` at ~6 s, staying a **single row**
across the transition — which is the whole point, and the thing two toasts could not do. The
`failed` path has still only been seen with mock data; provoking it needs an actual write failure.

Tab switching is an explicit control frame, not prompt-driven (`chat/page.tsx:handleTabChange`):
the client sends `{type: "switch_tab", tab_name}` and only applies the new `activeTab` once the
server confirms with `tab_switched` (§9.1–9.2) — it no longer sets `activeTab` optimistically
before the switch is known to have succeeded.

`connect` tracks the live socket in a ref (`wsRef`, `useWebSocket.ts:useWebSocket`) rather than
reading the Zustand `ws` state through its own closure, so a reconnect from `onclose`'s
`setTimeout` or an effect re-run always closes the actual current socket — not a value captured at
whatever render created that particular `connect` closure. Replacement and unmount both route
through `useWebSocket.ts:teardownSocket`, which detaches handlers before closing so an intentional
close can never schedule a reconnect (§9.3). `setWs` is called from `onopen` rather than at
construction, so the store never advertises a socket still in `CONNECTING`.

---

## 15. Deployment, CI/CD & Tests

### 15.1 Compose topology

| Service | Build | Command | Published ports |
|---|---|---|---|
| `postgres` | `postgres:16` | default | **none** |
| `redis` | `redis:7-alpine` | `redis-server --requirepass $REDIS_PASSWORD` | **none** |
| `backend` | `./backend` | `uvicorn app.main:app --host 0.0.0.0 --port 8000` | **none** |
| `worker` | `./backend` | `python -m app.queue.worker` | none |
| `frontend` | `./frontend` | `node server.js` | **none** |
| `caddy` | `caddy:2-alpine` | default | `80:80`, `443:443` |

**Only Caddy publishes.** Until 2026-08 this table read `5433:5432`, `6379:6379`, `8000:8000` and
`3000:3000` — every one of which put a service on the public internet, since a compose `ports:`
mapping binds `0.0.0.0` by default. Nothing needed them: Caddy reverse-proxies to `backend:8000`
and `frontend:3000` over the compose network, and every other consumer addresses services by name.

The exposed, unauthenticated Redis is how the production host was compromised. Redis ships with no
authentication, and an attacker who can reach it can `CONFIG SET dir /root/.ssh` +
`CONFIG SET dbfilename authorized_keys` + `SAVE` to write their own key — which is what happened,
followed by two long-running C2 processes and several TB of egress before Hetzner's abuse
notifications were traced back. **A host firewall does not mitigate this**: Docker installs its own
`iptables` rules ahead of the `INPUT` chain, so a published port bypasses UFW entirely — a
`ufw deny 6379/tcp` was in place on that host and had no effect. Port filtering has to happen
either at the cloud provider's firewall (outside the VM) or by not publishing the port at all.

So, defence in depth: no mapping *and* `--requirepass`. `DB_PASSWORD` and `REDIS_PASSWORD` use
compose's `${VAR:?message}` form, which aborts startup when either is unset rather than
substituting an empty string and quietly starting a passwordless database or an open Redis — the
precise failure mode that started this.

`backend` and `worker` both load `.env` via `env_file` (`docker-compose.yml`, both services) with
`DATABASE_URL`/`REDIS_URL` overridden to compose service names (the `.env` values are for running
outside Docker only). Volumes: `pgdata`, `caddy_data`, `caddy_config` — **Redis has none**, so the
write queue is memory-only.

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

`frontend/Dockerfile`'s builder stage sets `ARG JWT_SECRET=build-placeholder` / `ENV
JWT_SECRET=$JWT_SECRET` as defence in depth against a future module-scope env read anywhere in a
route's import chain (§4.2) — it is not what fixes the lazy-validation case, which the `auth.ts`
change already covers on its own. Builder stage only: the runtime secret is injected into the
runner by `docker-compose.yml`'s `frontend` service `environment:`, and the placeholder was verified
not to be inlined into the server bundle (Next.js statically replaces only `NEXT_PUBLIC_*`, so
`process.env.JWT_SECRET` survives as a runtime lookup and the injected value wins). A real secret
must never be passed via `ARG` — build args are recorded in the image history.

`deploy.yml` no longer triggers on push directly — it's a `workflow_run` gated on `ci.yml`
("CI Pipeline") completing with `conclusion == 'success'` on `main`, so a push straight to main
that bypasses a PR (or that CI would have failed) can no longer deploy. The SSH script then
`git fetch origin main && git reset --hard origin/main`, `docker compose build` (no `--no-cache`
— `requirements.txt` has been exact-pinned since Phase 0, so a cached build is just as
reproducible), `up -d`, then verifies the deploy: checks `docker compose ps` for any container
stuck `Restarting`, polls `GET /api/ready` for up to 75 s — from *inside* the backend container
via `docker compose exec`, since the backend no longer publishes a host port (§15.1) — and
`exit 1`s with the last 80 log
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

55 test functions (58 collected cases, one being parametrised) across `tests/test_db.py` (4),
`tests/test_core/test_core_logic.py` (24), `tests/test_sheets/test_sheets_logic.py` (22),
`tests/integration/test_integration_logic.py` (5).
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

**Backend coverage gap worth knowing.** `core/errors.py:user_message_for` carries the detail
through for `UNKNOWN` and truncates it at 180 characters (§8.2). The carry-through is covered
indirectly by `test_worker_notifies_client_on_failure`, which is what caught the regression in CI
in the first place; the truncation is covered by nothing. Both deserve unit tests in
`test_core_logic.py` next to the existing three `classify_error` cases.

### 15.4 What the automated checks do not establish

Recorded because it cost real time this session. `tsc --noEmit`, `next build` and `npm run lint`
all passed while **four** genuine frontend defects were live, and every one of them was found only
by driving the deployed app in a browser:

| Defect | Why the checks missed it |
|---|---|
| Dialog overlay captured by a transformed ancestor (§14.0.1) | valid TS, valid CSS; only wrong at runtime, and only on pages that need an authenticated backend |
| `next/font/google` fetching gstatic during `next build` | the fetch succeeded locally and Next cached it — the failure is environmental, and surfaced only in the Docker builder |
| Component classes unlayered, silently eating every Tailwind utility (§14.0) | both stylesheets are valid; the cascade resolves quietly to the wrong value |
| Write ledger's `queued` state unreachable (§14.3) | frontend and backend are each self-consistent; the gap is in the contract between them |

The generalisable part: none of these are type errors or syntax errors, and three of the four are
invisible without a running backend. A green build says the frontend *compiles*, not that it
works. Local `npm run dev` covers `/` only — `/chat` and everything under `/admin` need a real
session and a live API, so they cannot be exercised on a machine without the stack running. Until
there is a way to run the app end to end locally (or a browser-driven smoke test in CI), changes
to those pages should be verified against a deploy before being called done.

---

## 16. Known Issues & Technical Debt

Ordered by severity. Items marked **(verified)** were reproduced by executing code. Items resolved
by the remediation plan are removed from this list, not annotated — see git history for what
changed and when.

### 16.1 OAuth tokens are serialised into the queue
**Severity: medium.** `WriteJobPayload` (`queue/schemas.py:WriteJobPayload`) carries
`google_access_token` **and, since Phase 4, `google_refresh_token`** as plain fields,
JSON-serialised into the Redis entry (`producer.py:enqueue_write_job`) so the worker can rebuild a
self-refreshing client (`worker.py:process_job`). Live user credentials — now including a
long-lived refresh token, not just an hour-lived access token — sit in Redis for as long as the
job is queued. That Redis is no longer published or unauthenticated (§15.1), which removes the
network path that made this critical rather than medium, but the tokens are still at rest in
plaintext in a process whose compromise would hand over live Google access.
Inherent to the OAuth-only design plus the queue boundary; noted, not solved.
Threading the refresh token through was a deliberate Phase 4 tradeoff — the alternative (queued
writes dying whenever the access token expires before the worker gets to them) was worse — but it
does widen what's exposed here, and a token-reference indirection (store tokens server-side, pass
the queue only an opaque reference) remains the real fix, not attempted here.

### 16.2 Recovery can replay a write that already succeeded
**Severity: low, but `add_row` is not idempotent.** A job is cleared from `PROCESSING_KEY` by an
`LREM` that runs *after* `process_job` returns. If that `LREM` doesn't land — Redis drops out in
the window between the Sheets write and the clear — the job stays in `PROCESSING_KEY` and
`recover_stale_jobs` re-queues it on the next worker start, applying the write a second time.

This is not new behaviour: previously the worker crashed at that point, leaving the job in exactly
the same state, so the replay happened either way. Re-applying `update_cell`, `bulk_update` or
`format_row` is harmless (they set an absolute value), but `add_row` computes a fresh sequential
ID via `meta.py:next_ricefw_id` and would insert a **duplicate row**. The real fix is an
idempotency key checked before the mutation rather than after it; the window is small enough that
it hasn't been observed, and it is recorded here rather than papered over.

### 16.3 SAP-specific assumptions still remain outside the prompt and tool schemas
**Severity: medium.** The hard blocks were removed (§7.3, §8.1) but the product goal — running
against *any* company tracking sheet, not only a WRICEF tracker — is not fully met:

- **Inline column defaults.** `read.py`/`write.py`/`worker.py` still default
  `primary_id_column` → `"RICEFW ID"`, `status_column` → `"Dev Status"`, `assignee_column` →
  `"Technical Resource "`, and the `overdue` report defaults `go_live` → `"Go-Live Date"` (§7.1).
  These only apply when `schema_config` omits the key — which on a non-SAP sheet is exactly when
  they are wrong, and they fail as "column not found" rather than as a misconfiguration.
- **`add_row` requires `module` and `type`.** Both are `required` in the tool schema and consumed
  by `worker.py:process_job`. A sheet with no category or type column cannot satisfy them. Making
  them optional means touching the write path, so it was left alone.
- **The `ricefw_id` parameter key** is unchanged across `tool_dispatch.py`, `read.py`, `write.py`
  and `worker.py`. Cosmetic for the model (its description no longer implies SAP), but it keeps the
  domain baked into the wire format.
- **Existing projects are unaffected until re-detected.** These changes alter what
  `detect_all_tabs` *produces*; a project onboarded earlier still carries its old `schema_config`
  in Postgres, possibly the hardcoded fallback. Re-running tab detection from `/admin/projects` is
  the migration path — there is no automatic backfill.

### 16.4 Dependency advisories are outstanding
**Severity: medium, unverified.** `npm audit` in `frontend/` reports 2 critical and 6 high, all in
pre-existing dependencies: `@auth/core` / `next-auth` (existence-based auth bypass, and `getToken()`
raising on a malformed bearer), `next` (middleware/proxy bypass in App Router with Turbopack),
plus `postcss`, `sharp`, `nanoid`, `js-yaml`, `brace-expansion`. The two auth advisories bear
directly on §4's token chain. Not triaged against the versions actually in `package-lock.json`, and
no upgrade attempted — `next-auth` is on a beta pin, so bumping it is not a patch-level change.

---

## 17. Appendix — Environment Variables

| Variable | Consumed by | Default |
|---|---|---|
| `DATABASE_URL` | `db/engine.py` (module-level engine creation) | `…@localhost:5433/migrationbot` (`config.py:Settings`) |
| `REDIS_URL` | `producer.py:enqueue_write_job`, `events.py:publish_queue_update`, `worker.py:start_worker`, `chat.py:forward_queue_updates` | `redis://localhost:6379` (`config.py:Settings`) |
| `DEEPSEEK_API_KEY` | `chat.py:llm_client` | `"mock-deepseek-key"` (`config.py:Settings`) |
| `GOOGLE_CLIENT_ID` / `_SECRET` | `sheets/client.py:build_sheets_service`, `auth.ts` (`GoogleProvider` config) | mock values (`config.py:Settings`) |
| **`JWT_SECRET`** | `deps.py:get_current_user`, `chat.py:authenticate_ws_user`, `auth.ts:getJwtSecret` (and the `secret:` option, read from `process.env` directly — §4.2) | **required — no default on either side** (`config.py:Settings`; `auth.ts:getJwtSecret` throws if unset, at request time) |
| `ALLOW_DEV_AUTH` | `deps.py:get_current_user`, `chat.py:authenticate_ws_user` | `false` (`config.py:Settings`) — only set true for local dev/tests |
| `DEFAULT_ROLE` | `permissions.py:get_user_permissions` | `"viewer"` (`config.py:Settings`) — fail-closed default when no permissions row applies |
| **`CORS_ORIGINS`** | `main.py` (`CORSMiddleware` registration) | **required — no default** (`config.py:Settings`) |
| **`ADMIN_EMAILS`** | `config.py:Settings.admin_emails_list` | **required — no default** (`config.py:Settings`) |
| **`DEFAULT_SPREADSHEET_ID`** | declared `config.py:Settings` | **required — no default**; referenced nowhere in `backend/app/` |
| `DEFAULT_SHEET_TAB` / `_LABEL` | declared `config.py:Settings` | defaults present; referenced nowhere in `backend/app/` |
| `NEXTAUTH_SECRET` / `NEXTAUTH_URL` | `auth.ts`, `docker-compose.yml` (`frontend` service env) | secret falls back to `JWT_SECRET` |
| **`DB_PASSWORD`** | `docker-compose.yml` (`postgres`/`backend`/`worker` services) | **required under compose** — `${DB_PASSWORD:?…}` aborts startup if unset (§15.1) |
| **`REDIS_PASSWORD`** | `docker-compose.yml` (`redis` `--requirepass`, substituted into `backend`/`worker` `REDIS_URL`) | **required under compose** — `${REDIS_PASSWORD:?…}` aborts startup if unset (§15.1) |
| `NEXT_PUBLIC_WS_URL` | `useWebSocket.ts:connect` | empty in compose (`docker-compose.yml`, `frontend` service env) |
| `VPS_HOST` / `VPS_USER` / `VPS_SSH_KEY` / `VPS_PASSWORD` | `deploy.yml` (`appleboy/ssh-action` inputs) | GitHub secrets |
