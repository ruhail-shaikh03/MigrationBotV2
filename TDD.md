# MigrationBot Enterprise Portal — Technical Design Document

> **Provenance.** Written from a full read of the `main` branch at commit `0e988c7`. Every factual
> claim carries a `file:line` citation verified against that code. Descriptive sections state only
> what the code does; opinions — bugs, risks, smells — are confined to blockquoted **⚠ callouts**
> and to §16 *Known Issues*. Findings marked **(verified)** were reproduced by executing code, not
> only by reading it.
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
     │  HTTPS / WSS     │  Caddyfile:2-17                          │
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
                  │ auth.ts:107-109       │   │  WS   /ws                    │
                  └───────────────────────┘   └───┬──────────┬──────────┬────┘
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
image (`docker-compose.yml:24-25`, `:39-42`), so the worker cannot touch the API's sockets — job
outcomes travel back over a per-user Redis pub/sub channel (§5.1). And every read is a synchronous
round trip to Google, so read latency and Sheets quota are directly user-facing.

### 1.2 Technology inventory

| Layer | Technology | Citation |
|---|---|---|
| Frontend | Next.js 16.2.9, React 19.2.4, NextAuth 5.0.0-beta.31, Zustand 5, Tailwind 4, Recharts | `frontend/package.json:11-32` |
| Backend | FastAPI, uvicorn, SQLAlchemy 2.0 + asyncpg, Pydantic 2, python-jose | `backend/requirements.txt:1-10` |
| Data | PostgreSQL 16 — RBAC/audit only, **not** sheet data | `docker-compose.yml:4-5` |
| Queue | Redis 7 — list-backed FIFO **and** pub/sub event bus | `producer.py:57`, `events.py:46` |
| LLM | `AsyncOpenAI` against `https://api.deepseek.com/v1` | `api/chat.py:32-34` |
| Sheets | `google-api-python-client`, per-user OAuth `Credentials` | `sheets/client.py:11-19` |
| Proxy | Caddy 2-alpine, auto-TLS | `docker-compose.yml:71-72` |

---

## 2. Persistence Model

Five tables, all SQLAlchemy 2.0 `Mapped[...]` / `mapped_column(...)` on a shared `DeclarativeBase`
(`db/engine.py:25-26`). Created at API startup by `init_db()` in the lifespan hook
(`main.py:24`); failure is logged and swallowed (`main.py:26-28`). No migration tool exists —
schema evolution relies on `metadata.create_all` (`db/engine.py:39-40`).

| Table | Model | Notable columns |
|---|---|---|
| `users` | `models/user.py:12-26` | unique indexed `email`; nullable `google_sub`; `last_login` |
| `projects` | `models/project.py:14-30` | unique `spreadsheet_id`; `default_tab`; `company_prefix`; `schema_config` JSONB default `{}` |
| `permissions` | `models/permission.py:13-34` | `role` default `"editor"`; `allowed_fields` default `["*"]`; `denied_operations` default `[]`; unique `(user_id, project_id)`; CHECK `role IN ('admin','editor','viewer')` |
| `sessions` | `models/session.py:13-31` | UUID PK; `active_tab`; `project_id` `ON DELETE SET NULL` |
| `audit_logs` | `models/audit_log.py:8-30` | `old_value`/`new_value` Text; `args_json` JSONB; `result_ok`; generated `created_month` |

**Postgres holds no spreadsheet data.** Every WRICEF row is read live from Google Sheets. The
database exists for identity, authorisation, session state, and the audit trail only.

---

## 3. Configuration & Startup

`Settings` (`config.py:5-37`) reads `("../.env", ".env")` relative to the process working directory
(`config.py:34`) and ignores unknown keys (`:36`).

**Three variables are required with no default** — `DEFAULT_SPREADSHEET_ID` (`config.py:18`),
`ADMIN_EMAILS` (`:23`), `CORS_ORIGINS` (`:24`). A `ValidationError` on any of them is converted to
a `RuntimeError` naming the missing keys (`config.py:39-48`). This is a deliberate hardening
change: these previously fell back to hardcoded production values.

Secrets still carrying defaults: `DEEPSEEK_API_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
and `JWT_SECRET` (`config.py:11-14`).

> **⚠ This exact gap caused a live outage.** On 2026-08-06 the deployed `.env` had no
> `CORS_ORIGINS`, and because `settings` is built at module import (`config.py:40`), both
> `backend` and `worker` crash-looped — `docker ps` showed `Restarting (1)` on ~20-second cycles
> for roughly 21 hours before it was noticed, with `frontend`, `caddy`, `postgres`, and `redis` all
> healthy throughout, giving no visible signal that anything was wrong short of checking container
> status directly. Fixed operationally (the var added to the server's `.env`) and structurally:
> `.env.example` now documents all three required variables and ships a working `CORS_ORIGINS`
> value instead of the `*` it had before (a literal `*` also silently breaks credentialed
> cross-origin requests once combined with `allow_credentials=True` — see §16.1), and
> `docker-compose.yml`'s `backend` service now has a `healthcheck` against `GET /api/ready` (§10)
> so this class of failure shows as `unhealthy` in `docker compose ps` rather than requiring a log
> read to discover.

---

## 4. Authentication & Session Resolution

### 4.1 Token chain

1. **Google OAuth** — NextAuth requests `openid email profile
   https://www.googleapis.com/auth/spreadsheets` with `access_type: "offline"` and
   `prompt: "consent"` (`frontend/src/auth.ts:52-54`).
2. **JWT callback** — first sign-in captures the Google access token, absolute expiry, and refresh
   token (`auth.ts:63-70`). Later calls treat the token as fresh while
   `Date.now() < expiresAt - 5*60*1000` (`auth.ts:76`); otherwise `refreshGoogleAccessToken()`
   performs a refresh grant (`auth.ts:13-22`), preserving the existing refresh token when Google
   returns none (`:37`).
3. **Backend token** — the `session` callback signs a fresh HS256 JWT on every session read
   (`auth.ts:107-109`) carrying `email`, `name`, `picture`, `sub`, `google_access_token`, and
   `exp` = now + 24 h (`auth.ts:98-105`), exposed as `session.apiToken`.

> **⚠ Lifetime mismatch.** The backend JWT has a 24-hour `exp` (`auth.ts:104`) but embeds a Google
> access token that lives about an hour. The WebSocket extracts that Google token exactly once at
> connect time (`chat.py:43`) and holds it for the connection's life, so a long-lived socket will
> present an expired Google credential — surfacing as a 401 from Google, not from FastAPI.

> **⚠ Refresh token never reaches the backend.** `auth.ts:98-105` includes only
> `google_access_token`. Every Sheets client built on the WebSocket path is therefore constructed
> with `refresh_token=None` (`tool_dispatch.py:29`, `:66`; `sheets/client.py:6`) and cannot
> self-refresh. Only the admin REST path passes one, from the `X-Google-Refresh-Token` header
> (`deps.py:78`, `admin.py:96-99`, `:137-140`).

### 4.2 HTTP authentication

`get_current_user` (`deps.py:14-73`) decodes the bearer token with HS256 and requires an `email`
claim (`:25-31`). On `JWTError` it falls back to a developer mode accepting any token beginning
`mock-` or containing `@` (`:32-41`); otherwise 401 (`:43-46`). Unknown emails are
**auto-provisioned** (`:56-67`).

> **⚠ Production-reachable auth bypass.** The `mock-`/`@` fallback (`deps.py:34`, mirrored at
> `chat.py:46`) is not gated by any environment flag. Any string containing `@` that fails
> signature verification is accepted as that identity, and the account is created on the spot.
> Since admin status is decided purely by email membership (`admin.py:23`), presenting the admin
> address as a bearer token reaches `require_admin`. `JWT_SECRET` also still has a shipped default
> (`config.py:14`) matching the frontend fallback (`auth.ts:5`), so a deployment that omits it
> signs with a publicly known key.

### 4.3 WebSocket authentication and project resolution

`authenticate_ws_user` (`chat.py:37-60`) mirrors the HTTP decoder but pulls `google_access_token`
from the payload (`:43`) and does **not** auto-provision — unknown email returns `None` and the
socket closes (`:58-59`).

The socket is accepted *before* authentication (`chat.py:74`), then closed with `1008` on failure
(`:81-83`). Project resolution is a three-step fallback: the `project_id` query parameter
(`:67`, `:89`); else the user's most recently active session's project (`:90-97`); else the first
`is_active` project (`:100-104`). All three failing closes the socket (`:106-112`).

A `sessions` row is loaded or created with `active_tab` = `project.default_tab` or `"SD"`
(`:123-135`). Conversation history is **in-process only**, initialised empty per connection
(`:138`) and reassigned each turn (`:277`) — nothing persists chat history.

---

## 5. Data Flow Traces

### 5.1 Write path — `update_cell`, end to end

| # | Function | Location | Action |
|---|---|---|---|
| 1 | `websocket_chat_endpoint` | `chat.py:190`, `:247` | receives frame, extracts `content` |
| 2 | `get_user_permissions` | `chat.py:267` → `permissions.py:77` | builds `PermissionChecker` |
| 3 | `run_agentic_loop` | `chat.py:277-291` | enters the LLM loop |
| 4 | LLM call | `agentic_loop.py:60` | model returns `tool_calls` |
| 5 | `send_websocket_msg` | `agentic_loop.py:120-124` | emits `tool_start` |
| 6 | `can_execute` | `agentic_loop.py:127` → `permissions.py:28` | RBAC gate; denial → `error` frame (`:131-134`) |
| 7 | `dispatch_tool` | `agentic_loop.py:137-148` → `tool_dispatch.py:6` | routes by tool name |
| 8 | `build_sheets_service` | `tool_dispatch.py:66` | client built **with no refresh token** |
| 9 | `get_row_raw` | `tool_dispatch.py:76` → `read.py:50` | pre-reads old values **live from Sheets** for the audit trail |
| 10 | `enqueue_write_job` | `tool_dispatch.py:85-94` → `producer.py:21` | `RPUSH` onto `migrationbot:write_queue` (`producer.py:53-57`) |
| 11 | return | `tool_dispatch.py:95-100` | `{ok: true, status: "queued", job_id}` — **optimistic**, before Sheets is touched |
| 12 | `send_websocket_msg` | `agentic_loop.py:151-155` | emits `tool_result` with that optimistic payload |
| — | *process boundary* | | |
| 13 | `start_worker` | `worker.py:278` | `BLPOP`, 10 s timeout |
| 14 | `process_job` | `worker.py:30` | rehydrates `WriteJobPayload` |
| 15 | project lookup | `worker.py:34-37` | re-reads `schema_config` from Postgres |
| 16 | `build_sheets_service` | `worker.py:41` | rebuilds client from the token in the job |
| 17 | `update_cell` | `worker.py:58-65` → `write.py:10` | the mutation |
| 18 | `find_row_num` | `write.py:29` → `read.py:28` | scans the ID column live from Sheets |
| 19 | `get_header_row` | `write.py:33` → `meta.py:45` | fetches headers |
| 20 | `resolve_column` | `write.py:42` → `column_mapper.py:120` | maps field name to a real header |
| 21 | `_with_retry(batchUpdate)` | `write.py:54-60` → `retry.py:7` | one `values().batchUpdate` |
| 22 | `_write_audit_record` | `worker.py:75-88` | one `audit_logs` row **per field** |
| 23 | `publish_queue_update` | `worker.py:255-263` → `events.py:22` | publishes terminal state to `migrationbot:queue_events:<email>` |
| 24 | `forward_queue_updates` | `chat.py:157-182` | API's per-connection subscriber relays it as a `queue_update` frame |
| 25 | throttle | `worker.py:297` | `asyncio.sleep(1.0)` before the next job |

Steps 23–24 are the completion-feedback path. `publish_queue_update` is called on **every**
terminal path including the unsupported-tool branch (`worker.py:231-233`, which sets `error_msg`)
and the exception handler (`:235-252`), and a publish failure is swallowed so a successful write is
never failed by a notification problem (`events.py:51-53`). The API subscribes per connection
(`chat.py:184`) and tears the task down in `finally` (`:305-311`). Because the agentic loop and the
relay both write to one socket, sends are serialised behind an `asyncio.Lock` (`chat.py:151-155`).

`update_cell` returns `{"ok": True, ...}` unconditionally once `batchUpdate` returns
(`write.py:62-67`) — the API response body is not inspected.

### 5.2 Read path — `search_rows`

| # | Function | Location | Action |
|---|---|---|---|
| 1–7 | as the write path, through `dispatch_tool` | | |
| 8 | read branch | `tool_dispatch.py:26` | membership test against the 5-name read tuple |
| 9 | `search_rows` | `tool_dispatch.py:36-45` → `read.py:156` | filters, `return_fields`, `limit` (default 20) |
| 10 | `get_header_row` | `read.py:170` → `meta.py:45` | **API call 1** — headers, each cell `.strip()`ed (`meta.py:52`) |
| 11 | `col_idx` build | `read.py:176` | `{h.lower().strip(): i}` — normalised on both sides, fixed in Phase 1 (§16 history) |
| 12 | `resolve_column` | `read.py:190` | maps each filter term to a canonical name |
| 13 | mapping guard | `read.py:191-193` | normalised canonical not in `col_idx` → returns `{"ok": false, "error": ...}` |
| 14 | bulk fetch | `read.py:200-204` | **API call 2** — `{tab}!{start}:{start+2000}` |
| 15 | matching | `read.py:209-234` | AND across filters; `blank` / `contains` / `exact` |
| 16 | truncation | `read.py:232-233`, `:239` | stops at `limit`; reports `capped` |

Two Sheets API calls per search, and no cache — every `search_rows`, `summarize`, and
`data_quality` invocation re-reads up to 2001 rows from Google (`read.py:203`, `:274`, `:437`).

---

## 6. RBAC Model

### 6.1 Two authorisation systems

- **Config admin** governs `/api/admin/*`: `require_admin` compares the caller's email against
  `settings.admin_emails_list` (`admin.py:22-28`), re-read from `os.environ` per access
  (`config.py:27-30`).
- **Row role** governs tool execution: `permissions.role` ∈ `admin`/`editor`/`viewer`
  (`models/permission.py:33`).

They bridge one way only — a config admin short-circuits to `role="admin"` before any DB lookup
(`permissions.py:84-85`). A row-level `admin` gets no REST access.

### 6.2 Enforcement

Exactly one point: `checker.can_execute(...)` in the agentic loop before every dispatch
(`agentic_loop.py:127`). `dispatch_tool` performs no check of its own (`tool_dispatch.py:6-108`).
Frontend admin gating is cosmetic (`chat/page.tsx` fetches `/api/me`'s `is_admin`).

```python
READ_ONLY_TOOLS = {"get_row", "search_rows", "summarize", "switch_module", "data_quality"}
WRITE_TOOLS     = {"update_cell", "bulk_update", "format_row", "add_row"}
```
— `permissions.py:8-9`.

| Role | Reads | Writes | Field limits | `denied_operations` |
|---|---|---|---|---|
| **Config admin** | all | all | none | not consulted |
| **Row admin** | all | all | none — returns at `permissions.py:34-35` | **not consulted** |
| **Editor** (incl. default) | all | all not in `denied_ops` | `update_cell` (`:52-63`), `bulk_update` (`:66-72`) only | honoured (`:45-49`) |
| **Viewer** | all | none (`:37-43`) | n/a | **not consulted** |

Field enforcement is skipped entirely when `allowed_fields == ["*"]` (`:52`, `:66`), and covers only
those two tools — `add_row`'s free-form `fields` dict and `format_row` are ungated.

> **⚠ RBAC is fail-open.** `get_user_permissions` returns **editor with `["*"]`** when no
> `project_id` is given (`permissions.py:90-91`), when no `users` row matches (`:94-97`), and when
> no `permissions` row exists (`:113`). Combined with the WebSocket's "first active project"
> fallback (`chat.py:100-104`), a user who was never granted anything lands on an arbitrary project
> as an editor.

> **⚠ `WRITE_TOOLS` is never read.** Defined at `permissions.py:9` but `can_execute` only tests
> `READ_ONLY_TOOLS` (`:38`). Classification is by exclusion, so a new read tool omitted from that
> set is silently denied to viewers.

---

## 7. Schema & Column Resolution

### 7.1 `schema_config` shape

JSONB defaulting to `{}` (`models/project.py:23`), in one of two shapes distinguished by a
top-level `"tabs"` key: multi-tab (`{"tabs": {...}, "global": {...}}`, what `detect_all_tabs`
produces at `schema_detect.py:73-79`) or flat. The disambiguation is reimplemented at
`read.py:14-17`, `write.py:23`, `:82`, `:193`, `worker.py:188`, `chat.py:271`, and
`agentic_loop.py:32-35` — seven copies, one of them (`read.py`) a private `_get_tab_schema`
function.

Per-tab defaults are applied inline at each use: `data_start_row` → `3`, `primary_id_position` →
`"B"`, `primary_id_column` → `"RICEFW ID"`, `assignee_column` → `"Technical Resource "` (with
trailing space), `critical_fields` → a six-name list (`read.py:181`). `header_row_num` is always
`data_start_row - 1`.

### 7.2 Column-name resolution and the whitespace asymmetry

`resolve_column(term, column_map)` (`column_mapper.py:120-140`) resolves in three stages: exact
match on canonical keys, alias-list match (both compared case-insensitively and stripped), then
`difflib.get_close_matches` at `cutoff=0.6`. It returns the **unstripped** canonical key.

`COLUMN_ALIASES` deliberately preserves the tracker's real header typos and trailing spaces —
`"Technical Resource "`, `"Functinal Resource "`, `"Color "`, `"Programe Name"`
(`column_mapper.py:70-76`).

`get_header_row` strips every header cell (`meta.py:52`). Both read and write paths now treat that
the same way: build `{h.lower().strip(): i}` and look up `canonical.lower().strip()`.

- **Write path**: `write.py:34`/`:43`, `:90`/`:96`.
- **Read path**: `read.py:176` (`search_rows`) and `read.py:267` plus its `_col()` helper
  (`summarize`, all four report branches). Previously these built `{h: i}` and tested `canonical in
  col_idx` verbatim, which missed any canonical name carrying trailing whitespace — fixed in the
  Phase 1 remediation; regression tests in `test_sheets_logic.py` cover both `search_rows` and
  `summarize.count_by_field` against a header with a real trailing space.

### 7.3 Auto-detection

`detect_all_tabs` (`schema_detect.py:19`) enumerates tabs, reads `A1:Z10` from each, and asks
`deepseek-chat` at `temperature=0.1` to classify the tab and map columns (`:128-157`). Non-tracker
tabs are skipped (`:58-60`); `data_start_row` = `header_row_index + 2` (`:64`). On LLM failure a
hard-coded fallback is returned with `is_tracker_sheet: True` (`:167-187`) — so an outage registers
every tab, including cover pages, as a tracker.

`build_column_map` (`column_mapper.py:151-210`), a two-pass LLM alias generator, is never called
from anywhere in `backend/`; `detect_all_tabs` writes no `column_map` key. Every deployment
therefore runs on the static `COLUMN_ALIASES` unless an admin hand-edits `schema_config`.

---

## 8. The Agentic Loop

`run_agentic_loop` (`agentic_loop.py:12-200`) drives at most **8** iterations (`:26`).

- **Model routing** — `select_model` returns `deepseek-reasoner` only on iteration 0 and only when
  the latest user message contains a conditional keyword (`llm_router.py:5`, `:9-21`); everything
  else uses `deepseek-chat`.
- **Prompt swapping** — iteration 0 sends the full prompt with the serialised column map
  (`:39`); from iteration 1 the system message is replaced in place with a compact variant (`:46-47`).
- **DSML leakage guard** — content containing `<｜｜DSML｜｜>` triggers one retry against
  `deepseek-chat` (`:66-76`).
- **CoT suppression** — `reasoning_content` is logged, never forwarded (`:78-82`).
- **Self-repair** — a failed tool result gets a `[System Recovery Note]` appended (`:157-164`).
- **Termination** — no `tool_calls` → emit `assistant` with `done: true`, break (`:100-106`); any
  exception → emit `error`, break (`:173-179`); iteration cap exhausted without either → the `for`
  loop's `else:` clause emits `error` (`:180-187`, added in Phase 1 — previously this path sent
  nothing, leaving the client's pre-seeded empty assistant bubble spinning indefinitely).

### 8.1 Tool catalogue

Nine tools in `core/tool_schemas.py:TOOLS`: `get_row`, `update_cell`, `format_row`, `add_row`
(`:79`), `bulk_update`, `search_rows`, `summarize`, `switch_module`, `data_quality` (`:337`).

---

## 9. WebSocket Protocol Reference

Endpoint `WS /ws?token=<apiToken>[&project_id=<int>]` (`chat.py:61-66`). The client derives the URL
from `window.location` unless `NEXT_PUBLIC_WS_URL` is set (`useWebSocket.ts:26-29`);
`docker-compose.yml:58` leaves it empty, so the window-derived value is used.

### 9.1 Client → server

The server routes on the frame's declared `type` (`chat.py:190-200`) rather than only ever reading
`content` — a non-JSON or untyped frame is normalized to `{"type": "message", "content": <raw
text>}` (`:191-194`) before dispatch.

| `type` | Payload | Sent by | Server handling |
|---|---|---|---|
| `message` | `{type, content}` | `useWebSocket.ts:193` | extracts `content` (`:247`), drives `run_agentic_loop` (`:277-291`) |
| `ping` | `{type: "ping"}` | `useWebSocket.ts:42`, every 30 s | replies `pong` (`chat.py:202-204`) |
| `switch_tab` | `{type, tab_name}` | `useWebSocket.ts:199-203`, on tab-button click | RBAC-checked (`can_execute("switch_module", {})`, `chat.py:221-225`), then `switch_module` verifies the tab exists live against Sheets and updates `sessions.active_tab` (`chat.py:227-235`, `meta.py:117-154`) |

Any other `type` is ignored rather than misread as chat text (`chat.py:243-245`).

### 9.2 Server → client

| `type` | Payload | Emitted by | Client consumer | Effect |
|---|---|---|---|---|
| `connection_ok` | `{type, user_email, project_name, active_tab}` | `chat.py:139-144` | `useWebSocket.ts:147-154` | `setSessionInfo` applies all three fields |
| `assistant` | `{type, content, done: true}` | `agentic_loop.py:101-105` | `useWebSocket.ts:74-87` | appends to the most recent assistant message |
| `tool_start` | `{type, tool, args}` | `agentic_loop.py:120-124` | `useWebSocket.ts:88-103` | pushes `{name, args, status:"running"}` |
| `tool_result` | `{type, tool, result}` | `agentic_loop.py:151-155` | `useWebSocket.ts:104-120` | marks the matching running entry `"completed"` or `"failed"` per `result.ok` |
| `tab_switched` | `{type, active_tab}` | `chat.py:238` | `useWebSocket.ts:126-131` | `setActiveTab(active_tab)` — the client no longer sets it optimistically (§14) |
| `error` | `{type, message}` | `chat.py:81`, `:107-110`, `:118`, `:209`, `:224`, `:240`, `:302`; `agentic_loop.py:131-134`, `:184-187`, `:194-197` | `useWebSocket.ts:132-146` | appends a `system` message, deduped against an identical predecessor |
| `pong` | `{type: "pong"}` | `chat.py:203` | `useWebSocket.ts:155-156` | no-op |
| `queue_update` | `{type, job_id, status, tool_name, args, session_id, error}` | `events.py:35-43` via `worker.py:255-263`, relayed by `chat.py:167` | `useWebSocket.ts:121-124` → DOM `CustomEvent` → `chat/page.tsx:103-125` | toast keyed on `status` |

`status` is `"completed"` or `"failed"` (`worker.py:258`); the frontend also handles a generic
"other" branch (`chat/page.tsx:116-118`), which nothing currently emits.

`updateLastMessage` targets by explicit `targetId` or most-recent-role rather than always the final
element (`useChatStore.ts:68-87`), so interleaved `assistant` and `tool_*` frames no longer corrupt
each other.

### 9.3 Lifecycle

Reconnect after 3 s for any close code other than `1008` or `1000` (`useWebSocket.ts:57-62`).
Heartbeat every 30 s (`:39-44`). No server-side idle handling — `chat.py:190`'s `receive_text()`
blocks indefinitely.
`connect` tracks the live socket in a ref (`wsRef`, `:10`, `:18-24`, `:165`) rather than the Zustand
`ws` state, so a reconnect always closes the actual current socket regardless of which render's
closure is running (§14).

---

## 10. REST Surface

Admin routes (all gated by `require_admin`, `admin.py:19`, `:22-28`):

| Method | Path | Purpose | Citation |
|---|---|---|---|
| GET | `/admin/projects` | list all projects | `admin.py:64-78` |
| POST | `/admin/projects/detect-metadata` | LLM tab detection, no persist | `admin.py:81-110` |
| POST | `/admin/projects` | create; auto-detects `schema_config` when omitted | `admin.py:113-165` |
| PUT | `/admin/projects/{id}` | patch fields incl. raw `schema_config` | `admin.py:168-188` |
| DELETE | `/admin/projects/{id}` | delete, cascades to permissions | `admin.py:191-201` |
| PATCH | `/admin/projects/{id}/fields` | toggle a `critical_fields` entry | `admin.py:204-235` |
| GET/POST/DELETE | `/admin/permissions[/{id}]` | RBAC CRUD; POST creates the user if absent | `admin.py:240-316` |
| GET | `/admin/audits` | filter by user/tool/RICEFW ID, limit 100 | `admin.py:321-357` |
| GET | `/admin/analytics/summary` | counts and failure totals | `admin.py:360-388` |

Non-admin: `GET /api/health` (`health.py:10-13`, liveness — static literal, no dependency
probing), `GET /api/ready` (`health.py:16-48`, added in Phase 0 of the remediation plan — live
`SELECT 1` against Postgres and `PING` against the shared `producer.redis_client`, `503` with a
per-service `detail` dict when either fails; verified against unreachable dependencies), `GET
/api/me` (`api/auth.py:19-34`, mounted directly in `main.py:54-55`), and `GET /api/projects`
(`chat.py:314-326`).

`docker-compose.yml`'s `backend` service now runs a `healthcheck` against `/api/ready` (interval
30 s, 3 retries, `start_period` 15 s) — see §3.

> **⚠ `/api/projects` performs no authorisation filtering.** Every authenticated caller receives all
> active projects including `spreadsheet_id` and full `schema_config` (`chat.py:317-326`). With the
> fail-open default (§6.2) and the verbatim `project_id` query parameter (`chat.py:67`, `:89`), a
> user can select any project from that list and operate on it as an editor.

---

## 11. Google Sheets Integration

`build_sheets_service(access_token, refresh_token=None)` builds `Credentials` with the process
client ID/secret and disables discovery caching (`sheets/client.py:11-19`). There is no service
account — every call is made as the signed-in user, which is what makes audit attribution real.

`_with_retry(fn)` (`retry.py:7-32`) is the sole sync bridge: it dispatches onto a module-level
`ThreadPoolExecutor(max_workers=4)` (`:5`, `:20`) and retries with exponential backoff (base 1 s,
doubling) on HTTP `{429, 500, 503}` for up to 4 attempts (`:13`, `:25-27`). Other errors propagate.

| Function | File | Sheets calls |
|---|---|---|
| `get_header_row` | `meta.py:45-52` | 1 — strips every cell (`:52`) |
| `get_sheet_id` | `meta.py:34-42` | 1 |
| `find_row_num` | `read.py:28-47` | 1 — full ID-column scan |
| `get_row` | `read.py:126-153` | 3 — `find_row_num` + row fetch + headers |
| `get_row_raw` | `read.py:50-77` | 3 — same shape |
| `search_rows` | `read.py:156-247` | 2 — headers + ≤2001 rows |
| `summarize` | `read.py:250-417` | 2 |
| `run_data_quality_check` | `read.py:424-491` | 2 (+1 or +2 for `consistency`/`stale` DB reads) |
| `update_cell` | `write.py:10-67` | 3 — `find_row_num` + headers + one `batchUpdate` |
| `bulk_update` | `write.py:70-175` | 2 + **one `find_row_num` per target ID** (`:126-134`) |
| `add_row` | `write.py:178-233` | 1 + headers, `values().append` |
| `format_row` | `format.py:18-93` | `spreadsheets().batchUpdate` `repeatCell` |

`next_ricefw_id` (`meta.py:79-114`) computes max+1 over parsed IDs, formatted `%03d`;
`switch_module` (`meta.py:117-154`) optionally verifies the tab exists then updates
`sessions.active_tab`.

> **⚠ Compounding read cost on bulk paths.** `get_bulk_rows_raw` calls `get_row_raw` once per
> target ID (`read.py:119-122`), and each of those is three Sheets calls — so the audit pre-read
> alone costs ~3N calls, before `bulk_update` then spends another N on `find_row_num`
> (`write.py:127`). A 50-row bulk update is on the order of 200 API calls against a quota-limited
> endpoint, throttled at one job/second by the worker.

---

## 12. Audit Logging

`audit_logs` rows are written only by `_write_audit_record` (`audit.py:10-47`), which opens its own
`AsyncSessionLocal` (`:29`) rather than joining the caller's transaction and swallows every
exception (`:46-47`) so an audit failure cannot fail a mutation. Its only caller is the worker
(`worker.py:18`).

| Tool | Rows | Citation |
|---|---|---|
| `update_cell` | one per updated field | `worker.py:71-88` |
| `bulk_update` | one per succeeded **and** per failed ID | `worker.py:106-121`, `:124-140` |
| `format_row` | one, `field="Color"` | `worker.py:160-173` |
| `add_row` | one, `field="ID"` | `worker.py:217-230` (ID always server-computed via `next_ricefw_id`, `prefix=None` — the schema exposes no `ricefw_id`/`prefix` arg) |
| exception | one, `field="Mutation"` | `worker.py:239-252` |

`old_value` comes from the live pre-read at dispatch time (`tool_dispatch.py:76`, `:79`). Read-only
tools produce no audit rows.

`log_audit` (`audit.py:50-82`), a fire-and-forget `create_task` wrapper, is never called.

---

## 13. Data Quality Engine

`DataQualityChecker` (`core/data_quality.py`) takes headers, a row matrix, and a tab schema,
building a case-insensitive header index at construction and resolving schema-named columns through
`_get_col_idx`.

| Method | Behaviour |
|---|---|
| `blank_field_counts` | blank count per named column; missing column counts as fully blank |
| `stale_items` | latest mutation per ID from audit rows, excluding done statuses; no history → `"Never (no logs)"`, `days_inactive: 999` |
| `consistency_checks` | four rules — completed-without-signoff, completed-without-completion-date, required-with-blank-status, assignee-not-in-known-emails (`:108-209`) |
| `completeness_score` | fill rate across `critical_fields`; `100.0` when no rows or no resolvable columns (`:211-241`) |

`run_data_quality_check` (`read.py:424-491`) reads headers and ≤2001 rows live, then dispatches on
the **required** `check_type` arg (`blank_fields` / `consistency` / `stale` / `completeness_score` /
`all`) rather than always running every check — each branch only does the work (and, for
`consistency`/`stale`, the DB queries) its check needs. `scope_module` filters `rows` to one module
before the checker is built; `blank_fields` reads `fields` from args (default: the same six-column
list `search_rows` defaults to) and calls the previously-dead `DataQualityChecker.blank_field_counts`
(`data_quality.py:20-33`). `consistency` sources "known emails" from
`SELECT DISTINCT audit_logs.user_email` (`read.py:474-476`); `stale` pulls audit timestamps scoped to
the spreadsheet and tab (`read.py:484-488`) using `threshold_days` (the schema's actual arg name —
the code previously read a `stale_threshold_days` key the schema never sends).

> **⚠ "Registered users" is drawn from the audit log**, not `users` or `permissions`
> (`read.py:474-476`) — a legitimate user who has never performed a write is reported as
> unregistered.

---

## 14. Frontend Application

Next.js 16 App Router, `output: "standalone"` (`next.config.ts:4`), `SessionProvider` at the root
layout.

| Route | File | Purpose |
|---|---|---|
| `/` | `app/page.tsx` | landing, `signIn` |
| `/chat` | `app/chat/page.tsx` | main chat UI |
| `/admin` | `app/admin/page.tsx` | dashboard; four parallel fetches, Recharts |
| `/admin/projects` | `app/admin/projects/page.tsx` | project CRUD + tab detection; sends Google token headers |
| `/admin/users` | `app/admin/users/page.tsx` | permission upsert/delete |
| `/admin/audit` | `app/admin/audit/page.tsx` | filtered audit browser |

State lives in one Zustand store (`useChatStore.ts:49-95`) holding `projects`, `activeProject`,
`activeTab`, `isConnected`, `messages`, `ws`, and session metadata.

Tab switching is an explicit control frame, not prompt-driven (`chat/page.tsx:148-150`): the client
sends `{type: "switch_tab", tab_name}` and only applies the new `activeTab` once the server confirms
with `tab_switched` (§9.1–9.2) — it no longer sets `activeTab` optimistically before the switch is
known to have succeeded.

`connect` tracks the live socket in a ref (`wsRef`, `useWebSocket.ts:10`) rather than reading the
Zustand `ws` state through its own closure, so a reconnect from `onclose`'s `setTimeout` (`:59-61`)
or an effect re-run always closes the actual current socket — not a value captured at whatever
render created that particular `connect` closure.

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

`backend` and `worker` both load `.env` via `env_file` (`docker-compose.yml:27-28`, `:43-44`) with
`DATABASE_URL`/`REDIS_URL` overridden to compose service names (`:30-31`, `:46-47`). Volumes:
`pgdata`, `caddy_data`, `caddy_config` — **Redis has none**, so the write queue is memory-only.

Caddy routes in order (`Caddyfile:2-17`): `/api/me` → backend (carved out before the NextAuth
wildcard), `/api/auth/*` → frontend, `/api/*` → backend, `/ws*` → backend, `*` → frontend.

### 15.2 CI/CD

`ci.yml` runs two jobs on push/PR to `main`/`develop`/`phase3`: `lint-and-test` (Postgres + Redis
service containers, `pytest -v` from `backend/` with `DATABASE_URL` pointed at `migrationbot_test`
and `CORS_ORIGINS`/`ADMIN_EMAILS`/`DEFAULT_SPREADSHEET_ID` supplied at `:59-61`) and
`frontend-build` (`npm ci && npm run build`). Despite the job name there is **no linter** in either.

`deploy.yml` SSHes to a VPS on push to `main`/`phase3`, then `git fetch origin main`,
`git reset --hard origin/main`, `docker compose down`, `build --no-cache`, `up -d`, `image prune`.

> **⚠ Deploy pipeline risks.** Full downtime on every merge (`down` before a from-scratch rebuild).
> The workflow triggers on `phase3` but hard-resets to `origin/main`, so a `phase3` push redeploys
> main's code. Both an SSH key and a password are passed where one would do. `.env` itself survives
> — it is gitignored (`.gitignore:7`) and `git reset --hard` does not touch ignored files — but any
> *other* untracked file in the repo directory is discarded silently.

### 15.3 Tests

20 tests across `tests/test_db.py` (4), `tests/test_core/test_core_logic.py` (5),
`tests/test_sheets/test_sheets_logic.py` (8), `tests/integration/test_integration_logic.py` (3).
`test_db.py` and the integration suite each define their own `setup_*` and `db_session` fixtures
locally, both autouse (`test_db.py:15`, `test_integration_logic.py:22`).

**Both are now gated (Phase 0).** `require_test_database()` (`tests/conftest.py:9-26`) parses the
database name out of `settings.DATABASE_URL` and raises `RuntimeError` unless it contains `"test"`
(case-insensitive) — the convention `ci.yml:55` already uses (`migrationbot_test`). It is called
from `setup_test_db` (`test_db.py:16`) and `setup_integration_db` (`test_integration_logic.py:23`)
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
**Severity: medium.** `main.py:44-49` sets `allow_credentials=True` with origins split from
`CORS_ORIGINS` (`:43`). `.env.example:14` ships `CORS_ORIGINS=*`. Starlette does not expand a
literal `"*"` here, so credentialed cross-origin requests fail rather than being permitted — and
the example file steers deployments toward exactly that value.

### 16.2 `/api/projects` performs no authorisation filtering
**Severity: medium.** `chat.py:314-326` — see §10.

### 16.3 RBAC is fail-open
**Severity: medium.** `permissions.py:88`, `:113` — see §6.2.

### 16.4 Auth bypass and default `JWT_SECRET`
**Severity: medium.** `deps.py:34`, `chat.py:46`, `config.py:14` — see §4.2.

### 16.5 Queue has no durability, retry, or dead-letter path
**Severity: medium.** A plain Redis list (`producer.py:53-57`) on a volume-less container
(`docker-compose.yml:17-22`). `BLPOP` removes the job before processing (`worker.py:278`), so a
crash mid-`process_job` loses the write with no record beyond an audit row that is only written if
the exception was caught (`:235-252`), not if the process dies. No retry, no dead-letter list, and
no job-state key — the `job_id` returned to the user (`tool_dispatch.py:98`) is stored nowhere and
cannot be queried after the fact.

### 16.6 OAuth access tokens are serialised into the queue
**Severity: medium.** `WriteJobPayload` carries `google_access_token` as a plain field
(`queue/schemas.py:11`), JSON-serialised into the Redis entry (`producer.py:48-57`) so the worker
can rebuild a client (`worker.py:41`). Live user credentials sit in a Redis instance with no auth
and port 6379 published to the host (`docker-compose.yml:20-21`) for as long as the job is queued.
Inherent to the OAuth-only design plus the queue boundary; noted, not solved.

### 16.7 Silent 2001-row ceiling on every scan
**Severity: low-medium.** `read.py:203`, `:274`, `:441` request `{start}:{start+2000}`. A tracker
larger than that is silently truncated — `search_rows` reports fewer matches, `summarize`
percentages are computed over a partial denominator, and `data_quality` scores a subset, all with
no indication to the user.

### 16.8 Seven copies of the schema-shape branch
**Severity: low (maintenance).** `read.py:14-17`, `write.py:23`, `:82`, `:193`, `worker.py:188`,
`chat.py:271`, `agentic_loop.py:32-35`. Per-key defaults are similarly scattered.

### 16.9 Dead code
**Severity: low.** `core/planner.py` and `core/memory.py` (never imported); `column_mapper.py:151`
`build_column_map` and `:143` `get_column_map_json` (never called — §7.3); `audit.py:50`
`log_audit`; `permissions.py:9` `WRITE_TOOLS`; `permissions.py:25-26` `is_admin()`;
`meta.py:10` `_detect_header_row` (imported by `read.py:5`, called nowhere);
`models/audit_log.py` `created_month` (no partitioning exists).

### 16.10 Startup `init_db()` failure is non-fatal, and readiness doesn't fully cover it
**Severity: low.** `init_db()` failures are caught and execution continues (`main.py:26-28`).
`GET /api/ready` (`health.py:16-47`, added in Phase 0) narrows this but does not close it: it runs
a live `SELECT 1` (`health.py:28`), which succeeds against a reachable Postgres regardless of
whether `init_db()` ever created the application's tables — `SELECT 1` needs no table. So the
probe catches "Postgres is down" (which is what caused the 2026-08-06 outage — see §3) but not
"Postgres is up with an empty schema". Closing that gap needs `main.py`'s startup handler to record
`init_db()`'s outcome and have `/api/ready` report unready when it failed.

---

## 17. Appendix — Environment Variables

| Variable | Consumed by | Default |
|---|---|---|
| `DATABASE_URL` | `db/engine.py:9` | `…@localhost:5433/migrationbot` (`config.py:7`) |
| `REDIS_URL` | `producer.py:12`, `events.py:14`, `worker.py:269`, `chat.py:159` | `redis://localhost:6379` (`config.py:8`) |
| `DEEPSEEK_API_KEY` | `chat.py:33` | `"mock-deepseek-key"` (`config.py:11`) |
| `GOOGLE_CLIENT_ID` / `_SECRET` | `sheets/client.py:15-16`, `auth.ts:48-49` | mock values (`config.py:12-13`) |
| `JWT_SECRET` | `deps.py:25`, `chat.py:41`, `auth.ts:5` | `"mock-jwt-secret-…"` (`config.py:14`) |
| **`CORS_ORIGINS`** | `main.py:43` | **required — no default** (`config.py:24`) |
| **`ADMIN_EMAILS`** | `config.py:29` | **required — no default** (`config.py:23`) |
| **`DEFAULT_SPREADSHEET_ID`** | declared `config.py:18` | **required — no default**; referenced nowhere in `backend/app/` |
| `DEFAULT_SHEET_TAB` / `_LABEL` | declared `config.py:19-20` | defaults present; referenced nowhere in `backend/app/` |
| `NEXTAUTH_SECRET` / `NEXTAUTH_URL` | `auth.ts:114`, `docker-compose.yml:59-60` | secret falls back to `JWT_SECRET` |
| `DB_PASSWORD` | `docker-compose.yml:10`, `:30`, `:46` | none — compose only |
| `NEXT_PUBLIC_WS_URL` | `useWebSocket.ts:28` | empty in compose (`docker-compose.yml:58`) |
| `VPS_HOST` / `VPS_USER` / `VPS_SSH_KEY` / `VPS_PASSWORD` | `deploy.yml:14-17` | GitHub secrets |
