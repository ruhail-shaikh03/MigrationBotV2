# MigrationBot Enterprise Portal — Technical Design Document

**Version:** 2.0 (Enterprise Portal)  
**Team:** FF Team  
**Status:** Active Development (Phase 2 — Partial Integration)  
**Stack:** Next.js 15 · FastAPI · PostgreSQL · Redis · DeepSeek · Google Sheets API · Google OAuth 2.0  
**Deployment Target:** Hetzner CX22 VPS via Docker Compose + Caddy reverse proxy  
**Domain:** `migrationbot.duckdns.org`

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Repository Structure](#2-repository-structure)
3. [Authentication & Session Management](#3-authentication--session-management)
4. [Database Schema (PostgreSQL)](#4-database-schema-postgresql)
5. [LLM Orchestration & Agentic Loop](#5-llm-orchestration--agentic-loop)
6. [Tool System & RBAC Enforcement](#6-tool-system--rbac-enforcement)
7. [Embedded Sheets Layer](#7-embedded-sheets-layer)
8. [Queue-Backed Write Path](#8-queue-backed-write-path)
9. [Column Mapping (LLM-Driven & Fallbacks)](#9-column-mapping-llm-driven--fallbacks)
10. [Schema Auto-Detection](#10-schema-auto-detection)
11. [Audit Logging System](#11-audit-logging-system)
12. [Data Quality & Analytics Engine](#12-data-quality--analytics-engine)
13. [Next.js Frontend](#13-nextjs-frontend)
14. [WebSocket Protocol](#14-websocket-protocol)
15. [Deployment Architecture](#15-deployment-architecture)
16. [Performance Architecture](#16-performance-architecture)
17. [Feature Interaction Map](#17-feature-interaction-map)
18. [Known Bugs, Discrepancies & Technical Debt](#18-known-bugs-discrepancies--technical-debt)

---

## 1. System Overview

MigrationBot Enterprise Portal is the second-generation implementation of the MigrationBot conversational AI assistant, migrated from a synchronous Streamlit prototype (v3.0) to a high-concurrency event-driven architecture. The system allows SAP team members to interact with S/4HANA WRICEF Migration Tracker Google Sheets via natural language.

### Architecture Summary

| Layer | Technology | Responsibility |
|-------|-----------|----------------|
| Frontend | Next.js 15 (App Router) + Tailwind CSS + shadcn/ui | Auth, Chat UI, Admin Dashboard |
| Backend | FastAPI (async) | WebSocket chat, REST admin API, Agentic loop |
| LLM | DeepSeek (`deepseek-chat`, `deepseek-reasoner`) via `AsyncOpenAI` | Intent parsing, tool selection, response composition |
| Data Source | Google Sheets API v4 | WRICEF tracker data (reads and writes) |
| Auth | NextAuth.js v5 (Google OAuth) → HS256 JWT → FastAPI | Single-scope OAuth, JWT-based API auth |
| RBAC/Audit | PostgreSQL | Users, permissions, projects, audit logs |
| Write Queue | Redis (RPUSH/BLPOP FIFO) | Throttled mutation pipeline (1 req/sec) |
| Reverse Proxy | Caddy 2 | Auto-HTTPS, WebSocket proxying, route splitting |

### Request Lifecycle

```text
User types message in Next.js Chat UI
        │
        ▼
WebSocket sends JSON: {"type": "message", "content": "..."}
        │
        ▼
FastAPI /ws endpoint receives, authenticates via JWT query param
        │
        ▼
run_agentic_loop() invoked with user context
        │
        ├── Iteration 0: select_model() → deepseek-reasoner (if conditional) or deepseek-chat
        │         │
        │         ▼
        │   AsyncOpenAI.chat.completions.create()
        │         │
        │         ├── finish_reason == "stop" → stream final reply via WS
        │         │
        │         └── finish_reason == "tool_calls"
        │                   │
        │                   ▼
        │             PermissionChecker.can_execute() — RBAC interception
        │                   │
        │                   ├── READ tool → dispatch_tool() → Sheets API (direct)
        │                   │
        │                   └── WRITE tool → dispatch_tool() → Redis queue (throttled)
        │                              │
        │                              ▼
        │                        Worker processes at 1 req/sec
        │                              │
        │                              ▼
        │                        Audit log → PostgreSQL (non-blocking)
        │
        └── Iterations 1-7: deepseek-chat processes tool results
                  │
                  ▼
        Final assistant message streamed via WebSocket
```

### Design Principles

- **No service accounts:** Every Sheets API call uses the signed-in user's own Google OAuth access token, forwarded from NextAuth via JWT claims.
- **Async everywhere:** FastAPI uses `AsyncOpenAI`, `asyncpg`, and `redis.asyncio` for non-blocking I/O.
- **Queue-backed writes:** All mutations are enqueued to Redis and executed by a throttled worker process to prevent Google Sheets API quota exhaustion.
- **Schema-driven:** Column references are resolved via `schema_config` (JSONB per project) rather than hardcoded names.
- **RBAC at dispatch layer:** Permissions are checked before every tool execution, not at the UI layer.

---

## 2. Repository Structure

```text
migrationbot/
├── _legacy/                         # Reference-only v3.0 Streamlit code
│   ├── llm/
│   │   ├── deepseek_client.py       # Sync OpenAI client (superseded by AsyncOpenAI)
│   │   └── tools.py                 # Original tool schemas & system prompts
│   ├── sheets/
│   │   ├── column_map.py            # Static alias dictionary (ported to core/column_mapper.py)
│   │   ├── dynamic_column_mapper.py # LLM 2-pass mapper (ported to core/column_mapper.py)
│   │   ├── executor.py              # SheetsExecutor (logic split into sheets/ submodules)
│   │   ├── project_registry.py      # Config sheet project CRUD (replaced by PostgreSQL)
│   │   └── sheet_registry.py        # Active sheet tracking (replaced by sessions table)
│   ├── permissions.py               # PermissionChecker (ported to core/permissions.py)
│   ├── audit.py                     # AuditLogger (ported to core/audit.py)
│   ├── data_quality.py              # DataQualityChecker (ported to core/data_quality.py)
│   └── sheets_auth.py               # Credential builder (ported to sheets/client.py)
│
├── backend/                         # FastAPI application
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                  # FastAPI app entry, CORS, lifespan, router registration
│   │   ├── config.py                # Pydantic Settings (reads from .env)
│   │   ├── deps.py                  # JWT decode dependency, Google token extraction
│   │   │
│   │   ├── api/                     # HTTP/WS endpoints
│   │   │   ├── auth.py              # GET /api/auth/me — user profile
│   │   │   ├── chat.py              # WS /ws — agentic chat + GET /api/projects
│   │   │   ├── admin.py             # CRUD: projects, permissions, audit logs
│   │   │   └── health.py            # GET /api/health
│   │   │
│   │   ├── core/                    # Business logic
│   │   │   ├── agentic_loop.py      # Multi-turn tool-chaining engine
│   │   │   ├── llm_router.py        # Complexity-based model selector
│   │   │   ├── tool_schemas.py      # 9 tool definitions + system prompts
│   │   │   ├── tool_dispatch.py     # Read/write routing dispatcher
│   │   │   ├── permissions.py       # PermissionChecker + DB resolver
│   │   │   ├── audit.py             # Non-blocking audit logger (asyncio.create_task)
│   │   │   ├── data_quality.py      # DataQualityChecker (schema_config-driven)
│   │   │   ├── column_mapper.py     # Static aliases + LLM 2-pass mapper + resolve_column()
│   │   │   └── schema_detect.py     # LLM-based schema auto-detection for new projects
│   │   │
│   │   ├── sheets/                  # Embedded Google Sheets layer
│   │   │   ├── client.py            # OAuth → googleapiclient service builder
│   │   │   ├── read.py              # get_row, search_rows, summarize, data_quality
│   │   │   ├── write.py             # update_cell, bulk_update, add_row
│   │   │   ├── format.py            # format_row (batchUpdate color formatting)
│   │   │   ├── meta.py              # Header detection, ID generation, tab switching
│   │   │   └── retry.py             # Exponential backoff (429/500/503)
│   │   │
│   │   ├── models/                  # SQLAlchemy ORM models
│   │   │   ├── __init__.py          # Re-exports Base for init_db()
│   │   │   ├── user.py
│   │   │   ├── project.py           # Includes schema_config JSONB
│   │   │   ├── permission.py        # 3-tier RBAC with field-level access
│   │   │   ├── session.py           # UUID PK, active_tab tracking
│   │   │   └── audit_log.py         # 13-column audit trail + computed partition column
│   │   │
│   │   ├── queue/                   # Redis write queue
│   │   │   ├── producer.py          # enqueue_write_job() → Redis RPUSH
│   │   │   ├── worker.py            # BLPOP consumer, 1-sec throttle, audit logging
│   │   │   └── schemas.py           # WriteJobPayload Pydantic model
│   │   │
│   │   └── db/
│   │       └── engine.py            # Async engine, session factory, init_db/drop_db
│   │
│   ├── tests/                       # pytest test suites
│   │   ├── test_db.py
│   │   ├── test_core/
│   │   ├── test_sheets/
│   │   └── integration/
│   │
│   ├── requirements.txt
│   ├── Dockerfile
│   └── pyproject.toml
│
├── frontend/                        # Next.js 15 application
│   ├── src/
│   │   ├── auth.ts                  # NextAuth v5 config (Google provider, JWT+session callbacks)
│   │   ├── hooks/
│   │   │   └── useWebSocket.ts      # WebSocket client with reconnect + ping heartbeat
│   │   ├── store/
│   │   │   └── useChatStore.ts      # Zustand global state (projects, messages, WS)
│   │   └── app/
│   │       ├── layout.tsx           # Root layout with SessionProvider
│   │       ├── page.tsx             # Landing/login page
│   │       ├── globals.css          # Tailwind + custom glassmorphism styles
│   │       ├── chat/page.tsx        # Chat interface with tool call visualization
│   │       ├── admin/
│   │       │   ├── layout.tsx       # Admin sidebar layout
│   │       │   ├── page.tsx         # Admin overview dashboard
│   │       │   ├── projects/page.tsx # Project CRUD + schema config editor
│   │       │   ├── users/page.tsx   # User permission management
│   │       │   └── audit/page.tsx   # Audit log viewer
│   │       └── api/auth/[...nextauth]/route.ts
│   │
│   ├── Dockerfile                   # Multi-stage Next.js Docker build
│   └── package.json
│
├── docker-compose.yml               # PostgreSQL, Redis, backend, worker, frontend, Caddy
├── Caddyfile                        # Reverse proxy with auto-HTTPS
├── .env                             # Environment variables (secret, gitignored)
├── .gitignore
├── .gitattributes                   # LF enforcement for .sh and .py files
├── implementation.md                # Detailed implementation plan (gitignored)
├── TDD.md                           # This document (gitignored)
└── gemini.md                        # AI developer guide
```

---

## 3. Authentication & Session Management

### OAuth Flow

```text
Browser → NextAuth.js (Google OAuth, scope: openid email profile spreadsheets)
       ↓
Google authorization code → NextAuth exchanges for tokens
       ↓
NextAuth JWT callback: stores google_access_token in JWT claims
       ↓
NextAuth Session callback: signs HS256 JWT containing {email, name, sub, google_access_token}
       ↓
Frontend stores session.apiToken (signed JWT) and session.googleAccessToken
       ↓
WebSocket: sends apiToken as query parameter ?token=<JWT>
REST API: sends apiToken in Authorization: Bearer <JWT> header
       ↓
FastAPI deps.py: jose.jwt.decode(token, JWT_SECRET, HS256) → extracts email
       ↓
User lookup/auto-create in PostgreSQL → attach to request context
```

### Key Auth Files

| File | Responsibility |
|------|---------------|
| `frontend/src/auth.ts` | NextAuth v5 config: Google provider, JWT/session callbacks, HS256 signing |
| `backend/app/deps.py` | `get_current_user()`: JWT decode + user upsert; `get_google_token()`: header extraction |
| `backend/app/api/chat.py` | `authenticate_ws_user()`: WebSocket-specific JWT decode from query param |

### Security Notes

- Google OAuth tokens are embedded in the signed JWT and forwarded to FastAPI for Sheets API calls.
- A mock fallback exists (`token.startsWith("mock-")`) for local development without Google OAuth.
- The `google_access_token` is passed via JWT claims, **not** via `X-Google-Access-Token` header as originally planned in implementation.md. The WS endpoint extracts it from the decoded JWT payload.

---

## 4. Database Schema (PostgreSQL)

### Tables

| Table | Purpose | Key Fields |
|-------|---------|------------|
| `users` | Auto-registered on first login | `email` (unique), `google_sub`, `display_name`, `avatar_url`, `last_login` |
| `projects` | Registered spreadsheet trackers | `spreadsheet_id` (unique), `project_name`, `default_tab`, `company_prefix`, `schema_config` (JSONB), `is_active` |
| `permissions` | Per-user per-project RBAC | `user_id` + `project_id` (unique), `role` (admin/editor/viewer), `allowed_fields` (JSONB), `denied_operations` (JSONB) |
| `sessions` | WebSocket session tracking | `id` (UUID), `user_id`, `project_id`, `active_tab`, `last_active` |
| `audit_logs` | Immutable mutation trail | `user_email`, `tool_name`, `ricefw_id`, `field`, `old_value`, `new_value`, `args_json`, `result_ok`, `error`, `created_month` (computed) |

### ORM: SQLAlchemy 2.0 (Mapped Columns)

All models use `DeclarativeBase` with `Mapped[]` type annotations. The `init_db()` lifespan hook creates tables via `metadata.create_all()`. No Alembic migrations are configured yet.

---

## 5. LLM Orchestration & Agentic Loop

### Model Selection (`core/llm_router.py`)

| Condition | Model | Rationale |
|-----------|-------|-----------|
| Iteration 0 + conditional keywords detected | `deepseek-reasoner` | Chain-of-thought for "if X then Y" logic |
| All other cases | `deepseek-chat` (V3) | Faster, cheaper, no CoT leakage risk |

Conditional keywords: `if`, `only if`, `check first`, `depending on`, `unless`, `conditional`, `where`.

### Agentic Loop (`core/agentic_loop.py`)

- **Max iterations:** 8 (hard limit)
- **System prompt swap:** Full prompt (with column map JSON) on iteration 0; compact prompt from iteration 1+
- **DSML leakage guard:** If response contains `<｜｜DSML｜｜>`, retry with `deepseek-chat`
- **CoT stripping:** `reasoning_content` attribute logged internally, never sent to client
- **Tool dispatch flow:** RBAC check → `dispatch_tool()` → WebSocket status updates for each tool call
- **Return value:** Updated message history (system prompt stripped)

---

## 6. Tool System & RBAC Enforcement

### Tool Catalogue (9 tools in `core/tool_schemas.py`)

| Tool | Path | Description |
|------|------|-------------|
| `get_row` | READ (direct) | Fetch WRICEF object by ID |
| `search_rows` | READ (direct) | Multi-filter search with AND logic |
| `summarize` | READ (direct) | Aggregation reports (count, completion, overdue) |
| `switch_module` | READ (direct) | Change active sheet tab |
| `data_quality` | READ (direct) | Validation checks (blank, stale, consistency) |
| `update_cell` | WRITE (queued) | Update one or more field values |
| `bulk_update` | WRITE (queued) | Batch update field across multiple IDs |
| `format_row` | WRITE (queued) | Apply background color to row/cells |
| `add_row` | WRITE (queued) | Append new WRICEF object |

### RBAC Model (`core/permissions.py`)

| Tier | Capabilities | Resolution |
|------|-------------|------------|
| **Admin** | Bypass all restrictions | Matched via `ADMIN_EMAILS` env var |
| **Editor** | Write access, field-level restrictions, tool blacklists | DB `permissions` table lookup |
| **Viewer** | Read-only tools only | DB `permissions` table lookup |
| **Default** | Editor with `["*"]` access | Fallback when no DB record exists |

**Field-level enforcement:** Checks `allowed_fields` list for `update_cell` and `bulk_update` operations.

---

## 7. Embedded Sheets Layer

All Google Sheets API interactions are encapsulated in `backend/app/sheets/`:

| File | Functions | Notes |
|------|-----------|-------|
| `client.py` | `build_sheets_service()` | Builds `googleapiclient` from OAuth token |
| `read.py` | `get_row()`, `search_rows()`, `summarize()`, `run_data_quality_check()` | Schema-driven column resolution |
| `write.py` | `update_cell()`, `bulk_update()`, `add_row()` | Uses `batchUpdate` for minimal API calls |
| `format.py` | `format_row()` | `repeatCell` color formatting |
| `meta.py` | `_detect_header_row()`, `get_all_ids()`, `next_ricefw_id()`, `switch_module()` | Header scanning, ID generation |
| `retry.py` | `_with_retry()` | Exponential backoff for 429/500/503 |

### Key Design Decisions

- **`_with_retry()` is synchronous** (`time.sleep()`), used within synchronous `googleapiclient` calls. The async `await` in the calling functions wraps the synchronous Google API calls but the retry itself blocks the thread.
- **Column B is assumed** for RICEFW IDs in `get_all_ids()` (`meta.py`), but `find_row_num()` (`read.py`) properly uses `schema_config.primary_id_position`.
- **`data_start_row`** defaults to `3` (header row assumed at row 2).

---

## 8. Queue-Backed Write Path

### Architecture

```text
Tool dispatch → enqueue_write_job() → Redis RPUSH "migrationbot:write_queue"
                                                    │
                                          ┌─────────┴──────────┐
                                          │  Worker Process     │
                                          │  (BLPOP consumer)   │
                                          │  1-sec sleep per job│
                                          └─────────┬──────────┘
                                                    │
                                          Google Sheets API write
                                                    │
                                          Audit log → PostgreSQL
```

### Queue Schema (`WriteJobPayload`)

Fields: `user_email`, `google_access_token`, `session_id`, `tool_name`, `spreadsheet_id`, `sheet_tab`, `args`, `old_values`.

### Worker (`queue/worker.py`)

- Runs as a separate Docker container (`python -m app.queue.worker`)
- Processes one job at a time with 1-second delay between jobs
- Handles all 4 write tools: `update_cell`, `bulk_update`, `format_row`, `add_row`
- Logs audit records directly via `_write_audit_record()` (synchronous in-worker)

---

## 9. Column Mapping (LLM-Driven & Fallbacks)

### Static Aliases (`core/column_mapper.py`)

72 pre-defined aliases covering SAP/WRICEF domain terms. Supports typo-ridden headers (e.g., `"Functinal Resource "`).

### Resolution Pipeline (`resolve_column()`)

1. **Exact match** against canonical keys (case-insensitive)
2. **Alias list match** against generated aliases
3. **Fuzzy match** via `difflib.get_close_matches` (cutoff 0.6)

### LLM Two-Pass Mapper (`build_column_map()`)

- **Pass 1:** Generate 3-6 natural-language aliases per header
- **Pass 2:** Verify, correct ambiguities, add missing SAP terms
- **Hallucination guard:** Strips keys not in actual header row after each pass
- **Fallback:** Static `COLUMN_ALIASES` if LLM calls fail

---

## 10. Schema Auto-Detection

### Trigger

When an admin adds a new project, `detect_schema_config()` in `core/schema_detect.py` analyzes the sheet structure via a single LLM call.

### Detection Output (`schema_config` JSONB)

```json
{
  "primary_id_column": "RICEFW ID",
  "primary_id_position": "B",
  "status_column": "Dev Status",
  "module_column": "Module",
  "assignee_column": "Technical Resource ",
  "description_column": "Description",
  "type_column": "Type",
  "date_columns": {
    "go_live": "Go-Live Date",
    "signoff": "Sign-Off Date",
    "start": "Start Date",
    "completion": "Completion Date"
  },
  "critical_fields": ["RICEFW ID", "Module", "Type", "Description", "Dev Status"],
  "valid_modules": ["FI", "MM", "SD", "PM", "QM", "PP"],
  "valid_types": ["R", "I", "C", "E", "F", "W"],
  "data_start_row": 3
}
```

### Current Status

The `detect_schema_config()` function exists but is **not called** from the admin project creation flow (`admin.py` `create_project()`). Projects are created with empty `schema_config = {}`, and admins must manually provide the JSON via the frontend editor. This is a critical gap.

---

## 11. Audit Logging System

### Storage

PostgreSQL `audit_logs` table (replaced the v3.0 Config Sheet approach).

### Non-Blocking Pattern

`log_audit()` uses `asyncio.create_task()` to fire-and-forget. The background task opens its own DB session via `AsyncSessionLocal()` context manager.

### Audit Columns

`timestamp`, `user_email`, `session_id`, `tool_name`, `spreadsheet_id`, `sheet_tab`, `ricefw_id`, `field`, `old_value`, `new_value`, `args_json`, `result_ok`, `error`.

### Convenience Wrappers

`log_update_cell()`, `log_bulk_update()`, `log_format_row()`, `log_add_row()` — these are defined in `core/audit.py` but **not called** from the agentic loop or tool dispatch. The worker handles audit logging directly.

---

## 12. Data Quality & Analytics Engine

### `DataQualityChecker` (`core/data_quality.py`)

Framework-agnostic class that operates on `headers[]` and `rows[][]`, driven by `schema_config`.

| Method | Purpose |
|--------|---------|
| `blank_field_counts(fields)` | Count blank cells per column |
| `completeness_score()` | Critical field fill rate (0-100%) |
| `stale_items(audit_entries, threshold)` | Items inactive for N days |
| `consistency_checks(valid_emails)` | Logical contradictions (4 rules) |

### Consistency Rules

1. Completed items missing Sign-Off Date
2. Completed items missing Completion Date
3. Required items with blank Dev Status
4. Assigned user not in registered emails

---

## 13. Next.js Frontend

### Technology Stack

| Concern | Choice |
|---------|--------|
| Framework | Next.js 15 (App Router) |
| Styling | Tailwind CSS |
| Auth | NextAuth.js v5 |
| State | Zustand |
| WebSocket | Native WebSocket + custom hook |
| Icons | Lucide React |

### Routes

| Route | Component | Auth | Description |
|-------|-----------|------|-------------|
| `/` | Landing Page | Public | Google sign-in |
| `/chat` | Chat Panel | User | Main conversational interface |
| `/admin` | Admin Dashboard | Admin | Overview metrics |
| `/admin/projects` | Project Manager | Admin | CRUD projects + schema editor |
| `/admin/users` | User Manager | Admin | RBAC management |
| `/admin/audit` | Audit Viewer | Admin | Filterable audit log |

### Chat Page Features

- WebSocket connection with auto-reconnect (3s delay, excludes auth failures)
- Ping heartbeat every 30 seconds
- Tool call visualization with running/completed/failed status
- Module tab selector (hardcoded: SD, MM, FI, CO, PP, QM)
- Toast notifications for background queue updates
- Empty state with example prompts

---

## 14. WebSocket Protocol

```jsonc
// Client → Server
{"type": "message", "content": "Set SD-045 status to Done"}
{"type": "ping"}

// Server → Client
{"type": "connection_ok", "user_email": "...", "project_name": "...", "active_tab": "SD"}
{"type": "assistant", "content": "Updated SD-045...", "done": true}
{"type": "tool_start", "tool": "update_cell", "args": {...}}
{"type": "tool_result", "tool": "update_cell", "result": {...}}
{"type": "queue_update", "job_id": "...", "status": "completed"}
{"type": "error", "message": "Permission denied: ..."}
{"type": "pong"}
```

---

## 15. Deployment Architecture

### Docker Compose Stack (6 services)

| Service | Image | Port | Purpose |
|---------|-------|------|---------|
| `postgres` | postgres:16 | 5433:5432 | Data store |
| `redis` | redis:7-alpine | 6379 | Write queue |
| `backend` | ./backend | 8000 | FastAPI API server |
| `worker` | ./backend | — | Queue consumer |
| `frontend` | ./frontend | 3000 | Next.js app |
| `caddy` | caddy:2-alpine | 80/443 | Reverse proxy + auto-HTTPS |

### Caddy Routing

```
/api/auth/* → frontend:3000   (NextAuth endpoints)
/api/*      → backend:8000    (FastAPI REST)
/ws*        → backend:8000    (WebSocket)
*           → frontend:3000   (Catch-all)
```

---

## 16. Performance Architecture

- **Batch operations:** `bulk_update()` uses `values.batchUpdate` for O(1) API calls
- **Full range scans:** Reporting downloads up to 2000 rows in a single GET, filtering in Python
- **Write throttling:** Worker enforces 1-second delay between Sheets API writes
- **Retry with backoff:** `_with_retry()` handles 429/500/503 with exponential backoff (1s, 2s, 4s, 8s)

---

## 17. Feature Interaction Map

```text
Schema Auto-Detection (schema_detect.py)
        ├── depends on: sheets/meta.py headers + sample data
        ├── NOT CURRENTLY WIRED into admin create_project flow
        └── feeds into: projects.schema_config JSONB (when manually invoked)

Column Mapping (column_mapper.py)
        ├── static COLUMN_ALIASES as fallback
        ├── LLM 2-pass build_column_map() available but not auto-triggered
        └── resolve_column() used by: sheets/read.py, sheets/write.py, sheets/format.py

RBAC (core/permissions.py)
        ├── ADMIN_EMAILS env var → admin bypass
        ├── PostgreSQL permissions table → editor/viewer roles
        └── enforced in: agentic_loop.py per tool_call

Agentic Loop (core/agentic_loop.py)
        ├── depends on: all 9 tool schemas, LLM router, permission checker
        ├── dispatches to: tool_dispatch.py
        └── streams results via: WebSocket send_msg callback

Queue Worker (queue/worker.py)
        ├── consumes: Redis write_queue
        ├── executes: sheets/write.py, sheets/format.py, sheets/meta.py
        └── logs: core/audit.py _write_audit_record

Audit Logger (core/audit.py)
        ├── asyncio.create_task pattern for non-blocking writes
        └── convenience wrappers available but unused by dispatch
```

---

## 18. Known Bugs, Discrepancies & Technical Debt

### Critical (P0)

1. **Schema detection not wired into project creation:**
   - `admin.py:create_project()` creates projects with `schema_config = {}`.
   - `schema_detect.py:detect_schema_config()` exists but is never called.
   - Impact: Admins must manually write schema JSON. Tabs are not auto-detected.

2. **`data_quality` tool missing from RBAC sets:**
   - `permissions.py` defines `READ_ONLY_TOOLS` and `WRITE_TOOLS` but `data_quality` is absent from both.
   - `tool_dispatch.py` routes it as a read tool, but RBAC will reject it for Viewers since it's not in `READ_ONLY_TOOLS`.

3. **`resolve_column()` called without `column_map` in write.py and format.py:**
   - `write.py:update_cell()` calls `resolve_column(field)` without passing the active column map, defaulting to static aliases.
   - `format.py:format_row()` calls `resolve_column("Color")` without column map.
   - Impact: LLM-generated dynamic aliases are ignored during write operations.

4. **Hardcoded module tabs in frontend:**
   - `chat/page.tsx` hardcodes `["SD", "MM", "FI", "CO", "PP", "QM"]` as module tabs.
   - Should dynamically read from `schema_config.valid_modules` or the project's detected tabs.

5. **Hardcoded admin check in frontend:**
   - `chat/page.tsx` line 144: `isAdmin` checks if email contains "rohai", "ruhail", or "admin".
   - Should query the backend RBAC system instead.

### High (P1)

6. **`datetime.utcnow()` usage:**
   - `chat.py` line 204: `datetime.utcnow()` is deprecated in Python 3.12+.
   - Should use `datetime.now(timezone.utc)`.

7. **Blocking `time.sleep()` in retry.py:**
   - `_with_retry()` uses synchronous `time.sleep()` which blocks the event loop.
   - In an async FastAPI context, this blocks other concurrent requests during backoff periods.

8. **`get_all_ids()` hardcodes Column B:**
   - `meta.py:get_all_ids()` always reads column B for RICEFW IDs.
   - Should use `schema_config.primary_id_position`.

9. **No Alembic migrations:**
   - Schema changes require manual table drops. `init_db()` only creates tables that don't exist.

10. **`.env` file committed concerns:**
    - `.env` is in `.gitignore` but contains actual API keys and secrets.
    - No `.env.example` template exists in the repo.

### Medium (P2)

11. **`connection_ok` message not consumed by frontend:**
    - Backend sends `{"type": "connection_ok", ...}` but `useWebSocket.ts` has no handler for it.

12. **`updateLastMessage` race condition:**
    - Multiple rapid WS messages (assistant content + tool_start) can interleave, causing the updater to miss tool call associations.

13. **Frontend project fetch hits chat API route:**
    - `chat/page.tsx` fetches `/api/projects` which is defined in `chat.py`, not `admin.py`.
    - This is a REST endpoint on the chat router, violating separation of concerns.

14. **Audit convenience wrappers unused:**
    - `log_update_cell()`, `log_bulk_update()`, etc. in `core/audit.py` are never called.
    - The worker calls `_write_audit_record()` directly. The wrappers are dead code.

15. **No CORS domain restriction:**
    - `main.py` sets `allow_origins=["*"]` — acceptable for dev, must be locked down for production.

### Low (P3)

16. **LLM client base_url logic fragile:**
    - `chat.py` line 29: Heuristic to detect DeepSeek vs OpenAI based on API key content is brittle.

17. **No queue job status feedback to client:**
    - Worker processes jobs but never sends `queue_update` WS messages back.
    - The `queue_update` event handler in the frontend is wired but never triggered.

18. **`tests/` directory gitignored:**
    - `.gitignore` includes `tests/` — test code is not version controlled.

19. **No `next.config.ts` output: "standalone":**
    - Dockerfile expects standalone output but `next.config.ts` may not set it.
