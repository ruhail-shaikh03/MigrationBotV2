# MigrationBot Enterprise Portal — Technical Design Document

**Version:** 2.1 (Comprehensive Audit)  
**Team:** FF Team  
**Status:** Active Development (Phase 3 — Deployed with Critical Gaps)  
**Stack:** Next.js 16 · FastAPI · PostgreSQL 16 · Redis 7 · DeepSeek · Google Sheets API v4 · Google OAuth 2.0  
**Deployment Target:** Hetzner CX22 VPS via Docker Compose + Caddy 2 reverse proxy  
**Domain:** `migrationbot.duckdns.org`

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Repository Structure — Complete File Map](#2-repository-structure--complete-file-map)
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
13. [Next.js Frontend — Complete Component Audit](#13-nextjs-frontend--complete-component-audit)
14. [WebSocket Protocol](#14-websocket-protocol)
15. [Deployment Architecture](#15-deployment-architecture)
16. [CI/CD Pipeline](#16-cicd-pipeline)
17. [Performance Architecture](#17-performance-architecture)
18. [Feature Interaction Map](#18-feature-interaction-map)
19. [Known Bugs, Discrepancies & Technical Debt](#19-known-bugs-discrepancies--technical-debt)

---

## 1. System Overview

MigrationBot Enterprise Portal is the second-generation implementation of the MigrationBot conversational AI assistant, migrated from a synchronous Streamlit prototype (v3.0) to a high-concurrency event-driven architecture. The system allows SAP team members to interact with S/4HANA WRICEF Migration Tracker Google Sheets via natural language.

### Architecture Summary

| Layer | Technology | Responsibility |
|-------|-----------|----------------|
| Frontend | Next.js 16 (App Router) + Tailwind CSS v4 | Auth, Chat UI, Admin Dashboard |
| Backend | FastAPI (async) | WebSocket chat, REST admin API, Agentic loop |
| LLM | DeepSeek (`deepseek-chat`, `deepseek-reasoner`) via `AsyncOpenAI` | Intent parsing, tool selection, response composition |
| Data Source | Google Sheets API v4 | WRICEF tracker data (reads and writes) |
| Auth | NextAuth.js v5 (Google OAuth) → HS256 JWT → FastAPI | Single-scope OAuth, JWT-based API auth |
| RBAC/Audit | PostgreSQL 16 | Users, permissions, projects, sessions, audit logs |
| Write Queue | Redis 7 (RPUSH/BLPOP FIFO) | Throttled mutation pipeline (1 req/sec) |
| Reverse Proxy | Caddy 2 (Alpine) | Auto-HTTPS (Let's Encrypt), WebSocket proxying, route splitting |

### Request Lifecycle

```text
User types message in Next.js Chat UI
        │
        ▼
WebSocket sends JSON: {"type": "message", "content": "..."}
        │
        ▼
Caddy /ws* → reverse_proxy → backend:8000
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

## 2. Repository Structure — Complete File Map

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
│   │   ├── main.py                  # FastAPI app entry, CORS, lifespan (init_db), router mounts
│   │   ├── config.py                # Pydantic Settings: DATABASE_URL, REDIS_URL, DEEPSEEK_API_KEY,
│   │   │                            #   GOOGLE_CLIENT_ID/SECRET, JWT_SECRET, DEFAULT_* params,
│   │   │                            #   ADMIN_EMAILS, CORS_ORIGINS — reads from .env
│   │   ├── deps.py                  # get_current_user(): JWT decode + user auto-upsert
│   │   │                            # get_google_token(): X-Google-Access-Token header extraction
│   │   │
│   │   ├── api/                     # HTTP/WS endpoints
│   │   │   ├── auth.py              # GET /api/auth/me — returns {email, is_admin, display_name}
│   │   │   ├── chat.py              # WS /ws — agentic chat endpoint
│   │   │   │                        # GET /api/projects — user's accessible projects list
│   │   │   │                        # Contains: authenticate_ws_user(), connection_ok msg,
│   │   │   │                        #   session creation, agentic_loop invocation
│   │   │   ├── admin.py             # POST/GET/PUT/DELETE /api/admin/projects
│   │   │   │                        # POST/GET /api/admin/permissions
│   │   │   │                        # GET /api/admin/audits
│   │   │   │                        # POST /api/admin/projects/detect-metadata (auto-detect)
│   │   │   └── health.py            # GET /api/health — returns {"status": "ok"}
│   │   │
│   │   ├── core/                    # Business logic
│   │   │   ├── agentic_loop.py      # run_agentic_loop(): max 8 iterations, DSML guard,
│   │   │   │                        #   CoT stripping, system prompt swap (full → compact),
│   │   │   │                        #   WS streaming (tool_start/tool_result/assistant msgs)
│   │   │   ├── llm_router.py        # select_model(): conditional keywords → deepseek-reasoner,
│   │   │   │                        #   else deepseek-chat; has_conditional_logic() helper
│   │   │   ├── tool_schemas.py      # 9 tool definitions (OpenAI function-calling format) +
│   │   │   │                        #   SYSTEM_PROMPT + SYSTEM_PROMPT_COMPACT templates
│   │   │   ├── tool_dispatch.py     # dispatch_tool(): routes reads (direct) vs writes (queue),
│   │   │   │                        #   pre-reads old_values for audit, enqueue_write_job()
│   │   │   ├── permissions.py       # PermissionChecker: 3-tier RBAC (admin/editor/viewer),
│   │   │   │                        #   field-level enforcement, denied_operations,
│   │   │   │                        #   READ_ONLY_TOOLS & WRITE_TOOLS sets,
│   │   │   │                        #   resolve_user_permissions() DB lookup
│   │   │   ├── audit.py             # log_audit(): asyncio.create_task non-blocking pattern,
│   │   │   │                        #   _write_audit_record(): direct sync insert,
│   │   │   │                        #   convenience wrappers: log_update_cell, log_bulk_update,
│   │   │   │                        #     log_format_row, log_add_row (UNUSED — dead code)
│   │   │   ├── data_quality.py      # DataQualityChecker: blank_field_counts(),
│   │   │   │                        #   completeness_score(), stale_items(),
│   │   │   │                        #   consistency_checks() (4 rules, schema_config-driven)
│   │   │   ├── column_mapper.py     # COLUMN_ALIASES (72 entries), resolve_column() 3-tier
│   │   │   │                        #   (exact → alias → fuzzy), build_column_map() 2-pass LLM,
│   │   │   │                        #   get_column_map_json() for system prompt injection
│   │   │   └── schema_detect.py     # detect_schema_config(): single LLM call per tab,
│   │   │                            #   detect_all_tabs(): iterates all tabs, returns
│   │   │                            #   {tabs: {tabName: config}, global: {...}}
│   │   │
│   │   ├── sheets/                  # Embedded Google Sheets layer
│   │   │   ├── client.py            # build_sheets_service(): OAuth Credentials → googleapiclient
│   │   │   │                        #   Uses settings.GOOGLE_CLIENT_ID/SECRET for token refresh
│   │   │   ├── read.py              # get_row(), search_rows() (AND filters, 3 match types),
│   │   │   │                        #   summarize() (count_by_field, completion_rate,
│   │   │   │                        #     blank_fields, overdue), run_data_quality_check(),
│   │   │   │                        #   get_row_raw(), get_bulk_rows_raw(), find_row_num(),
│   │   │   │                        #   idx_to_col_letter(), _get_tab_schema()
│   │   │   ├── write.py             # update_cell() (batchUpdate), bulk_update() (with filter
│   │   │   │                        #   fallback + individual retry), add_row() (append)
│   │   │   ├── format.py            # format_row(): repeatCell color formatting,
│   │   │   │                        #   COLOR_MAP: red/green/amber/blue/white
│   │   │   ├── meta.py              # _detect_header_row() (canonical markers scan),
│   │   │   │                        #   get_sheet_id(), get_header_row(), get_all_ids(),
│   │   │   │                        #   detect_prefix(), next_ricefw_id(), switch_module()
│   │   │   └── retry.py             # _with_retry(): exponential backoff for 429/500/503,
│   │   │                            #   max 4 attempts, base delay 1.0s
│   │   │
│   │   ├── models/                  # SQLAlchemy 2.0 ORM models (Mapped[] annotations)
│   │   │   ├── __init__.py          # Re-exports Base for init_db()
│   │   │   ├── user.py              # User: email(unique), google_sub, display_name,
│   │   │   │                        #   avatar_url, last_login, created_at
│   │   │   ├── project.py           # Project: spreadsheet_id(unique), project_name,
│   │   │   │                        #   default_tab, company_prefix, is_active,
│   │   │   │                        #   schema_config(JSON), created_at
│   │   │   ├── permission.py        # Permission: user_id+project_id(unique), role(enum),
│   │   │   │                        #   allowed_fields(JSON), denied_operations(JSON)
│   │   │   ├── session.py           # Session: id(UUID), user_id, project_id, active_tab,
│   │   │   │                        #   last_active, created_at
│   │   │   └── audit_log.py         # AuditLog: 13 columns + created_month(computed)
│   │   │
│   │   ├── queue/                   # Redis write queue
│   │   │   ├── producer.py          # enqueue_write_job() → Redis RPUSH "migrationbot:write_queue",
│   │   │   │                        #   EnqueuedJob class, uuid4 job IDs
│   │   │   ├── worker.py            # process_job(): handles update_cell/bulk_update/format_row/add_row
│   │   │   │                        #   start_worker(): BLPOP consumer, 1-sec throttle,
│   │   │   │                        #   audit logging per mutation, __main__ entry point
│   │   │   └── schemas.py           # WriteJobPayload: Pydantic model with user_email,
│   │   │                            #   google_access_token, session_id, tool_name,
│   │   │                            #   spreadsheet_id, sheet_tab, args, old_values
│   │   │
│   │   └── db/
│   │       └── engine.py            # create_async_engine (asyncpg), AsyncSessionLocal,
│   │                                #   Base(DeclarativeBase), get_db() yield dependency,
│   │                                #   init_db() / drop_db() helpers
│   │
│   ├── tests/                       # pytest test suites
│   │   ├── test_db.py
│   │   ├── test_core/
│   │   ├── test_sheets/
│   │   └── integration/
│   │
│   ├── requirements.txt             # 19 dependencies: fastapi, uvicorn, sqlalchemy, asyncpg,
│   │                                #   pydantic, python-jose, redis, structlog, google-*,
│   │                                #   openai, pandas, pytest, httpx, greenlet
│   ├── Dockerfile                   # python:3.12-slim, uvicorn CMD
│   └── pyproject.toml
│
├── frontend/                        # Next.js 16 application
│   ├── src/
│   │   ├── auth.ts                  # NextAuth v5 config: Google provider (spreadsheets scope),
│   │   │                            #   JWT callback (stores googleAccessToken), session callback
│   │   │                            #   (signs HS256 JWT with email/name/sub/google_access_token),
│   │   │                            #   trustHost: true, strategy: "jwt"
│   │   ├── hooks/
│   │   │   └── useWebSocket.ts      # useWebSocket(apiToken, projectId): auto-connect,
│   │   │                            #   reconnect on close (3s, skip code 1000/1008),
│   │   │                            #   30s ping heartbeat, message dispatch:
│   │   │                            #     assistant → append content to last msg
│   │   │                            #     tool_start → add toolCall with running status
│   │   │                            #     tool_result → mark toolCall as completed
│   │   │                            #     queue_update → dispatch CustomEvent
│   │   │                            #     error → add system message
│   │   │                            #     connection_ok → set activeTab
│   │   ├── store/
│   │   │   └── useChatStore.ts      # Zustand store: Project/Message interfaces,
│   │   │                            #   projects[], activeProject, activeTab("SD" default),
│   │   │                            #   isConnected, messages[], ws ref,
│   │   │                            #   setProjects, setActiveProject (resets activeTab),
│   │   │                            #   addMessage, updateLastMessage, clearChat
│   │   └── app/
│   │       ├── layout.tsx           # Root layout: SessionProvider, Geist + Geist_Mono fonts,
│   │       │                        #   dark mode, bg-[#030014], metadata title/description
│   │       ├── page.tsx             # Landing page: Google sign-in button, 3 feature cards
│   │       │                        #   (AI-Agentic Actions, Queue-Throttled Writes, RBAC),
│   │       │                        #   animated blobs, grid overlay, auto-redirect if authed
│   │       ├── globals.css          # Tailwind v4 @import, custom CSS variables, glass-panel,
│   │       │                        #   glass-card, chat-bubble-user/agent, custom scrollbar,
│   │       │                        #   animate-pulse-slow, animate-slide-up, animate-blob
│   │       ├── chat/
│   │       │   └── page.tsx         # Chat interface: WebSocket messages, project dropdown,
│   │       │                        #   module tab selector (from schema_config.tabs keys,
│   │       │                        #   fallback to hardcoded [SD,MM,FI,CO,PP,QM]),
│   │       │                        #   tool call visualization, toast notifications,
│   │       │                        #   streaming dots, HARDCODED admin email check
│   │       ├── admin/
│   │       │   ├── layout.tsx       # Admin sidebar: Overview, Projects Manager, User
│   │       │   │                    #   Permissions, Audit Viewer nav links,
│   │       │   │                    #   HARDCODED admin email guard (rohai/ruhail/admin)
│   │       │   ├── page.tsx         # Overview dashboard: 4 metric cards (projects, users,
│   │       │   │                    #   audits, errors), operations area chart (recharts),
│   │       │   │                    #   tool distribution bar chart
│   │       │   ├── projects/
│   │       │   │   └── page.tsx     # Project CRUD: table list, create/edit modal,
│   │       │   │                    #   Auto-Detect Wizard (URL → analyze → tab checkboxes),
│   │       │   │                    #   Manual Mode (JSON schema editor), delete with confirm
│   │       │   ├── users/
│   │       │   │   └── page.tsx     # User permission management: assign projects, set roles,
│   │       │   │                    #   field-level access, denied operations
│   │       │   └── audit/
│   │       │       └── page.tsx     # Audit log viewer: filterable, paginated, sortable
│   │       └── api/auth/[...nextauth]/
│   │           └── route.ts         # NextAuth catch-all API route handler
│   │
│   ├── Dockerfile                   # Multi-stage: deps → builder → runner (node:20-alpine),
│   │                                #   standalone output, non-root user
│   ├── package.json                 # Dependencies: next@16.2.9, react@19.2.4, next-auth@5,
│   │                                #   zustand@5, recharts@2, lucide-react, framer-motion,
│   │                                #   jose, clsx, tailwind-merge
│   └── next.config.ts               # output: "standalone" (for Docker deployment)
│
├── docker-compose.yml               # 6 services: postgres:16, redis:7-alpine, backend,
│                                    #   worker, frontend, caddy:2-alpine
├── Caddyfile                        # migrationbot.duckdns.org: route-based splitting
│                                    #   /api/auth/* → frontend, /api/* → backend,
│                                    #   /ws* → backend, * → frontend
├── .github/workflows/
│   ├── ci.yml                       # On push to main/develop/phase3: pytest + Next.js build
│   └── deploy.yml                   # On push to main/phase3: SSH to VPS, git pull,
│                                    #   docker compose down && up --build
├── .env.example                     # Template for all env vars (23 lines)
├── .env                             # Actual secrets (gitignored)
├── .gitignore
├── .gitattributes                   # LF enforcement for .sh and .py files
├── implementation.md                # Detailed implementation plan
├── TDD.md                           # This document
└── gemini.md                        # AI developer guide
```

### File Byte Sizes (Significant Files)

| File | Size | Purpose |
|------|------|---------|
| `backend/app/core/agentic_loop.py` | ~5.3 KB | Central agent execution engine |
| `backend/app/core/tool_schemas.py` | ~5.0 KB | 9 tool definitions + system prompts |
| `backend/app/core/column_mapper.py` | ~5.3 KB | 72 aliases + LLM mapper + resolve_column |
| `backend/app/sheets/read.py` | ~16.2 KB | All read operations + summarize + data quality |
| `backend/app/sheets/write.py` | ~8.6 KB | All write operations (cell, bulk, add) |
| `backend/app/queue/worker.py` | ~11.0 KB | Queue consumer with all 4 write handlers |
| `frontend/src/app/admin/projects/page.tsx` | ~28.2 KB | Full project CRUD + auto-detect wizard |
| `frontend/src/app/admin/users/page.tsx` | ~15.6 KB | RBAC permission management UI |
| `frontend/src/app/chat/page.tsx` | ~16.9 KB | Chat interface + tool visualization |
| `frontend/src/hooks/useWebSocket.ts` | ~5.6 KB | WebSocket client with reconnect logic |

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
| `frontend/src/auth.ts` | NextAuth v5 config: Google provider (spreadsheets scope), JWT callback stores `googleAccessToken`, session callback signs HS256 JWT via `jose.SignJWT` |
| `backend/app/deps.py` | `get_current_user()`: JWT decode + user auto-upsert; `get_google_token()`: X-Google-Access-Token header extraction |
| `backend/app/api/chat.py` | `authenticate_ws_user()`: WebSocket JWT decode from `?token=` query param; extracts `google_access_token` from JWT payload |

### Security Architecture

- Google OAuth tokens are embedded in the signed JWT and forwarded to FastAPI for Sheets API calls.
- The `google_access_token` is embedded in the JWT payload (signed at NextAuth session callback) — NOT via a separate `X-Google-Access-Token` header.
- The WS endpoint extracts `google_access_token` directly from the decoded JWT payload.
- A mock fallback exists (`token.startsWith("mock-")`) for local development without Google OAuth.
- Token expiry: JWT is signed with `exp` = 24 hours from creation time.

### Admin Detection

- **Backend:** `config.py` → `ADMIN_EMAILS` env var → `admin_emails_list` property. `deps.py` checks email against this list.
- **Frontend (BUG):** `chat/page.tsx` line 161 and `admin/layout.tsx` line 28 use a **hardcoded** substring check (`["rohai", "ruhail", "admin"].some(key => email.includes(key))`) as admin detection. This duplicates and deviates from the backend's `ADMIN_EMAILS` env var approach.

---

## 4. Database Schema (PostgreSQL)

### Tables

| Table | Purpose | Key Fields |
|-------|---------|------------|
| `users` | Auto-registered on first login | `email` (unique), `google_sub`, `display_name`, `avatar_url`, `last_login` |
| `projects` | Registered spreadsheet trackers | `spreadsheet_id` (unique), `project_name`, `default_tab`, `company_prefix`, `schema_config` (JSON), `is_active` |
| `permissions` | Per-user per-project RBAC | `user_id` + `project_id` (unique), `role` (admin/editor/viewer), `allowed_fields` (JSON), `denied_operations` (JSON) |
| `sessions` | WebSocket session tracking | `id` (UUID), `user_id`, `project_id`, `active_tab`, `last_active` |
| `audit_logs` | Immutable mutation trail | `user_email`, `tool_name`, `ricefw_id`, `field`, `old_value`, `new_value`, `args_json`, `result_ok`, `error`, `created_month` (computed) |

### ORM Details (SQLAlchemy 2.0)

All models use `DeclarativeBase` with `Mapped[]` type annotations (SQLAlchemy 2.0 style):

```python
# Example: Project model (project.py)
class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_name: Mapped[str] = mapped_column(String(255), nullable=False)
    spreadsheet_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    default_tab: Mapped[Optional[str]] = mapped_column(String(100))
    company_prefix: Mapped[Optional[str]] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    schema_config: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
```

### Database Lifecycle

- **`init_db()`**: Called in FastAPI lifespan hook at startup — `metadata.create_all()`. Only creates tables that don't exist.
- **No Alembic migrations** configured. Schema changes require manual `drop_db()` or direct SQL.
- **Connection:** `create_async_engine()` with `postgresql+asyncpg`, `pool_pre_ping=True`, `expire_on_commit=False`.

---

## 5. LLM Orchestration & Agentic Loop

### Model Selection (`core/llm_router.py`)

| Condition | Model | Rationale |
|-----------|-------|-----------| 
| Iteration 0 + conditional keywords detected | `deepseek-reasoner` | Chain-of-thought for "if X then Y" logic |
| All other cases | `deepseek-chat` (V3) | Faster, cheaper, no CoT leakage risk |

**Conditional keywords:** `if`, `only if`, `check first`, `depending on`, `unless`, `conditional`, `where`.

### LLM Client Configuration (`chat.py`)

```python
client = AsyncOpenAI(
    api_key=settings.DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"  # or OpenAI URL heuristic
)
```

The base URL is determined by a heuristic: if `DEEPSEEK_API_KEY` does not contain `"sk-"` + alphanumeric OpenAI pattern, it defaults to DeepSeek's API endpoint.

### Agentic Loop (`core/agentic_loop.py`)

- **Max iterations:** 8 (hard limit)
- **System prompt strategy:**
  - Iteration 0: Full `SYSTEM_PROMPT` with `column_map` JSON and complete schema context
  - Iterations 1+: `SYSTEM_PROMPT_COMPACT` (shorter, saves tokens)
- **DSML leakage guard:** If response content contains `<｜｜DSML｜｜>` markers, the response is discarded and retried with `deepseek-chat` (not reasoner)
- **CoT stripping:** `reasoning_content` attribute from deepseek-reasoner is logged internally but never sent to the client
- **Tool dispatch flow:** For each `tool_call`, sends `tool_start` WS message → RBAC check → `dispatch_tool()` → `tool_result` WS message
- **Streaming:** The final assistant text is sent via `{"type": "assistant", "content": "...", "done": true}` as a complete message (not token-by-token streaming)

---

## 6. Tool System & RBAC Enforcement

### Tool Catalogue (9 tools in `core/tool_schemas.py`)

| Tool | Path | Description |
|------|------|-------------|
| `get_row` | READ (direct) | Fetch WRICEF object by ID |
| `search_rows` | READ (direct) | Multi-filter search with AND logic, 3 match types (exact, contains, blank) |
| `summarize` | READ (direct) | Aggregation reports: count_by_field, completion_rate, blank_fields, overdue |
| `switch_module` | READ (direct) | Change active sheet tab (validates tab existence) |
| `data_quality` | READ (direct) | Validation checks: blank fields, stale items, consistency |
| `update_cell` | WRITE (queued) | Update one or more field values for a single RICEFW ID |
| `bulk_update` | WRITE (queued) | Batch update field across multiple IDs (by list or filter) |
| `format_row` | WRITE (queued) | Apply background color to row/cells (5 colors) |
| `add_row` | WRITE (queued) | Append new WRICEF object with auto-generated ID |

### Tool Dispatch (`core/tool_dispatch.py`)

```python
READ_TOOLS = {"get_row", "search_rows", "summarize", "switch_module", "data_quality"}
WRITE_TOOLS = {"update_cell", "bulk_update", "format_row", "add_row"}
```

- **READ tools** execute directly via `sheets/read.py` or `sheets/meta.py` and return results immediately.
- **WRITE tools** pre-read old values for auditing, then enqueue via `enqueue_write_job()` to Redis.

### RBAC Model (`core/permissions.py`)

| Tier | Capabilities | Resolution |
|------|-------------|------------|
| **Admin** | Bypass all restrictions | Matched via `ADMIN_EMAILS` env var |
| **Editor** | Write access, field-level restrictions, tool blacklists | DB `permissions` table lookup |
| **Viewer** | Read-only tools only | DB `permissions` table lookup |
| **Default** | Editor with `["*"]` access | Fallback when no DB record exists |

**Permission Resolution Chain:**
1. Check `ADMIN_EMAILS` → if match, grant all
2. Query `permissions` table for `(user_id, project_id)` → resolve role
3. For editors: check `allowed_fields` list for write operations
4. Check `denied_operations` list for tool blacklists
5. No record found → fall back to default editor with `["*"]` access

---

## 7. Embedded Sheets Layer

All Google Sheets API interactions are encapsulated in `backend/app/sheets/`:

| File | Functions | Notes |
|------|-----------|-------|
| `client.py` | `build_sheets_service()` | Builds `googleapiclient` from OAuth token, uses `cache_discovery=False` |
| `read.py` | `get_row()`, `search_rows()`, `summarize()`, `run_data_quality_check()`, `find_row_num()`, `get_row_raw()`, `get_bulk_rows_raw()` | Schema-driven column resolution via `_get_tab_schema()` |
| `write.py` | `update_cell()`, `bulk_update()`, `add_row()` | Uses `values.batchUpdate` for minimal API calls; `bulk_update` has individual retry fallback |
| `format.py` | `format_row()` | `repeatCell` color formatting via `batchUpdate`; COLOR_MAP: red/green/amber/blue/white |
| `meta.py` | `_detect_header_row()`, `get_sheet_id()`, `get_header_row()`, `get_all_ids()`, `detect_prefix()`, `next_ricefw_id()`, `switch_module()` | Header scanning (canonical markers: ricefw id, module, description, type), RICEFW ID sequence generation |
| `retry.py` | `_with_retry()` | Exponential backoff: base 1.0s, max 4 attempts, transient codes: {429, 500, 503} |

### Multi-Tab Schema Resolution

All sheet operations support multi-tab configurations via `_get_tab_schema()`:

```python
def _get_tab_schema(schema_config: dict, active_tab: str) -> dict:
    if "tabs" in schema_config:
        return schema_config.get("tabs", {}).get(active_tab, {})
    return schema_config
```

This allows both legacy single-tab configs and the newer `{tabs: {SD: {...}, MM: {...}}}` format.

### Key Defaults

- **`data_start_row`**: defaults to `3` (header row assumed at row 2)
- **`primary_id_position`**: defaults to `"B"` (column B contains RICEFW IDs)
- **`primary_id_column`**: defaults to `"RICEFW ID"`
- **Scan limit**: `search_rows()` and `summarize()` scan up to 2000 data rows

---

## 8. Queue-Backed Write Path

### Architecture

```text
Tool dispatch → pre-read old_values → enqueue_write_job() → Redis RPUSH "migrationbot:write_queue"
                                                                    │
                                                          ┌─────────┴──────────┐
                                                          │  Worker Process     │
                                                          │  (separate Docker   │
                                                          │   container)        │
                                                          │  BLPOP consumer     │
                                                          │  1-sec sleep/job    │
                                                          └─────────┬──────────┘
                                                                    │
                                                          Google Sheets API write
                                                                    │
                                                          Audit log → PostgreSQL
```

### Queue Schema (`WriteJobPayload`)

```python
class WriteJobPayload(BaseModel):
    user_email: str
    google_access_token: str = "mock-google-access-token"
    session_id: Optional[UUID] = None
    tool_name: str
    spreadsheet_id: str
    sheet_tab: str
    args: Dict[str, Any] = Field(default_factory=dict)
    old_values: Dict[str, Any] = Field(default_factory=dict)
```

### Worker (`queue/worker.py`)

- Runs as separate Docker container (`python -m app.queue.worker`)
- BLPOP with 10-second timeout (async)
- Processes one job at a time with 1-second `asyncio.sleep()` between jobs
- Handles all 4 write tools: `update_cell`, `bulk_update`, `format_row`, `add_row`
- Fetches fresh `schema_config` from PostgreSQL per job
- Logs audit records via `_write_audit_record()` — per field for `update_cell`, per RICEFW ID for `bulk_update`
- `add_row` auto-generates RICEFW ID via `next_ricefw_id()` if not supplied

### Job Envelope Format

```json
{
  "job_id": "uuid4-string",
  "payload": { /* WriteJobPayload fields */ }
}
```

---

## 9. Column Mapping (LLM-Driven & Fallbacks)

### Static Aliases (`core/column_mapper.py`)

72 pre-defined aliases covering SAP/WRICEF domain terms. Examples:
- `"ricefw id"` → `["object id", "tracker id", "rice id", ...]`
- `"dev status"` → `["development status", "status", "progress", ...]`
- `"technical resource"` → `["developer", "assigned to", "resource", ...]`

### Resolution Pipeline (`resolve_column()`)

1. **Exact match** against canonical keys (case-insensitive, stripped)
2. **Alias list match** against all alias entries
3. **Fuzzy match** via `difflib.get_close_matches` (cutoff 0.6)

### LLM Two-Pass Mapper (`build_column_map()`)

- **Pass 1:** Send actual headers to DeepSeek → generate 3-6 natural-language aliases per header
- **Pass 2:** Send Pass 1 result + actual headers back → verify, correct ambiguities, add missing SAP terms
- **Hallucination guard:** After each pass, strips any keys not present in the actual header row
- **Fallback:** If LLM calls fail, returns static `COLUMN_ALIASES` dictionary

---

## 10. Schema Auto-Detection

### API Endpoint

`POST /api/admin/projects/detect-metadata`
- Input: `{ "spreadsheet_url": "..." }`
- Headers: `Authorization: Bearer <JWT>`, `X-Google-Access-Token: <token>`
- Output: `{ "spreadsheet_id": "...", "detected_config": { tabs: {...}, global: {...} } }`

### Detection Flow (`core/schema_detect.py`)

1. Parse spreadsheet ID from URL
2. Fetch all tab names from spreadsheet metadata
3. For each tab: read first 5 rows for headers + 3 sample data rows
4. Send to DeepSeek via `detect_schema_config()` → returns per-tab config
5. Aggregate all tabs into `detect_all_tabs()` response

### Per-Tab Detection Output

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
  "data_start_row": 3,
  "column_map": { /* LLM-generated aliases */ }
}
```

### Current Integration Status

- The `/api/admin/projects/detect-metadata` endpoint exists and works.
- The frontend `admin/projects/page.tsx` has a full **Auto-Detect Wizard** UI that calls this endpoint.
- However, `admin.py:create_project()` still creates projects with `schema_config = {}` initially. The schema is applied via a subsequent `PUT` update after creation.
- The `detect_schema_config()` function is NOT called automatically during standard `POST /api/admin/projects` creation — it requires the admin to explicitly click "Analyze" in the wizard.

---

## 11. Audit Logging System

### Storage

PostgreSQL `audit_logs` table with 13 columns + computed `created_month` for partitioning readiness.

### Dual Logging Paths

| Path | Used By | Pattern |
|------|---------|---------|
| `_write_audit_record()` | Queue worker (`worker.py`) | Direct insert in worker context |
| `log_audit()` | Agentic loop (available but minimal usage) | `asyncio.create_task()` fire-and-forget |

### Audit Columns

| Column | Type | Description |
|--------|------|-------------|
| `timestamp` | TIMESTAMPTZ | Default `func.now()` |
| `user_email` | VARCHAR(255) | Authenticated user |
| `session_id` | UUID | WebSocket session reference |
| `tool_name` | VARCHAR(50) | Which tool was invoked |
| `spreadsheet_id` | VARCHAR(255) | Target spreadsheet |
| `sheet_tab` | VARCHAR(100) | Active tab |
| `ricefw_id` | VARCHAR(50) | Target object ID |
| `field` | VARCHAR(255) | Modified column |
| `old_value` | TEXT | Value before change |
| `new_value` | TEXT | Value after change |
| `args_json` | JSON | Full tool arguments |
| `result_ok` | BOOLEAN | Success/failure |
| `error` | TEXT | Error message if failed |

### Dead Code

`log_update_cell()`, `log_bulk_update()`, `log_format_row()`, `log_add_row()` convenience wrappers in `core/audit.py` are **never called** from any code path. The worker calls `_write_audit_record()` directly.

---

## 12. Data Quality & Analytics Engine

### `DataQualityChecker` (`core/data_quality.py`)

Framework-agnostic class operating on `headers[]`, `rows[][]`, and `schema_config`:

| Method | Purpose |
|--------|---------|
| `blank_field_counts(fields)` | Count blank cells per column across all rows |
| `completeness_score()` | Critical field fill rate (0-100%) based on `schema_config.critical_fields` |
| `stale_items(audit_entries, threshold)` | Items inactive for N days based on audit log timestamps |
| `consistency_checks(valid_emails)` | 4 logical contradiction detection rules |

### Consistency Rules

1. Items with status="Completed" but missing Sign-Off Date
2. Items with status="Completed" but missing Completion Date
3. Items with type in [R,I,C,E,F,W] but blank Dev Status
4. Assigned user email not found in registered audit trail emails

### Invocation

Called via the `data_quality` tool through `run_data_quality_check()` in `sheets/read.py`, which:
1. Fetches headers and all rows from the spreadsheet
2. Instantiates `DataQualityChecker(headers, rows, schema_config)`
3. Queries audit log for staleness evaluation
4. Returns `{completeness_score, alerts, stale_items}`

---

## 13. Next.js Frontend — Complete Component Audit

### Technology Stack

| Concern | Choice | Version |
|---------|--------|---------|
| Framework | Next.js (App Router) | 16.2.9 |
| Runtime | React | 19.2.4 |
| Styling | Tailwind CSS | v4 |
| Auth | NextAuth.js | v5.0.0-beta.31 |
| State | Zustand | 5.0.14 |
| Charts | Recharts | 2.15.0 |
| Icons | Lucide React | 0.468.0 |
| Animation | Framer Motion | 12.42.0 (imported but unused in current pages) |
| WebSocket | Native WebSocket + custom hook | — |

### Routes

| Route | File | Auth | Description |
|-------|------|------|-------------|
| `/` | `app/page.tsx` | Public | Landing page with Google sign-in, feature cards, animated blobs |
| `/chat` | `app/chat/page.tsx` | User | Main chat interface with WS, project/tab selectors |
| `/admin` | `app/admin/page.tsx` | Admin | Overview dashboard with 4 metrics + 2 charts |
| `/admin/projects` | `app/admin/projects/page.tsx` | Admin | Project CRUD + Auto-Detect Wizard + schema JSON editor |
| `/admin/users` | `app/admin/users/page.tsx` | Admin | User permission assignment (role, fields, denied ops) |
| `/admin/audit` | `app/admin/audit/page.tsx` | Admin | Filterable, paginated audit log table |
| `/api/auth/*` | `app/api/auth/[...nextauth]/route.ts` | — | NextAuth API endpoints |

### Chat Page Architecture (`chat/page.tsx`)

- **State:** Zustand store (`useChatStore`) for projects, messages, connection status, active tab
- **WebSocket:** Custom `useWebSocket` hook manages connection lifecycle, message dispatch, reconnection
- **Project selector:** Dropdown populated from `/api/projects` (backend REST)
- **Module tabs:** Reads from `activeProject.schema_config.tabs` keys; falls back to `schema_config.global.valid_modules`; hardcoded fallback `["SD", "MM", "FI", "CO", "PP", "QM"]`
- **Tab switching:** Sends natural language "Switch active module to {tab}" via WS
- **Tool visualization:** Each assistant message can have `toolCalls[]` with `running`/`completed`/`failed` status indicators
- **Toast notifications:** Listens for `queue_update` CustomEvents from WebSocket hook

### Admin Dashboard Architecture (`admin/page.tsx`)

- **Data sources:** Fetches from `/api/admin/projects`, `/api/admin/permissions`, `/api/admin/audits?limit=500`
- **Metrics cards:** Active Projects, Authorized Users, Audit Entries, Failed Operations
- **Charts:**
  - Area chart: Operations over time (success vs failed, last 7 active days)
  - Bar chart: Tool distribution (count by tool_name)
- **Chart library:** Recharts with custom dark-theme tooltips

### Admin Projects Page (`admin/projects/page.tsx`)

- **Modes:** Auto-Detect Wizard (default for create) and Manual Mode
- **Auto-Detect flow:**
  1. Admin enters Google Sheet URL
  2. Clicks "Analyze" → `POST /api/admin/projects/detect-metadata`
  3. Returns detected tabs with per-tab schema configs
  4. Tabs displayed as checkboxes — admin can select/deselect
  5. Generates JSON schema config automatically
  6. Admin fills project name, company prefix, default tab
  7. Save → `POST /api/admin/projects` + `PUT /api/admin/projects/:id` (schema update)
- **Manual Mode:** Direct JSON schema editor textarea

### Design System (globals.css)

- **Color palette:** Deep space black (`#030014`), dark indigo (`#0b0726`), indigo/cyan/purple accents
- **Glass effects:** `glass-panel` (blur 16px), `glass-card` (blur 12px)
- **Chat bubbles:** User = gradient indigo→purple, Agent = subtle glass with border
- **Animations:** `animate-pulse-slow` (4s), `animate-slide-up` (0.3s), `animate-blob` (7s)
- **Scrollbar:** Custom thin dark scrollbar

---

## 14. WebSocket Protocol

### Message Types

```jsonc
// Client → Server
{"type": "message", "content": "Set SD-045 status to Done"}
{"type": "ping"}

// Server → Client
{"type": "connection_ok", "user_email": "...", "project_name": "...", "active_tab": "SD"}
{"type": "assistant", "content": "...", "done": true}
{"type": "tool_start", "tool": "update_cell", "args": {...}}
{"type": "tool_result", "tool": "update_cell", "result": {...}}
{"type": "queue_update", "job_id": "...", "status": "completed"}
{"type": "error", "message": "Permission denied: ..."}
{"type": "pong"}
```

### Connection Lifecycle

1. Client opens WebSocket with `?token=<JWT>&project_id=<id>`
2. Server authenticates JWT, creates session in DB
3. Server sends `connection_ok` with user context
4. Client starts 30-second ping heartbeat
5. On disconnect (non-auth): client auto-reconnects after 3 seconds
6. Auth failures (code 1008) and clean closes (code 1000): no reconnect

---

## 15. Deployment Architecture

### Docker Compose Stack (6 services)

| Service | Image | Port | Purpose |
|---------|-------|------|---------|
| `postgres` | postgres:16 | 5433:5432 | Data store (pgdata volume) |
| `redis` | redis:7-alpine | 6379 | Write queue |
| `backend` | ./backend | 8000 | FastAPI API server (uvicorn) |
| `worker` | ./backend | — | Queue consumer (`python -m app.queue.worker`) |
| `frontend` | ./frontend | 3000 | Next.js app (standalone, non-root user) |
| `caddy` | caddy:2-alpine | 80/443 | Reverse proxy + auto-HTTPS (Let's Encrypt) |

### Caddy Routing (Caddyfile)

```
migrationbot.duckdns.org {
    route {
        /api/auth/* → frontend:3000   (NextAuth endpoints — HIGHEST priority)
        /api/*      → backend:8000    (FastAPI REST)
        /ws*        → backend:8000    (WebSocket)
        *           → frontend:3000   (Catch-all)
    }
}
```

### Volumes

- `pgdata`: PostgreSQL persistent data
- `caddy_data`: TLS certificates
- `caddy_config`: Caddy configuration cache

---

## 16. CI/CD Pipeline

### CI Pipeline (`.github/workflows/ci.yml`)

**Triggers:** Push/PR to `main`, `develop`, `phase3` branches.

| Job | Steps |
|-----|-------|
| `lint-and-test` | Python 3.12, Postgres 16 service, Redis 7 service → `pip install` → `pytest -v` |
| `frontend-build` | Node 20 → `npm ci` → `npm run build` |

### CD Pipeline (`.github/workflows/deploy.yml`)

**Triggers:** Push to `main` or `phase3` branches.

**Deployment:** SSH to VPS via `appleboy/ssh-action@v1.0.3`:
```bash
cd ~/migrationbot && git pull origin main
docker compose down && docker compose up --build -d
```

---

## 17. Performance Architecture

- **Batch operations:** `bulk_update()` uses `values.batchUpdate` for O(1) API calls; falls back to individual writes on failure
- **Full range scans:** `search_rows()` and `summarize()` download up to 2000 rows in one GET, filtering in Python
- **Write throttling:** Worker enforces 1-second `asyncio.sleep()` between Sheets API writes
- **Retry with backoff:** `_with_retry()` handles 429/500/503 with exponential backoff (1s, 2s, 4s, 8s)
- **Connection pooling:** SQLAlchemy async engine with `pool_pre_ping=True`
- **WebSocket heartbeat:** 30-second ping prevents connection timeouts through Caddy/proxy

---

## 18. Feature Interaction Map

```text
Schema Auto-Detection (schema_detect.py)
        ├── depends on: sheets/meta.py headers + sample data + LLM
        ├── WIRED into admin detect-metadata endpoint
        ├── NOT auto-triggered on project create (requires explicit "Analyze" click)
        └── feeds into: projects.schema_config JSON

Column Mapping (column_mapper.py)
        ├── static COLUMN_ALIASES (72 entries) as fallback
        ├── LLM 2-pass build_column_map() available, used in schema detection
        ├── resolve_column() used by: sheets/read.py, sheets/write.py, sheets/format.py
        └── BUG: write.py and format.py call resolve_column() without column_map

RBAC (core/permissions.py)
        ├── ADMIN_EMAILS env var → admin bypass
        ├── PostgreSQL permissions table → editor/viewer roles
        ├── field-level enforcement for update_cell/bulk_update
        └── enforced in: agentic_loop.py per tool_call

Frontend Admin Guard
        ├── HARDCODED email substring check (rohai/ruhail/admin)
        ├── ALSO queries /api/auth/me for is_admin flag
        └── CONFLICT: two admin detection systems active simultaneously

Agentic Loop (core/agentic_loop.py)
        ├── depends on: all 9 tool schemas, LLM router, permission checker
        ├── dispatches to: tool_dispatch.py
        ├── streams results via: WebSocket send_msg callback
        └── DSML leakage guard forces deepseek-chat fallback

Queue Worker (queue/worker.py)
        ├── consumes: Redis migrationbot:write_queue
        ├── executes: sheets/write.py, sheets/format.py, sheets/meta.py
        ├── logs: core/audit.py _write_audit_record (per field/per ID)
        └── NO WebSocket feedback to client (queue_update never sent)

Audit Logger (core/audit.py)
        ├── asyncio.create_task pattern available for non-blocking writes
        ├── _write_audit_record() used directly by worker
        └── 4 convenience wrappers defined but NEVER CALLED (dead code)
```

---

## 19. Known Bugs, Discrepancies & Technical Debt

### Critical (P0)

1. **Schema detection not auto-triggered on project creation:**
   - `admin.py:create_project()` creates projects with `schema_config = {}`.
   - The "Analyze" button exists in the UI but is a manual step. New projects created via the API directly will have empty schemas.
   - Impact: Chat agent cannot function without schema_config — all column lookups will use hardcoded defaults.

2. **`data_quality` tool missing from RBAC tool sets:**
   - `permissions.py` defines `READ_ONLY_TOOLS` and `WRITE_TOOLS` but `data_quality` is absent from both.
   - `tool_dispatch.py` routes it as a read tool, but RBAC will reject it for Viewers since it's not in `READ_ONLY_TOOLS`.

3. **`resolve_column()` called without `column_map` in write operations:**
   - `write.py:update_cell()` line 42: `resolve_column(field, column_map)` where `column_map` is extracted from tab_schema but may be `None`.
   - `write.py:bulk_update()` line 95: Same pattern.
   - `format.py:format_row()` line 53: `resolve_column("Color", column_map)` where `column_map` comes from schema but may be `None`.
   - Impact: LLM-generated dynamic aliases are ignored during write operations, falling back to static aliases only.

4. **Hardcoded admin detection in frontend (TWO conflicting systems):**
   - `chat/page.tsx` line 161: `isAdmin = isAdminState || email.includes("rohai")...` — hybrid check.
   - `admin/layout.tsx` line 28: `isAdmin = ["rohai", "ruhail", "admin"].some(key => email.includes(key))` — pure hardcoded.
   - Should exclusively query `/api/auth/me` for `is_admin` flag from the backend.

5. **Hardcoded module tabs fallback in chat:**
   - `chat/page.tsx` line 208: Falls back to `["SD", "MM", "FI", "CO", "PP", "QM"]` when `schema_config.tabs` and `schema_config.global.valid_modules` are both missing.
   - No tabs should be shown when no project is loaded or schema is empty.

### High (P1)

6. **No queue job status feedback to client:**
   - Worker processes jobs but never sends `queue_update` WS messages back.
   - The `queue_update` event handler in the frontend (chat/page.tsx lines 103-125) is wired but never triggered.
   - Impact: Users get no feedback when writes complete or fail.

7. **Blocking `_with_retry()` in async context:**
   - `retry.py` line 15: `return fn()` calls the synchronous Google API client directly.
   - `asyncio.sleep(delay)` is used for backoff, but the actual `fn()` call is synchronous.
   - In an async FastAPI context, this blocks the event loop during Google API calls.

8. **`datetime.utcnow()` deprecation:**
   - `chat.py` line 204: `datetime.utcnow()` is deprecated in Python 3.12+.
   - Should use `datetime.now(timezone.utc)`.

9. **`get_all_ids()` default Column B:**
   - `meta.py:get_all_ids()` line 55: `primary_id_pos` parameter defaults to `"B"` but callers should always pass the schema value.
   - Similarly `find_row_num()` defaults to `"B"`.

10. **No Alembic migrations:**
    - Schema changes require manual table drops. `init_db()` only creates tables that don't exist; it cannot modify existing columns.
    - Risk: Data loss on schema evolution in production.

11. **Frontend projects fetch hits chat API route:**
    - `chat/page.tsx` fetches `/api/projects` which is defined in `chat.py`, not `admin.py`.
    - This is a REST endpoint on the chat router, mixing concerns.

### Medium (P2)

12. **`updateLastMessage` race condition:**
    - Multiple rapid WS messages (assistant content + tool_start) can interleave in `useChatStore`.
    - The `updateLastMessage` updater function operates on `messages[length-1]` which may not be the intended message.

13. **Audit convenience wrappers are dead code:**
    - `log_update_cell()`, `log_bulk_update()`, `log_format_row()`, `log_add_row()` in `core/audit.py` are defined but never called from any code path.

14. **No CORS domain restriction in production:**
    - `main.py` sets `allow_origins` from `CORS_ORIGINS` env var, which defaults to `"*"` in config.py.
    - `.env.example` also has `CORS_ORIGINS=*`.
    - Must be locked down for production deployment.

15. **Default Zustand store tab is hardcoded "SD":**
    - `useChatStore.ts` line 49: `activeTab: "SD"` as default.
    - Should be empty or derived from the active project's `default_tab`.

16. **`connection_ok` handler incomplete:**
    - `useWebSocket.ts` handles `connection_ok` to set `activeTab`, but doesn't update project context, user role, or other session metadata sent by the server.

### Low (P3)

17. **LLM client base_url heuristic is fragile:**
    - `chat.py` uses API key pattern matching to determine DeepSeek vs OpenAI base URL.
    - Should be an explicit environment variable.

18. **`tests/` directory potentially gitignored:**
    - `.gitignore` may include `tests/` — test code may not be version controlled.

19. **No header row caching:**
    - `get_header_row()` is called on every read/write operation.
    - Original implementation plan specified Redis-based header caching (TTL 5 min) but it was never implemented.

20. **No RICEFW ID-to-row caching:**
    - `find_row_num()` scans the entire ID column on every call.
    - Original plan specified Redis caching but it was never implemented.

21. **`framer-motion` imported but unused:**
    - Package is in `package.json` dependencies but not imported in any current page component.

22. **`jose` library used in frontend but not in `package.json`:**
    - `auth.ts` imports from `jose` (SignJWT), but `jose` is not listed in `package.json` — it's likely a transitive dependency of `next-auth`.
