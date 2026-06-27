# MigrationBot Enterprise Portal — Implementation Plan

**Version:** 1.0  
**Status:** Planning Phase  
**Date:** June 2026  
**Previous Version:** v3.0 Streamlit Prototype (see TDD.md)

---

## 1. Executive Summary

MigrationBot is migrating from a synchronous Streamlit prototype to a high-concurrency **Event-Driven, Queue-Backed Architecture** designed to support 100+ concurrent SAP team users with zero Google API starvation. The new stack is:

| Layer | Old (v3.0) | New (Enterprise Portal) |
|-------|-----------|------------------------|
| Frontend | Streamlit (Python) | **Next.js 15** (React, App Router) |
| Backend | Streamlit `app.py` (sync) | **FastAPI** (async, WebSocket, embedded MCP) |
| LLM | DeepSeek via sync `OpenAI` | **DeepSeek** via async `AsyncOpenAI` with complexity routing |
| Data Source | Google Sheets (direct API) | Google Sheets via **embedded MCP layer** in FastAPI |
| Auth | Streamlit OAuth2 component | **NextAuth.js** + Google OAuth2 (single scope) |
| RBAC/Audit | Config Google Sheet tabs | **PostgreSQL** |
| Queue | None (direct writes) | **Redis + Bull** (throttled worker) |
| Cache | `st.session_state` | **Redis** (shared across workers) |
| Schema | Hardcoded column names | **Auto-detected `schema_config`** per project via LLM |

---

## 2. Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                         NEXT.JS FRONTEND                           │
│  ┌──────────┐  ┌──────────────┐  ┌────────────────┐  ┌──────────┐ │
│  │ Login    │  │ Chat Panel   │  │ Admin Dashboard│  │ Settings │ │
│  │ (NextAuth)│  │ (WebSocket) │  │ (RBAC mgmt)   │  │          │ │
│  └──────────┘  └──────┬───────┘  └───────┬────────┘  └──────────┘ │
└────────────────────────┼─────────────────┼──────────────────────────┘
                         │ WSS             │ REST
┌────────────────────────┼─────────────────┼──────────────────────────┐
│                     FASTAPI BACKEND                                 │
│  ┌─────────────┐  ┌───┴───────────┐  ┌──┴──────────────────────┐   │
│  │ Auth Guard  │  │ Agentic Loop  │  │ Admin API               │   │
│  │ (JWT verify)│  │ (tool chain)  │  │ (RBAC, Audit, Projects) │   │
│  └──────┬──────┘  └───┬───────────┘  └──────────┬──────────────┘   │
│         │             │                         │                   │
│  ┌──────┴──────┐  ┌───┴────────────┐  ┌────────┴───────────────┐   │
│  │ Permission  │  │ LLM Router     │  │ Data Quality Engine    │   │
│  │ Checker     │  │ (V3/Flash/R1)  │  │ (schema_config-driven) │   │
│  └─────────────┘  └───┬────────────┘  └────────────────────────┘   │
│                       │                                             │
│           ┌───────────┴─────────────────┐                           │
│           │                             │                           │
│     ┌─────┴──────┐              ┌───────┴───────┐                   │
│     │ READ PATH  │              │ WRITE PATH    │                   │
│     │ (direct)   │              │ (via queue)   │                   │
│     └─────┬──────┘              └───────┬───────┘                   │
│           │                             │                           │
│     ┌─────┴──────────────┐      ┌───────┴────────┐                  │
│     │ Embedded Sheets    │      │  Redis Queue   │                  │
│     │ Layer              │      │  (Bull)        │                  │
│     └─────┬──────────────┘      └───────┬────────┘                  │
└───────────┼─────────────────────────────┼───────────────────────────┘
            │                             │
    ┌───────┴────────┐            ┌───────┴────────┐
    │ Google Sheets  │            │ Throttled      │
    │ API            │◄───────────│ Worker         │
    │                │            │ (1 req/sec)    │
    └────────────────┘            └────────────────┘
            
    ┌────────────────┐
    │  PostgreSQL    │
    │  - users       │
    │  - permissions │
    │  - audit_logs  │
    │  - projects    │  ← includes schema_config JSONB
    │  - sessions    │
    └────────────────┘
```

---

## 3. Portable Business Logic Inventory

> These are the core logic patterns from v3.0 that MUST be preserved in the new architecture. The source files are kept as reference.

### 3.1 Tool Schema & System Prompts

**Source:** `src/llm/tools.py`

9 tool definitions (OpenAI function-calling format) that form the agent's capabilities:

| Tool | Type | Description |
|------|------|-------------|
| `get_row` | READ | Fetch WRICEF object by RICEFW ID |
| `update_cell` | WRITE | Update one or more field values |
| `format_row` | WRITE | Apply background color to row/cells |
| `add_row` | WRITE | Append new WRICEF object |
| `bulk_update` | WRITE | Batch update field across multiple IDs |
| `search_rows` | READ | Multi-filter search with AND logic |
| `summarize` | READ | Aggregation reports (count, completion, overdue) |
| `switch_module` | READ | Switch active sheet tab |
| `data_quality` | READ | Validation checks (blank, stale, consistency) |

**Migration:** Tool schemas transfer 1:1 to FastAPI. Remove `DynamicModulesCall` (Streamlit-dependent). System prompts (`SYSTEM_PROMPT`, `SYSTEM_PROMPT_COMPACT`) move to a config file or constants module.

### 3.2 RBAC Logic

**Source:** `src/permissions.py`

- **3-tier model:** Admin → Editor → Viewer
- **Field-level access:** Editors can be restricted to specific columns
- **Denied operations:** Per-user tool blacklists
- **Project-scoped permissions:** `ProjectPermissionsDict` supports per-project overrides with `*` wildcard fallback
- **Group matching:** `group:prefix` keys match email prefixes for team-level rules
- **Resolution order:** Admin list → Project-specific → Wildcard → Default policy

**Migration:** The `PermissionChecker` class logic is framework-agnostic except for `st.secrets` and `st.session_state`. Port to a FastAPI dependency that reads config from environment variables and PostgreSQL instead of Google Sheets.

### 3.3 Audit Logging

**Source:** `src/audit.py`

- **Schema:** 13-column fixed layout (timestamp, user_email, session_id, tool_name, spreadsheet_id, sheet_tab, ricefw_id, field, old_value, new_value, args_json, result_ok, error)
- **Loggable tools:** `update_cell`, `bulk_update`, `format_row`, `add_row`
- **Non-blocking:** All audit write failures are caught, never surface to user
- **Old-value capture:** `update_cell` fetches current value before writing; `bulk_update` does batch pre-read
- **Convenience wrappers:** `log_update_cell()`, `log_bulk_update()`, `log_format_row()`, `log_add_row()`

**Migration:** Port to PostgreSQL `audit_logs` table. Keep the same schema columns. The non-blocking pattern becomes `asyncio.create_task()` in FastAPI.

### 3.4 Data Quality Engine

**Source:** `src/data_quality.py`

- **`DataQualityChecker`** class — framework-agnostic, operates on `headers[]` and `rows[][]`
- **Checks:** `blank_field_counts()`, `stale_items()`, `consistency_checks()`, `completeness_score()`
- **Consistency rules:**
  1. Completed items missing Sign-Off Date
  2. Completed items missing Completion Date
  3. Required items with blank Dev Status
  4. Assigned-to emails not in permissions registry

**Migration:** Transfers directly — no Streamlit dependencies. Instantiate with sheet data fetched via MCP server.

### 3.5 Sheets Executor Operations

**Source:** `src/sheets/executor.py`

Critical patterns to preserve:
- **Header row auto-detection:** `_detect_header_row()` scans first 5 rows for canonical markers
- **RICEFW ID layout caching:** `_id_row_cache` maps ID → row number
- **Prefix detection:** `detect_prefix()` handles both `MODULE-NNN` and `PREFIX-MODULE-NNN` formats
- **`next_ricefw_id()`:** Sequence generation with prefix awareness
- **Retry with backoff:** `_with_retry()` handles HTTP 429/500/503 with exponential backoff
- **Batch writes:** `bulk_update()` uses `values.batchUpdate` for O(1) API calls
- **Color formatting:** `format_row()` with `repeatCell` batchUpdate requests

**Migration:** These operations will be encapsulated within the internal Embedded Sheets Layer. This layer abstracts the raw Google Sheets API calls so the rest of the FastAPI backend can invoke them via normal async Python functions. Retry logic is maintained within this internal layer.

### 3.6 Dynamic Column Mapper

**Source:** `src/sheets/dynamic_column_mapper.py`

- **Two-pass LLM analysis:** Pass 1 generates aliases, Pass 2 verifies/corrects
- **Hallucination guard:** Strips keys not in actual header row after each pass
- **Fallback:** Static `COLUMN_ALIASES` if LLM fails

**Migration:** Core `build_column_map()` logic is reusable. Remove `st.session_state` caching — use Redis. Remove `st.spinner` — use WebSocket status messages.

### 3.7 Column Alias Dictionary

**Source:** `src/sheets/column_map.py`

- **72 static aliases** covering SAP/WRICEF domain terms (BADI, user exit, tcode, z-table, etc.)
- **`resolve_column()`**: 3-tier resolution (exact match → alias match → fuzzy match via difflib)
- **`get_column_map_json()`**: Serializes for system prompt injection

**Migration:** Transfers directly. Remove Streamlit import fallback in `resolve_column()`.

---

## 4. Implementation Phases

### Phase 0: Repository Cleanup & Scaffolding

> **Goal:** Remove obsolete Streamlit-only code, preserve portable business logic as reference, and scaffold the new project structure.

#### 4.0.1 Files to DELETE (Purely Streamlit UI / No Portable Logic)

| File | Size | Reason |
|------|------|--------|
| `app.py` | 864 lines | Streamlit entry point, OAuth gate, UI rendering, agentic loop — all framework-coupled |
| `pages/admin.py` | ~1000 lines | Streamlit admin dashboard widgets — replaced by Next.js |
| `src/admin_helpers.py` | 241 lines | Streamlit-specific data fetching with `st.session_state` caching |
| `tracker.xlsx` | 847 KB | Sample data file, not part of application code |
| `package-lock.json` | 91 bytes | Empty npm artifact from prototype |

#### 4.0.2 Files to KEEP (Portable Business Logic — Reference During Migration)

| File | Key Logic to Port |
|------|------------------|
| `src/llm/tools.py` | Tool schemas, system prompts, VALID_MODULES |
| `src/llm/deepseek_client.py` | Client factory pattern (→ AsyncOpenAI) |
| `src/permissions.py` | PermissionChecker, 3-tier RBAC, field-level access |
| `src/audit.py` | AuditLogger, schema, non-blocking pattern |
| `src/data_quality.py` | DataQualityChecker — framework-agnostic |
| `src/sheets/executor.py` | All sheet operation patterns, retry logic |
| `src/sheets/column_map.py` | COLUMN_ALIASES dict, resolve_column(), fuzzy matching |
| `src/sheets/dynamic_column_mapper.py` | Two-pass LLM mapper, hallucination guard |
| `src/sheets/sheet_registry.py` | `parse_sheet_id()` URL parser utility |
| `src/sheets/project_registry.py` | Project data model (→ PostgreSQL table schema) |
| `src/sheets_auth.py` | Credential builder pattern |
| `.streamlit/secrets.toml` | Env var structure reference |
| `requirements.txt` | Dependency reference |
| `tests/` | Test pattern reference |

#### 4.0.3 New Directory Structure

```
migrationbot/
├── _legacy/                    # Moved from src/ — reference only
│   ├── llm/
│   ├── sheets/
│   ├── permissions.py
│   ├── audit.py
│   ├── data_quality.py
│   └── sheets_auth.py
│
├── backend/                    # FastAPI application
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py             # FastAPI app entry point
│   │   ├── config.py           # Environment config (replaces secrets.toml)
│   │   ├── deps.py             # Dependency injection
│   │   │
│   │   ├── api/                # REST endpoints
│   │   │   ├── auth.py         # JWT token exchange
│   │   │   ├── chat.py         # WebSocket chat endpoint
│   │   │   ├── admin.py        # Admin CRUD (projects, users, permissions)
│   │   │   └── health.py       # Health check
│   │   │
│   │   ├── core/               # Business logic (ported from v3.0)
│   │   │   ├── agentic_loop.py # Tool-chaining loop (from app.py _handle)
│   │   │   ├── llm_router.py   # Complexity-based model routing
│   │   │   ├── tool_schemas.py # Tool definitions (from tools.py)
│   │   │   ├── tool_dispatch.py# Tool execution dispatcher
│   │   │   ├── permissions.py  # RBAC (from permissions.py)
│   │   │   ├── audit.py        # Audit logging (from audit.py)
│   │   │   ├── data_quality.py # Quality checks (schema_config-driven)
│   │   │   ├── column_mapper.py# Column alias + LLM mapper (merged)
│   │   │   └── schema_detect.py# Auto-detect schema_config for new projects
│   │   │
│   │   ├── sheets/             # Embedded Sheets layer (Google Sheets operations)
│   │   │   ├── client.py       # Sheets API client (OAuth token-based)
│   │   │   ├── read.py         # get_row, search_rows, summarize
│   │   │   ├── write.py        # update_cell, bulk_update, add_row
│   │   │   ├── format.py       # format_row (color formatting)
│   │   │   ├── meta.py         # switch_module, list_tabs, detect_headers
│   │   │   └── retry.py        # Exponential backoff (429/500/503)
│   │   │
│   │   ├── models/             # SQLAlchemy / Pydantic models
│   │   │   ├── user.py
│   │   │   ├── permission.py
│   │   │   ├── audit_log.py
│   │   │   ├── project.py      # Includes schema_config JSONB field
│   │   │   └── session.py
│   │   │
│   │   ├── queue/              # Redis queue layer
│   │   │   ├── producer.py     # Enqueue write operations
│   │   │   ├── worker.py       # Throttled consumer (1 req/sec)
│   │   │   └── schemas.py      # Job payload schemas
│   │   │
│   │   └── db/                 # Database
│   │       ├── engine.py       # SQLAlchemy async engine
│   │       └── migrations/     # Alembic migrations
│   │
│   ├── requirements.txt
│   ├── Dockerfile
│   └── pyproject.toml
│
├── frontend/                   # Next.js application
│   ├── app/
│   │   ├── layout.tsx          # Root layout with auth provider
│   │   ├── page.tsx            # Landing/login page
│   │   ├── chat/
│   │   │   └── page.tsx        # Main chat interface
│   │   ├── admin/
│   │   │   ├── page.tsx        # Admin dashboard
│   │   │   ├── users/page.tsx  # User/permission management
│   │   │   ├── projects/page.tsx # Project management (schema config editor)
│   │   │   └── audit/page.tsx  # Audit log viewer
│   │   └── api/
│   │       └── auth/[...nextauth]/route.ts
│   │
│   ├── components/
│   │   ├── chat/               # Chat UI components
│   │   ├── admin/              # Admin dashboard components
│   │   └── ui/                 # Shared UI primitives (shadcn/ui)
│   │
│   ├── lib/
│   │   ├── ws.ts               # WebSocket client manager
│   │   ├── api.ts              # REST API client
│   │   └── auth.ts             # NextAuth config
│   │
│   ├── package.json
│   ├── next.config.js
│   ├── tailwind.config.ts
│   └── Dockerfile
│
├── docker-compose.yml          # PostgreSQL, Redis, backend, frontend
├── .env.example                # Environment variables template
├── implementation.md           # This file
├── TDD.md                      # Technical Design Document (v3.0 reference)
└── gemini.md                   # Developer guide (to be updated)
```

---

### Phase 1: Database Foundation

> **Goal:** Replace Google Sheets as the config/state store with PostgreSQL.

#### 1.1 PostgreSQL Schema

```sql
-- Users table (populated on first Google OAuth login)
CREATE TABLE users (
    id            SERIAL PRIMARY KEY,
    email         VARCHAR(255) UNIQUE NOT NULL,
    display_name  VARCHAR(255),
    avatar_url    TEXT,
    google_sub    VARCHAR(255) UNIQUE,  -- Google OAuth subject ID
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    last_login    TIMESTAMPTZ
);

-- Projects table (replaces "MigrationBot Projects" sheet tab)
CREATE TABLE projects (
    id              SERIAL PRIMARY KEY,
    project_name    VARCHAR(255) NOT NULL,
    spreadsheet_id  VARCHAR(255) NOT NULL UNIQUE,
    default_tab     VARCHAR(100),
    company_prefix  VARCHAR(20),
    is_active       BOOLEAN DEFAULT TRUE,
    schema_config   JSONB DEFAULT '{}'::jsonb,  -- Auto-detected column role mapping
    created_by      INTEGER REFERENCES users(id),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Permissions table (replaces "MigrationBot Permissions" sheet tab)
CREATE TABLE permissions (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER REFERENCES users(id) ON DELETE CASCADE,
    project_id          INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    role                VARCHAR(20) NOT NULL DEFAULT 'editor'
                        CHECK (role IN ('admin', 'editor', 'viewer')),
    allowed_fields      JSONB DEFAULT '["*"]'::jsonb,
    denied_operations   JSONB DEFAULT '[]'::jsonb,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, project_id)
);

-- Audit log table (replaces "MigrationBot Audit Log" sheet tab)
CREATE TABLE audit_logs (
    id              BIGSERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ DEFAULT NOW(),
    user_email      VARCHAR(255) NOT NULL,
    session_id      UUID,
    tool_name       VARCHAR(50) NOT NULL,
    spreadsheet_id  VARCHAR(255),
    sheet_tab       VARCHAR(100),
    ricefw_id       VARCHAR(50),
    field           VARCHAR(255),
    old_value       TEXT,
    new_value       TEXT,
    args_json       JSONB,
    result_ok       BOOLEAN DEFAULT TRUE,
    error           TEXT,
    -- Partitioning-ready index
    created_month   DATE GENERATED ALWAYS AS (DATE_TRUNC('month', timestamp)) STORED
);

-- Index for common admin queries
CREATE INDEX idx_audit_timestamp ON audit_logs (timestamp DESC);
CREATE INDEX idx_audit_user ON audit_logs (user_email);
CREATE INDEX idx_audit_tool ON audit_logs (tool_name);
CREATE INDEX idx_audit_ricefw ON audit_logs (ricefw_id);

-- Sessions table (for WebSocket session tracking)
CREATE TABLE sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         INTEGER REFERENCES users(id),
    project_id      INTEGER REFERENCES projects(id),
    active_tab      VARCHAR(100),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    last_active     TIMESTAMPTZ DEFAULT NOW()
);
```

#### 1.2 Initial Seeding

No data migration required — the v3.0 prototype was not in production use. The database starts empty and is populated organically:

| Action | How |
|--------|-----|
| First admin user | Seeded via environment variable `ADMIN_EMAILS` on first boot |
| Projects | Created by admin through the dashboard UI |
| Schema config | Auto-detected by LLM on first project connection (see §2.5) |
| Permissions | Configured by admin per-project |
| Audit logs | Accumulate naturally from user operations |
| Column map cache | Built on first access per sheet+tab combo |

---

### Phase 2: FastAPI Backend

> **Goal:** Implement the async API server with agentic loop, RBAC, and queue-backed writes.

#### 2.1 Authentication Flow

```
Browser → NextAuth.js (Google OAuth) → JWT issued
       ↓
JWT in Authorization header → FastAPI middleware → verify + extract email
       ↓
Lookup user in PostgreSQL → create if first login → attach to request
```

- **Single OAuth scope:** `openid email profile https://www.googleapis.com/auth/spreadsheets` — same scope for all users regardless of role. RBAC is enforced at the application layer, not the OAuth layer
- **JWT verification:** FastAPI dependency using `python-jose` or `authlib`
- **No service accounts:** NextAuth will be configured (via `jwt` and `session` callbacks) to extract the Google `access_token` and `refresh_token` and explicitly pass them to FastAPI in custom headers (e.g., `X-Google-Access-Token`).
- **Token refresh:** NextAuth handles Google token refresh; FastAPI receives fresh tokens per request via these custom headers and forwards them to the embedded Sheets layer.

#### 2.2 Agentic Loop (Core Engine)

Port from `app.py` `_handle()` method (lines ~400-700):

```python
# Pseudocode for backend/app/core/agentic_loop.py

async def run_agentic_loop(
    user_message: str,
    session: Session,
    ws: WebSocket,
    max_iterations: int = 8,
):
    messages = session.message_history + [{"role": "user", "content": user_message}]
    
    for iteration in range(max_iterations):
        # Complexity-based routing
        model = select_model(iteration, messages)
        
        response = await llm_client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
        )
        
        choice = response.choices[0]
        
        if choice.finish_reason == "stop":
            # Final text response — stream to client via WebSocket
            await ws.send_json({"type": "assistant", "content": choice.message.content})
            break
        
        if choice.finish_reason == "tool_calls":
            for tool_call in choice.message.tool_calls:
                # RBAC check
                allowed, reason = permission_checker.can_execute(
                    tool_call.function.name,
                    json.loads(tool_call.function.arguments)
                )
                if not allowed:
                    # Send denial to chat, break loop
                    ...
                
                # Dispatch tool
                result = await dispatch_tool(tool_call, session)
                
                # Audit if write tool
                if tool_call.function.name in LOGGABLE_TOOLS:
                    await audit_logger.log(...)
                
                messages.append(tool_result_message)
```

#### 2.5 Schema Config Auto-Detection

When an admin adds a new project, the system auto-detects the sheet's structure via a single LLM call. This eliminates all hardcoded column references.

**Trigger:** Admin clicks "Add Project" → provides spreadsheet URL → backend reads headers + 3 sample rows + tab names → sends to DeepSeek → result stored in `projects.schema_config`.

```python
SCHEMA_DETECTION_PROMPT = """
You are analyzing a Google Sheet header row for a migration/project tracker.

Headers (exact, verbatim):
{headers_json}

Sample data (first 3 rows):
{sample_rows_json}

Tab names in this spreadsheet:
{tab_names_json}

Identify the semantic role of each column. Return JSON:
{{
  "primary_id_column": "<header containing the unique object ID>",
  "primary_id_position": "<column letter, e.g. B>",
  "status_column": "<header tracking dev/migration status>",
  "module_column": "<header for functional area/module/workstream>",
  "assignee_column": "<header for person assigned>",
  "description_column": "<header for object description>",
  "type_column": "<header for object type/category>",
  "date_columns": {{
    "go_live": "<header for target/go-live date, or null>",
    "signoff": "<header for sign-off/approval date, or null>",
    "start": "<header for start date, or null>",
    "completion": "<header for completion date, or null>"
  }},
  "critical_fields": ["<top 5-6 essential headers>"],
  "valid_modules": ["<from tab names or module column unique values>"],
  "valid_types": ["<unique values from the type column>"]
}}
Return ONLY valid JSON.
"""
```

**How each module consumes `schema_config`:**

| Module | v3.0 (hardcoded) | Enterprise (dynamic) |
|--------|-----------------|---------------------|
| `executor.find_row()` | Hardcoded column B | `schema_config.primary_id_position` |
| `data_quality.consistency_checks()` | Hardcoded `"Dev Status"`, `"Sign-Off Date"` | `schema_config.status_column`, `schema_config.date_columns.signoff` |
| `data_quality.completeness_score()` | Hardcoded 6 field names | `schema_config.critical_fields` |
| `tools.py` module enum | Hardcoded `["FI","MM","SD",...]` | `schema_config.valid_modules` |
| `tools.py` type enum | Hardcoded `["R","I","C","E","F","W"]` | `schema_config.valid_types` |
| `search_rows()` default fields | Hardcoded 6 columns | `schema_config.critical_fields` |
| `summarize()` overdue check | Hardcoded `"Go-Live Date"` | `schema_config.date_columns.go_live` |
| System prompt | Static column guide | Generated from `schema_config` + `column_map` |

**Admin override:** The auto-detected config is presented as an editable JSON form in the admin dashboard. Admin can correct misdetections before saving.

**Combined with column mapper:** The schema detection and alias generation happen in a single 2-pass LLM flow (Pass 1: detect roles + generate aliases, Pass 2: verify both). This costs one LLM call per project setup — not per session.

#### 2.3 Complexity-Based LLM Routing

| Condition | Model | Cost | Rationale |
|-----------|-------|------|-----------|
| Iteration 0 + conditional logic detected | `deepseek-reasoner` | ~$2/M tokens | Chain-of-thought for "if X then Y" |
| Iteration 0 (standard) | `deepseek-chat` (V3) | ~$0.27/M tokens | Best quality for first pass |
| Iterations 1-7 | `deepseek-chat` (V3) | ~$0.27/M tokens | Tool result processing |
| Column mapping (Pass 1 & 2) | `deepseek-chat` (V3) | ~$0.27/M tokens | Alias generation |
| Simple status checks | `deepseek-chat` (Flash) | Lowest | If/when available |

> **IMPORTANT:** DeepSeek Reasoner returns chain-of-thought in `reasoning_content` — this MUST be stripped before sending to the client to avoid DSML leakage. The v3.0 prototype already handles this.

#### 2.4 Queue-Backed Write Path

```
User: "Set SD-045 status to Done"
  ↓
Agentic Loop → tool_call: update_cell(SD-045, Dev Status, Done)
  ↓
RBAC check → allowed
  ↓
Read current value (old_value capture) → via embedded Sheets layer
  ↓
Enqueue write job → Redis/Bull queue
  ↓
Immediate WebSocket response: "✅ Update queued for SD-045"
  ↓
Worker picks up job (throttled: 1 request/second)
  ↓
Sheets Layer → Google Sheets API write
  ↓
Audit log entry → PostgreSQL
  ↓
WebSocket notification: "✅ SD-045 Dev Status updated to Done"
```

**Queue job schema:**
```python
class WriteJob(BaseModel):
    job_id: str          # UUID
    session_id: str      # WebSocket session
    user_email: str
    tool_name: str       # update_cell, bulk_update, etc.
    spreadsheet_id: str
    sheet_tab: str
    args: dict           # Tool arguments
    old_values: dict     # Pre-captured for audit
    priority: int        # 1=user-initiated, 2=bulk, 3=background
    created_at: datetime
```

#### 2.5 WebSocket Protocol

```jsonc
// Client → Server
{"type": "message", "content": "Set SD-045 status to Done"}
{"type": "ping"}

// Server → Client  
{"type": "assistant", "content": "Updated SD-045...", "done": true}
{"type": "tool_start", "tool": "update_cell", "args": {...}}
{"type": "tool_result", "tool": "update_cell", "result": {...}}
{"type": "queue_update", "job_id": "...", "status": "completed"}
{"type": "error", "message": "Permission denied: ..."}
{"type": "pong"}
```

---

### Phase 3: Embedded Sheets Layer (MCP in FastAPI)

> **Goal:** Encapsulate all Google Sheets API interactions in a clean internal module within the FastAPI backend. No standalone MCP server — fewer moving parts, simpler deployment.

#### 3.1 Internal Module Structure (`backend/app/sheets/`)

| File | v3.0 Executor Method | Path | Notes |
|------|---------------------|------|-------|
| `read.py` | `get_row()`, `search_rows()`, `summarize()` | Direct read | Uses `schema_config` for column resolution |
| `write.py` | `update_cell()`, `bulk_update()`, `add_row()` | Queue | Enqueued via Redis, executed by worker |
| `format.py` | `format_row()` | Queue | Color formatting via batchUpdate |
| `meta.py` | `switch_module()`, `list_tabs()`, `detect_headers()` | Direct | Tab discovery + header detection |
| `client.py` | `build_sheets_service()` | — | OAuth token → `googleapiclient` service builder |
| `retry.py` | `_with_retry()` | — | Exponential backoff for 429/500/503 |

All methods accept `schema_config` as a parameter instead of hardcoding column names. Example:

```python
# read.py
async def find_row(service, spreadsheet_id: str, sheet_tab: str,
                   ricefw_id: str, schema: SchemaConfig) -> int | None:
    """Find row number for a RICEFW ID using schema_config.primary_id_position."""
    id_col = schema.primary_id_position  # e.g. "B" — not hardcoded
    result = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_tab}!{id_col}{schema.data_start_row}:{id_col}"
    ).execute()
    ...
```

#### 3.2 Sheets Layer Responsibilities

- **OAuth token forwarding:** Receives user's access token per request, builds `Credentials` object
- **Header caching:** Cache header rows in Redis (TTL: 5 min) to avoid repeated API calls
- **ID-to-row mapping:** Cache RICEFW ID → row number in Redis (per spreadsheet+tab)
- **Retry logic:** `_with_retry()` pattern from executor.py, handles 429/500/503
- **Rate limiting:** Internal throttle to stay under Google Sheets API quota (60 req/min/user)
- **Schema-driven:** All operations resolve columns via `schema_config` — zero hardcoded column names

---

### Phase 4: Next.js Frontend

> **Goal:** Build the user-facing portal with real-time chat, admin dashboard, and modern UI.

#### 4.1 Technology Choices

| Concern | Choice | Rationale |
|---------|--------|-----------|
| Framework | Next.js 15 (App Router) | SSR + API routes + middleware |
| UI Library | shadcn/ui + Tailwind CSS | Consistent, accessible components |
| Auth | NextAuth.js v5 | Google OAuth with JWT sessions |
| State | Zustand | Lightweight, no Redux boilerplate |
| WebSocket | Native WebSocket + reconnection lib | Real-time chat |
| Charts | Recharts or Nivo | Admin dashboard visualizations |

#### 4.2 Pages & Routes

| Route | Component | Auth | Description |
|-------|-----------|------|-------------|
| `/` | Landing | Public | Login with Google |
| `/chat` | Chat Panel | User | Main conversational interface |
| `/chat?project=X` | Chat Panel | User | Pre-selected project |
| `/admin` | Dashboard | Admin | Overview metrics |
| `/admin/projects` | Project Manager | Admin | CRUD projects |
| `/admin/users` | User Manager | Admin | RBAC management |
| `/admin/audit` | Audit Viewer | Admin | Filterable audit log |

#### 4.3 Chat Panel Features

- **WebSocket connection** with auto-reconnect
- **Streaming responses** — render tokens as they arrive
- **Tool call visualization** — show tool name + args while executing
- **Queue status** — toast notifications when writes complete
- **Project/tab selector** — dropdown in header, triggers `switch_module`
- **Message history** — persisted in session, scrollable

---

### Phase 5: Integration & Deployment

#### 5.1 Docker Compose Stack

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: migrationbot
      POSTGRES_USER: migrationbot
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data
    ports:
      - "5432:5432"

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"

  backend:
    build: ./backend
    environment:
      DATABASE_URL: postgresql+asyncpg://migrationbot:${DB_PASSWORD}@postgres:5432/migrationbot
      REDIS_URL: redis://redis:6379
      DEEPSEEK_API_KEY: ${DEEPSEEK_API_KEY}
      GOOGLE_CLIENT_ID: ${GOOGLE_CLIENT_ID}
      GOOGLE_CLIENT_SECRET: ${GOOGLE_CLIENT_SECRET}
      JWT_SECRET: ${JWT_SECRET}
    ports:
      - "8000:8000"
    depends_on:
      - postgres
      - redis

  worker:
    build: ./backend
    command: python -m app.queue.worker
    environment:
      DATABASE_URL: postgresql+asyncpg://migrationbot:${DB_PASSWORD}@postgres:5432/migrationbot
      REDIS_URL: redis://redis:6379
    depends_on:
      - postgres
      - redis


  frontend:
    build: ./frontend
    environment:
      NEXT_PUBLIC_API_URL: http://backend:8000
      NEXT_PUBLIC_WS_URL: ws://backend:8000/ws
      NEXTAUTH_URL: ${NEXTAUTH_URL}
      NEXTAUTH_SECRET: ${NEXTAUTH_SECRET}
      GOOGLE_CLIENT_ID: ${GOOGLE_CLIENT_ID}
      GOOGLE_CLIENT_SECRET: ${GOOGLE_CLIENT_SECRET}
    ports:
      - "3000:3000"
    depends_on:
      - backend

volumes:
  pgdata:
```

#### 5.2 Environment Variables (`.env.example`)

```env
# Database
DB_PASSWORD=your_db_password

# DeepSeek
DEEPSEEK_API_KEY=sk-xxxx

# Google OAuth
GOOGLE_CLIENT_ID=xxxx.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxxx

# Auth
JWT_SECRET=your_jwt_secret
NEXTAUTH_URL=http://localhost:3000
NEXTAUTH_SECRET=your_nextauth_secret

# Defaults (from v3.0 secrets.toml)
DEFAULT_SPREADSHEET_ID=17mrUyJbhOhBbaQYzQ4iPFH6kPPHBjqOR3dt2EWGCDUA
DEFAULT_SHEET_TAB=SD
DEFAULT_SHEET_LABEL=FF Migration Tracker
```

---

## 5. Phase-by-Phase Testing Strategy

To ensure stability and prevent regressions as we migrate to the Event-Driven Architecture, testing will be executed strictly per-phase before moving to the next.

### Phase 1: Database Foundation Tests
*Location: `backend/tests/test_db.py`*

**Automated Tests:**
- `test_db_connection`: Verifies async SQLAlchemy engine connects to PostgreSQL successfully.
- `test_project_schema_config_default`: Asserts that creating a project without schema config defaults to `'{}'::jsonb`.
- `test_user_creation`: Verifies NextAuth/Google OAuth subject ID correctly inserts or finds users.
- `test_rbac_cascading_deletes`: Asserts that deleting a project or user successfully cascades to wipe `permissions` table rows.

**Manual Gate:** Run Alembic migrations up to head and down to base to ensure no schema lockups.

### Phase 2: FastAPI Backend & Core Engine Tests
*Location: `backend/tests/test_core/`*

**Automated Tests:**
- `test_jwt_verification`: Pass a mock NextAuth JWT and ensure FastAPI decodes and extracts user email/roles successfully.
- `test_agentic_loop_max_iterations`: Mock DeepSeek to endlessly return tool calls; assert the loop forcefully breaks at 8 iterations.
- `test_llm_routing`: Mock a message with "if / else" and verify it routes to `deepseek-reasoner`. Mock a standard query and verify `deepseek-chat`.
- `test_rbac_interception`: Mock an `update_cell` tool call for a "Viewer" role user; assert the permission checker returns `False` and blocks execution.
- `test_audit_logger_nonblocking`: Force the DB to throw an error during audit write; assert the main tool execution still returns successfully.

**Manual Gate:** Use Postman/Swagger UI to hit the WebSocket `/ws/chat` endpoint and verify echo responses.

### Phase 3: Embedded Sheets Layer Tests
*Location: `backend/tests/test_sheets/`*

**Automated Tests:**
- `test_read_find_row`: Mock Google API response; assert it correctly maps RICEFW ID using the `schema_config` column index.
- `test_write_job_enqueuing`: Trigger a write function; assert it places a valid `WriteJob` payload into the Redis Bull queue.
- `test_worker_throttling`: Spin up the worker, enqueue 5 jobs; assert they are processed with exactly 1-second delays (rate limiting).
- `test_api_retry_backoff`: Mock the Google API returning a 429 error; assert the `_with_retry` wrapper catches it, waits, and retries.
- `test_column_mapper`: Pass a mock header list and target alias; assert the fuzzy matcher resolves the correct canonical name.

**Manual Gate:** Create a test Google Sheet, run the worker locally, and verify an enqueue operation successfully modifies a cell in the live sheet.

### Phase 4: Next.js Frontend Tests
*Location: `frontend/__tests__/`*

**Automated Tests (Vitest + React Testing Library):**
- `test_chat_rendering`: Assert that streaming tokens from the WebSocket append sequentially to the chat bubble.
- `test_tool_call_ui`: Assert that when a `tool_start` message is received, the UI renders the animated loading spinner and tool name.
- `test_auth_guard`: Assert that navigating to `/chat` without a NextAuth session redirects to `/`.
- `test_admin_schema_editor`: Assert that the schema config JSON editor prevents saving invalid JSON syntax.

**Manual Gate:** Run `npm run dev`, login with Google, navigate to the chat, and ensure the UI connects to the local FastAPI WebSocket.

### Phase 5: Integration & Load Testing
*Location: `tests/integration/`*

**Automated Tests:**
- `test_e2e_write_flow`: Spin up `docker-compose`. Hit FastAPI directly to queue an update. Assert the Postgres audit log updates and Redis queue empties.
- `test_e2e_read_flow`: Hit the chat API; assert the entire loop completes and returns a text summary.

**Load Testing (Locust / Artillery):**
- Spawn 50 simulated WebSocket connections sending messages every 5 seconds.
- Assert 0 Google Sheets API 429 errors (proving the worker throttle works).
- Assert Redis memory stays within boundaries.

---

## 6. Risk Mitigation

| Risk | Impact | Mitigation |
|------|--------|------------|
| Google Sheets API quota exhaustion | Users see errors | Queue-backed writes + internal rate limiting in Sheets layer |
| DeepSeek API latency spikes | Slow chat responses | Streaming + fallback to `deepseek-chat` if Reasoner times out |
| Schema config misdetection | Wrong columns targeted | Admin review step before saving + editable JSON form in dashboard |
| OAuth token expiry mid-session | API calls fail | NextAuth auto-refresh + FastAPI token validation middleware |
| New sheet with unusual structure | Schema detection fails | Fallback to manual config entry; LLM prompt handles edge cases |

---

## 7. Implementation Priority

| Priority | Phase | Deliverable | Estimated Effort |
|----------|-------|-------------|------------------|
| **P0** | Phase 0 | Repo cleanup + scaffold | 1 day |
| **P0** | Phase 1 | PostgreSQL schema + migrations | 2 days |
| **P1** | Phase 2 | FastAPI backend (auth + agentic loop + schema detection) | 5 days |
| **P1** | Phase 3 | Embedded Sheets layer (read + write + queue worker) | 3 days |
| **P2** | Phase 4 | Next.js frontend (chat + admin + schema config editor) | 5 days |
| **P2** | Phase 5 | Docker compose + integration testing | 2 days |
| **P3** | — | Load testing + optimization | 2 days |

**Total estimated: ~20 working days (4 weeks)**

---

## 8. Resolved Decisions

| Question | Decision | Rationale |
|----------|----------|-----------|
| MCP Server | **Embedded in FastAPI** | Fewer moving parts, simpler deployment, no inter-process communication overhead |
| Data Migration | **Not needed** | Project was not in production use; database starts clean |
| OAuth Scopes | **Single scope for all users** | `openid email profile spreadsheets` — RBAC enforced at app layer, not OAuth layer |
| Webhooks/Async LLM | **Not needed** | DeepSeek is already ~$0.27/M tokens; 50% savings doesn't justify complexity |
| Hardcoded columns | **Eliminated via `schema_config`** | LLM auto-detects column roles on project setup; admin can override |
| Deployment target | **Hetzner VPS (CX22)** | ~$4.10/mo, full WebSocket support, swap file configured for low memory |

---

## 9. Deployment Configuration (Hetzner VPS)

### 9.1 Server Specification

| Resource | Spec | Notes |
|----------|------|-------|
| Plan | Hetzner **CX22** | 2 vCPU (shared), 4 GB RAM, 40 GB SSD |
| OS | Ubuntu 24.04 LTS | Docker pre-installed via cloud-init |
| Location | Nuremberg or Helsinki | Lowest latency to EU; acceptable for global SAP teams |
| Cost | **~€3.79/mo (~$4.10)** | + DeepSeek API ~$5-15/mo |
| Backups | Hetzner automated backups | +20% cost (~€0.75/mo) |

*Note: Since the CX22 has 4 GB RAM, we will configure a **2 GB swap file** on the Ubuntu VPS during provisioning to prevent PostgreSQL/Redis containers from being terminated under memory spikes.*

### 9.2 Reverse Proxy (Caddy)

Caddy provides automatic HTTPS (Let's Encrypt) and WebSocket proxying with zero config:

```Caddyfile
# Caddyfile
migrationbot.yourdomain.com {
    # Frontend (Next.js)
    handle /* {
        reverse_proxy frontend:3000
    }

    # Backend API (REST)
    handle /api/* {
        reverse_proxy backend:8000
    }

    # WebSocket endpoint (chat)
    handle /ws/* {
        reverse_proxy backend:8000
    }
}
```

Add Caddy to Docker Compose:

```yaml
  caddy:
    image: caddy:2-alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      - frontend
      - backend
    restart: unless-stopped
```

### 9.3 Deployment Workflow (from Windows local machine)

```powershell
# 1. SSH into VPS via PowerShell
ssh root@<vps-ip>

# 2. Inside the VPS: Pull and restart containers
cd /app && docker compose pull && docker compose up -d
```

#### Windows Development Considerations:
* **Line Endings (CRLF vs LF):** Ensure git does not check out entrypoint scripts with Windows carriage returns. Add a `.gitattributes` file:
  ```gitattributes
  *.sh text eol=lf
  docker-entrypoint.sh text eol=lf
  ```
* **Git Configuration:** Set CRLF behavior in PowerShell before pushing code:
  ```powershell
  git config --global core.autocrlf true
  ```
* **Local Docker:** Use **Docker Desktop for Windows** backed by WSL2 for local verification.

Future improvement: GitHub Actions CI/CD that auto-deploys on `main` push via SSH.
