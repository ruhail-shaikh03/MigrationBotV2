# MigrationBot Enterprise Portal — Implementation Plan (v2.0)

**Version:** 2.0 (Next-Phase Roadmap)  
**Status:** Planning Phase — All Prior Phases Deployed  
**Date:** July 2026  
**Previous:** v1.0 Implementation Plan (Phases 0-5 completed, deployed to Hetzner)  
**Reference:** TDD.md for current state documentation

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Current State Assessment](#2-current-state-assessment)
3. [Phase 6: Critical Bug Fixes & Hardcoding Elimination](#3-phase-6-critical-bug-fixes--hardcoding-elimination)
4. [Phase 7: Dynamic Admin — Zero-Config Sheet Onboarding](#4-phase-7-dynamic-admin--zero-config-sheet-onboarding)
5. [Phase 8: Agentic AI Orchestration Layer](#5-phase-8-agentic-ai-orchestration-layer)
6. [Phase 9: Advanced Admin Dashboard & Analytics](#6-phase-9-advanced-admin-dashboard--analytics)
7. [Phase 10: User Dashboard & Self-Service Features](#7-phase-10-user-dashboard--self-service-features)
8. [Phase 11: Frontend Polish & UX Overhaul](#8-phase-11-frontend-polish--ux-overhaul)
9. [Phase 12: Production Hardening & Observability](#9-phase-12-production-hardening--observability)
10. [Verification Plan](#10-verification-plan)
11. [Risk Mitigation](#11-risk-mitigation)
12. [Implementation Priority Matrix](#12-implementation-priority-matrix)

---

## 1. Executive Summary

MigrationBot has been successfully deployed to Hetzner via Docker Compose with CI/CD. The core architecture (FastAPI + Next.js + PostgreSQL + Redis + DeepSeek) is operational. However, significant gaps remain:

| Area | Current State | Target State |
|------|--------------|--------------|
| Admin Config | Manual JSON editing, hardcoded defaults | **Zero-config**: paste URL → auto-detect everything |
| Frontend Bugs | Hardcoded admin checks, static tab fallbacks | **Fully dynamic**: all data from backend APIs |
| AI Strategy | Single LLM call per turn, basic tool dispatch | **Agentic orchestration**: multi-step planning, context-aware routing |
| Admin Features | Basic CRUD + 2 charts | **Rich dashboard**: summaries, KPIs, alerts, export |
| User Experience | Basic chat only | **User dashboard**: personal stats, favorites, shortcuts |
| Queue Feedback | No write completion notifications | **Real-time**: WS push on job completion/failure |
| Observability | Console logging only | **Structured logging**: metrics, health checks, alerting |

**Key Principle:** Everything must be dynamic. No hardcoded values. Every configuration, label, module list, tab name, column mapping, and admin email must be sourced from the database, environment variables, or auto-detected at runtime.

---

## 2. Current State Assessment

### What's Working ✅

- Google OAuth → NextAuth → JWT → FastAPI authentication pipeline
- WebSocket chat with agentic loop (9 tools, RBAC enforcement)
- Queue-backed writes with Redis (producer → worker → audit log)
- Admin project CRUD with Auto-Detect Wizard (URL → analyze tabs)
- Admin user permission management (role, fields, denied ops)
- Admin audit log viewer
- Admin overview dashboard (4 metrics + 2 charts)
- Schema auto-detection via LLM per tab
- Caddy reverse proxy with auto-HTTPS on `migrationbot.duckdns.org`
- CI/CD via GitHub Actions (pytest + build → SSH deploy)

### What's Broken / Missing ❌

| Issue | Severity | File(s) |
|-------|----------|---------|
| Hardcoded admin email check in frontend | P0 | `chat/page.tsx:161`, `admin/layout.tsx:28` |
| Hardcoded module tab fallback `["SD","MM","FI","CO","PP","QM"]` | P0 | `chat/page.tsx:208` |
| Default Zustand `activeTab: "SD"` hardcoded | P1 | `useChatStore.ts:49` |
| `data_quality` missing from RBAC tool sets | P0 | `permissions.py` |
| `resolve_column()` called without `column_map` in writes | P0 | `write.py`, `format.py` |
| No queue job completion feedback via WebSocket | P1 | `worker.py` |
| Blocking `_with_retry()` in async context | P1 | `retry.py` |
| No Alembic migrations | P1 | `db/engine.py` |
| Audit convenience wrappers are dead code | P2 | `core/audit.py` |
| CORS set to `*` in production | P2 | `config.py`, `main.py` |
| No header/ID caching in Redis | P2 | `sheets/meta.py`, `sheets/read.py` |
| `framer-motion` imported but unused | P3 | `package.json` |

---

## 3. Phase 6: Critical Bug Fixes & Hardcoding Elimination

> **Goal:** Fix all P0/P1 bugs, remove every hardcoded value, make the entire stack fully dynamic.

### 6.1 Frontend: Remove Hardcoded Admin Detection

**Files:** `frontend/src/app/chat/page.tsx`, `frontend/src/app/admin/layout.tsx`

**Current (BROKEN):**
```typescript
// chat/page.tsx line 161
const isAdmin = isAdminState || (email ? ["rohai", "ruhail", "admin"].some(adminKey => email.includes(adminKey)) : false)

// admin/layout.tsx line 28
const isAdmin = email ? ["rohai", "ruhail", "admin"].some(adminKey => email.includes(adminKey)) : false
```

**Fix:** Remove ALL hardcoded email checks. Admin status comes exclusively from the backend `/api/auth/me` endpoint:

```typescript
// Both files: replace with:
const [isAdmin, setIsAdmin] = useState(false)
useEffect(() => {
  if (apiToken) {
    fetch('/api/auth/me', { headers: { Authorization: `Bearer ${apiToken}` } })
      .then(r => r.json())
      .then(data => setIsAdmin(data?.is_admin === true))
      .catch(() => setIsAdmin(false))
  }
}, [apiToken])
```

### 6.2 Frontend: Remove Hardcoded Module Tabs

**File:** `frontend/src/app/chat/page.tsx`

**Current (BROKEN):**
```typescript
// line 208: fallback to hardcoded modules
(activeProject?.schema_config?.global?.valid_modules || ["SD", "MM", "FI", "CO", "PP", "QM"])
```

**Fix:** Remove hardcoded fallback entirely. Show no tabs when schema is unavailable:

```typescript
const moduleTabs = activeProject?.schema_config?.tabs 
  ? Object.keys(activeProject.schema_config.tabs)
  : (activeProject?.schema_config?.global?.valid_modules || [])
// Render nothing if empty — no fallback array
```

### 6.3 Frontend: Remove Hardcoded Default Tab

**File:** `frontend/src/store/useChatStore.ts`

**Current:** `activeTab: "SD"` hardcoded default.

**Fix:** Default to empty string; set from project's `default_tab`:
```typescript
activeTab: "",
// In setActiveProject:
setActiveProject: (project) => set({
  activeProject: project,
  activeTab: project ? project.default_tab : ""
}),
```

### 6.4 Backend: Add `data_quality` to RBAC Tool Sets

**File:** `backend/app/core/permissions.py`

**Fix:** Add `"data_quality"` to `READ_ONLY_TOOLS`:
```python
READ_ONLY_TOOLS = {"get_row", "search_rows", "summarize", "switch_module", "data_quality"}
```

### 6.5 Backend: Pass `column_map` to All `resolve_column()` Calls

**Files:** `backend/app/sheets/write.py`, `backend/app/sheets/format.py`

**Fix:** Extract `column_map` from `schema_config` and pass it to `resolve_column()` in all functions:

```python
# In update_cell():
column_map = tab_schema.get("column_map") or schema_config.get("column_map") or {}
canonical = resolve_column(field, column_map) or field

# In bulk_update():
column_map = tab_schema.get("column_map") or schema_config.get("column_map") or {}
canonical_set_field = resolve_column(set_field, column_map) or set_field

# In format_row():
column_map = tab_schema.get("column_map") or schema_config.get("column_map") or {}
color_col = resolve_column("Color", column_map) or "Color "
```

### 6.6 Backend: Fix Async Retry

**File:** `backend/app/sheets/retry.py`

**Fix:** Run synchronous Google API calls in a thread executor to avoid blocking:

```python
import asyncio
from concurrent.futures import ThreadPoolExecutor

_executor = ThreadPoolExecutor(max_workers=4)

async def _with_retry(fn, max_attempts: int = 4, base_delay: float = 1.0):
    delay = base_delay
    last_exc = None
    loop = asyncio.get_event_loop()
    
    for attempt in range(max_attempts):
        try:
            return await loop.run_in_executor(_executor, fn)
        except HttpError as exc:
            if exc.status_code not in {429, 500, 503}:
                raise
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(delay)
                delay *= 2
    if last_exc:
        raise last_exc
```

### 6.7 Backend: Fix datetime.utcnow() Deprecation

**File:** `backend/app/api/chat.py`

**Fix:** Replace `datetime.utcnow()` with `datetime.now(timezone.utc)`.

### 6.8 Backend: Lock Down CORS

**File:** `backend/app/config.py`, `.env.example`

**Fix:** Change default `CORS_ORIGINS` to the actual production domain:
```python
CORS_ORIGINS: str = "https://migrationbot.duckdns.org"
```

### 6.9 Cleanup Dead Code

**File:** `backend/app/core/audit.py`

**Fix:** Remove unused convenience wrappers (`log_update_cell`, `log_bulk_update`, `log_format_row`, `log_add_row`) or wire them into the dispatch layer properly. Recommendation: remove them since the worker handles audit logging directly.

---

## 4. Phase 7: Dynamic Admin — Zero-Config Sheet Onboarding

> **Goal:** Admin pastes a Google Sheet URL, everything else is automatic. All tabs, fields, column maps, and schema configs are detected, displayed, and configurable via UI — zero JSON editing required.

### 7.1 Enhanced Auto-Detection Flow

**Current:** Admin clicks "Analyze" → gets detected tabs → can select/deselect → saves JSON.

**Target:** 

```
Admin pastes Google Sheet URL
       │
       ▼
Backend: detect_all_tabs()
       │
       ├── For each tab:
       │   ├── Detect headers (row scan)
       │   ├── Detect data_start_row
       │   ├── Detect primary_id_column & position
       │   ├── Detect status/module/assignee columns
       │   ├── Detect date columns
       │   ├── Extract unique module values
       │   ├── Extract unique type values
       │   ├── Build LLM column_map (2-pass)
       │   └── Compute completeness preview
       │
       ├── Detect company prefix from IDs
       ├── Auto-generate project_name from spreadsheet title
       └── Return rich structured result
              │
              ▼
Frontend: Renders interactive config wizard
       │
       ├── Spreadsheet title → editable project name
       ├── Detected tabs → checkboxes (all selected by default)
       ├── Per-tab: expandable card showing detected fields
       │   ├── Primary ID column → dropdown (auto-selected)
       │   ├── Status column → dropdown (auto-selected)
       │   ├── Module column → dropdown (auto-selected)
       │   ├── Assignee column → dropdown (auto-selected)
       │   ├── Date columns → multi-select
       │   ├── Critical fields → checkboxes (top N auto-selected)
       │   └── Column aliases → read-only preview
       │
       ├── Global: company prefix (auto-detected, editable)
       ├── Default tab → dropdown from selected tabs
       └── Save → creates project with full schema_config
```

### 7.2 Backend: Enhanced Detection Endpoint

**File:** `backend/app/api/admin.py`

**New/Modified Endpoint:** `POST /api/admin/projects/detect-metadata`

Add to the detection response:
```json
{
  "spreadsheet_id": "...",
  "spreadsheet_title": "FF Migration Tracker",
  "detected_prefix": "FFC",
  "detected_config": {
    "tabs": {
      "SD": {
        "headers": ["Serial #", "RICEFW ID", "Module", "Type", ...],
        "sample_rows": [["1", "FFC-SD-001", "SD", "R", ...], ...],
        "row_count": 156,
        "detected": {
          "primary_id_column": "RICEFW ID",
          "primary_id_position": "B",
          "status_column": "Dev Status",
          ...
        },
        "column_map": { ... }
      }
    },
    "global": {
      "valid_modules": ["SD", "MM", "FI"],
      "company_prefix": "FFC"
    }
  }
}
```

### 7.3 Frontend: Visual Schema Editor (Replace JSON Textarea)

**File:** `frontend/src/app/admin/projects/page.tsx`

Replace the raw JSON `<textarea>` with a structured visual form:

- **Per-tab config card:** Expandable accordion with dropdown selectors for each semantic role (primary ID, status, module, etc.)
- **Field selector:** Multi-select chips showing all detected headers, with drag-to-reorder for critical fields
- **Column alias preview:** Read-only display of LLM-generated aliases per header
- **Live validation:** Show warnings if required roles (primary_id, status) are not mapped
- **Advanced toggle:** Collapse to show raw JSON for power users

### 7.4 Backend: Auto-Trigger Detection on Project Create

**File:** `backend/app/api/admin.py`

When `POST /api/admin/projects` is called with a `spreadsheet_id`, automatically trigger `detect_all_tabs()` if `schema_config` is empty:

```python
@router.post("/api/admin/projects")
async def create_project(body: CreateProjectRequest, ...):
    # Create project record
    project = Project(spreadsheet_id=body.spreadsheet_id, ...)
    db.add(project)
    await db.commit()
    
    # Auto-detect schema if not provided
    if not body.schema_config:
        try:
            config = await detect_all_tabs(
                google_token, body.spreadsheet_id, llm_client
            )
            project.schema_config = config
            await db.commit()
        except Exception as e:
            logger.warning(f"Auto-detection failed: {e}")
    
    return project
```

### 7.5 Backend: Dynamic Field Deselection API

**New Endpoint:** `PATCH /api/admin/projects/{id}/fields`

Allow admins to toggle individual fields on/off per tab without editing raw JSON:

```python
@router.patch("/api/admin/projects/{project_id}/fields")
async def update_field_selection(
    project_id: int,
    body: FieldSelectionUpdate,  # { tab: str, field: str, enabled: bool }
    ...
):
    # Update the schema_config.tabs[tab].critical_fields
    # Or add/remove from a new "hidden_fields" array
    ...
```

---

## 5. Phase 8: Agentic AI Orchestration Layer

> **Goal:** Upgrade from simple single-turn tool dispatch to a multi-step agentic AI that orchestrates, plans, and executes complex workflows autonomously.

### 8.1 Current vs Target Architecture

| Feature | Current | Target |
|---------|---------|--------|
| Planning | None — LLM picks one tool per turn | **Multi-step planner**: decomposes complex requests into execution plans |
| Context | System prompt + last user message | **Contextual memory**: session history, project metadata, past results |
| Model routing | Binary (reasoner vs chat) | **Tri-tier**: planner (complex), executor (standard), summarizer (post-tool) |
| Error recovery | None — returns error to user | **Auto-retry**: on tool failure, reformulate and retry with different approach |
| Multi-tool | Sequential (one per iteration) | **Parallel dispatch**: independent tools execute concurrently |

### 8.2 Multi-Step Planner

**New File:** `backend/app/core/planner.py`

When a user request requires multiple steps (e.g., "Mark all overdue SD items as Critical and assign to John"):

```python
class AgenticPlanner:
    """
    Decomposes complex user requests into executable step sequences.
    Uses LLM to generate a plan, then executes steps sequentially or in parallel.
    """
    
    async def create_plan(self, user_message: str, context: dict) -> ExecutionPlan:
        """
        Sends user request + available tools + current context to LLM.
        Returns a structured plan with ordered steps.
        """
        plan_prompt = f"""
        You are a migration tracker assistant planning engine.
        
        User request: {user_message}
        Available tools: {tool_descriptions}
        Current project: {context['project_name']}
        Active tab: {context['active_tab']}
        
        Decompose this request into a sequence of tool calls.
        For each step, specify:
        - tool_name: which tool to call
        - args: the arguments
        - depends_on: list of step indices this step depends on
        - can_parallel: whether this can run alongside other steps
        
        Return JSON array of steps.
        """
        # LLM call returns structured plan
        ...
    
    async def execute_plan(self, plan: ExecutionPlan, ws: WebSocket):
        """
        Executes the plan, sending progress updates via WebSocket.
        Handles dependency ordering and parallel dispatch.
        """
        for step_group in plan.get_parallel_groups():
            results = await asyncio.gather(*[
                self.execute_step(step) for step in step_group
            ])
            # Feed results into dependent steps
            ...
```

### 8.3 Contextual Session Memory

**New File:** `backend/app/core/memory.py`

Maintain session-level context across turns:

```python
class SessionMemory:
    """
    Maintains conversation context including:
    - Recent tool results (last 5 tool outputs)
    - User preferences (detected from conversation patterns)
    - Active filters (e.g., "focus on SD module")
    - Frequently accessed IDs
    """
    
    async def build_context_prompt(self, session_id: UUID) -> str:
        """Build a context summary for the system prompt."""
        ...
    
    async def update_from_tool_result(self, tool_name: str, result: dict):
        """Extract and cache useful context from tool execution results."""
        ...
```

### 8.4 Intelligent Model Router Enhancement

**File:** `backend/app/core/llm_router.py`

Upgrade from binary to tri-tier routing:

```python
def select_model(iteration: int, messages: list, complexity: str = "auto") -> str:
    """
    Tri-tier model routing:
    - deepseek-reasoner: Complex conditional logic, multi-step planning
    - deepseek-chat: Standard tool dispatch, response generation
    - deepseek-chat (compact): Post-tool summarization, simple confirmations
    """
    if complexity == "plan":
        return "deepseek-reasoner"
    
    if iteration == 0:
        # Analyze request complexity
        user_msg = get_last_user_message(messages)
        if requires_planning(user_msg):
            return "deepseek-reasoner"
        if has_conditional_logic(user_msg):
            return "deepseek-reasoner"
    
    if iteration > 3:
        # Later iterations are simpler summarizations
        return "deepseek-chat"  # could use a lighter model
    
    return "deepseek-chat"
```

### 8.5 Auto-Retry on Tool Failure

**File:** `backend/app/core/agentic_loop.py`

Add automatic retry logic when a tool call fails:

```python
# In the tool dispatch section:
result = await dispatch_tool(tool_call, session)

if not result.get("ok"):
    # Attempt auto-recovery
    retry_prompt = f"""
    The tool {tool_call.function.name} failed with error: {result.get('error')}.
    Original args: {tool_call.function.arguments}
    
    Reformulate the request to fix the error. Common fixes:
    - Column name not found → use a different alias
    - RICEFW ID not found → search for the correct ID first
    - Permission denied → explain the restriction to the user
    """
    # Re-invoke LLM with error context for recovery
```

### 8.6 WebSocket Progress Streaming for Plans

Extend the WebSocket protocol for plan execution:

```jsonc
// New server → client messages:
{"type": "plan_start", "steps": [...], "total": 3}
{"type": "plan_step", "step": 1, "tool": "search_rows", "status": "running"}
{"type": "plan_step", "step": 1, "tool": "search_rows", "status": "completed", "result_summary": "Found 12 items"}
{"type": "plan_step", "step": 2, "tool": "bulk_update", "status": "queued"}
{"type": "plan_complete", "summary": "Updated 12 items across 2 modules"}
```

---

## 6. Phase 9: Advanced Admin Dashboard & Analytics

> **Goal:** Transform the admin panel from basic CRUD into a comprehensive operations center with real-time insights, exportable reports, and proactive alerts.

### 9.1 Admin Dashboard Enhancement — New Widgets

**File:** `frontend/src/app/admin/page.tsx`

#### Existing (Keep + Enhance):
- 4 metric cards (projects, users, audits, errors) — add trend indicators (↑12% vs last week)
- Operations area chart — add date range selector
- Tool distribution bar chart — add click-to-filter

#### New Widgets:

| Widget | Type | Data Source | Description |
|--------|------|-------------|-------------|
| **User Activity Heatmap** | Heatmap/Calendar | `audit_logs` | Shows daily/hourly activity intensity |
| **Top Active Users** | Ranked list | `audit_logs` | Users by operation count, with last-active timestamp |
| **Module Coverage** | Donut chart | `sheets/summarize` | Completion % per module (FI, SD, MM, etc.) |
| **Data Quality Score** | Gauge/Radial | `data_quality` | Real-time completeness + consistency score |
| **Overdue Items Alert** | Alert card | `sheets/summarize` (overdue) | Count + top 5 most overdue items |
| **Queue Health** | Status card | Redis LLEN | Current queue depth, avg processing time |
| **Error Rate Trend** | Sparkline | `audit_logs` | 7-day rolling error rate |
| **Recent Failures** | Log feed | `audit_logs` (result_ok=false) | Last 10 failed operations with details |

### 9.2 Backend: Admin Analytics API

**New Endpoints:**

```
GET /api/admin/analytics/summary
  → { projects, users, audits_today, audits_week, error_rate, queue_depth }

GET /api/admin/analytics/activity-heatmap?days=30
  → { data: [{ date: "2026-07-01", hour: 14, count: 23 }, ...] }

GET /api/admin/analytics/top-users?limit=10&days=30
  → { users: [{ email, operations_count, last_active, most_used_tool }, ...] }

GET /api/admin/analytics/module-coverage?project_id=1
  → { modules: [{ name: "SD", total: 156, completed: 89, pct: 57.1 }, ...] }

GET /api/admin/analytics/quality-score?project_id=1
  → { completeness: 78.5, consistency_alerts: 12, stale_count: 5, overall: 72.3 }

GET /api/admin/analytics/queue-health
  → { depth: 3, avg_process_time_ms: 1250, failed_last_hour: 0 }
```

### 9.3 Admin: Project-Level Analytics Page

**New Route:** `/admin/projects/:id/analytics`

Per-project deep dive showing:
- **Completion dashboard:** Per-module progress bars
- **Timeline:** Activity over time for this project
- **Top contributors:** Users with most operations on this project
- **Data health:** Completeness score, blank field breakdown, overdue items
- **Audit trail:** Filtered audit log for this project only

### 9.4 Admin: Export & Reports

**New Feature:** Download buttons for CSV/PDF export:

```
GET /api/admin/export/audits?format=csv&project_id=1&from=2026-06-01
GET /api/admin/export/quality-report?format=pdf&project_id=1
```

### 9.5 Admin: Bulk User Management

**Enhancement to:** `frontend/src/app/admin/users/page.tsx`

- **Import users from CSV:** Upload a CSV with `email, project, role, allowed_fields`
- **Bulk role change:** Select multiple users → change role for all
- **Invitation flow:** Enter email → auto-assign to project with default editor role
- **Activity indicator:** Show last login and total operations per user

### 9.6 Admin: Notification System

**New Feature:** Configurable alert rules:

```
Alert when: error_rate > 10% in last hour
Alert when: queue_depth > 50
Alert when: data_quality_score < 60%
Alert when: user hasn't logged in for 30 days
```

Notifications delivered via:
- Admin dashboard badge/bell icon
- Email notifications (optional, via SMTP)

---

## 7. Phase 10: User Dashboard & Self-Service Features

> **Goal:** Give regular (non-admin) users a personalized dashboard with stats, shortcuts, and self-service tools.

### 10.1 User Dashboard Page

**New Route:** `/dashboard` (between login and chat)

| Widget | Description |
|--------|-------------|
| **My Recent Activity** | Last 10 operations by this user (tool, target, timestamp) |
| **My Projects** | Cards for each assigned project with quick-access buttons |
| **Quick Actions** | Shortcut buttons: "Check Overdue Items", "Run Data Quality", "View SD Summary" |
| **Personal Stats** | Operations today/this week, success rate, most-used tools |
| **Bookmarked Items** | Pinned RICEFW IDs for quick access |

### 10.2 Chat Enhancements

- **Suggested prompts:** Based on user's project and recent activity
- **Chat history:** Persist conversations across sessions (store in PostgreSQL)
- **Export chat:** Download conversation as PDF/text
- **Share results:** Copy tool output as formatted text

### 10.3 User Preferences API

**New Endpoint:** `GET/PUT /api/users/me/preferences`

```json
{
  "default_project_id": 1,
  "default_tab": "SD",
  "theme": "dark",
  "notification_preferences": {
    "queue_completion": true,
    "daily_summary": false
  },
  "bookmarked_ids": ["FFC-SD-001", "FFC-MM-023"],
  "quick_actions": [
    {"label": "Check SD Overdue", "prompt": "Show me all overdue SD items"}
  ]
}
```

---

## 8. Phase 11: Frontend Polish & UX Overhaul

> **Goal:** Fix all frontend bugs, improve UX, add responsive design, and ensure premium design quality.

### 11.1 Chat Page Bugs to Fix

| Bug | Fix |
|-----|-----|
| Module tabs show hardcoded fallback | Show empty state when no schema (Phase 6.2) |
| Admin button uses email substring check | Use `/api/auth/me` response (Phase 6.1) |
| `updateLastMessage` race condition | Queue WS messages with microtask batching |
| No streaming — full message at once | Implement token-by-token streaming via WS |
| Tool args shown as raw JSON | Format as human-readable key-value pairs |

### 11.2 New Frontend Features

| Feature | Description |
|---------|-------------|
| **Markdown rendering** | Render assistant messages as markdown (tables, lists, bold) |
| **Copy button** | One-click copy for assistant responses |
| **Message timestamps** | Show relative time (2 min ago) on each message |
| **Keyboard shortcuts** | Cmd+Enter to send, Escape to clear |
| **Mobile responsive** | Collapsible sidebar, bottom tab bar for modules |
| **Dark/Light theme** | Toggle with system preference detection |
| **Loading skeletons** | Skeleton UI instead of spinners |
| **Error boundaries** | Graceful fallback UI for component errors |

### 11.3 Queue Completion Feedback

**Backend:** Worker sends WS notification on job completion:

```python
# In worker.py, after process_job():
# Publish completion status to a Redis pub/sub channel
await redis_client.publish(
    f"job_status:{payload.session_id}",
    json.dumps({
        "type": "queue_update",
        "job_id": job_id,
        "status": "completed" if result_ok else "failed",
        "tool_name": tool,
        "ricefw_id": args.get("ricefw_id", ""),
        "error": error_msg
    })
)
```

**Backend:** Chat WebSocket subscribes to Redis pub/sub for the session:

```python
# In chat.py ws handler, start a background listener:
async def listen_for_queue_updates(ws, session_id):
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(f"job_status:{session_id}")
    async for message in pubsub.listen():
        if message["type"] == "message":
            await ws.send_json(json.loads(message["data"]))
```

### 11.4 Recharts → Enhanced Visualizations

Add new chart components for admin pages:
- **Pie/Donut charts** for module distribution
- **Radar chart** for data quality scores
- **Gantt-style timeline** for project milestones
- **Treemap** for field coverage visualization

---

## 9. Phase 12: Production Hardening & Observability

> **Goal:** Make the system production-grade with structured logging, health monitoring, and database migrations.

### 12.1 Alembic Migration Setup

```bash
# Initialize Alembic in backend/
alembic init alembic
# Configure env.py to use async engine
# Generate initial migration from existing models
alembic revision --autogenerate -m "initial schema"
```

**Migration workflow:**
1. Modify SQLAlchemy model
2. `alembic revision --autogenerate -m "description"`
3. Review generated migration
4. `alembic upgrade head` (runs automatically in Docker entrypoint)

### 12.2 Structured Logging

Replace `print()` and basic `logging` with `structlog`:

```python
import structlog

logger = structlog.get_logger()

# Every log entry includes:
# - timestamp, level, event
# - user_email, session_id (from context)
# - tool_name, spreadsheet_id (from operation context)

logger.info("tool_dispatched", tool="update_cell", ricefw_id="SD-045", user="admin@tmcltd.com")
```

### 12.3 Health Check Enhancements

Expand `GET /api/health`:

```json
{
  "status": "healthy",
  "version": "2.1.0",
  "services": {
    "database": { "status": "ok", "latency_ms": 2 },
    "redis": { "status": "ok", "queue_depth": 3 },
    "llm": { "status": "ok", "model": "deepseek-chat" },
    "sheets_api": { "status": "ok" }
  },
  "uptime_seconds": 86400
}
```

### 12.4 Redis Header/ID Caching

**File:** `backend/app/sheets/meta.py`, `backend/app/sheets/read.py`

Implement the originally planned caching:

```python
HEADER_CACHE_TTL = 300  # 5 minutes
ID_CACHE_TTL = 60       # 1 minute

async def get_header_row_cached(service, spreadsheet_id, sheet_name, header_row_num):
    cache_key = f"headers:{spreadsheet_id}:{sheet_name}:{header_row_num}"
    cached = await redis_client.get(cache_key)
    if cached:
        return json.loads(cached)
    
    headers = await get_header_row(service, spreadsheet_id, sheet_name, header_row_num)
    await redis_client.setex(cache_key, HEADER_CACHE_TTL, json.dumps(headers))
    return headers
```

### 12.5 Rate Limiting

Add per-user rate limiting for API endpoints:

```python
from slowapi import Limiter

limiter = Limiter(key_func=get_remote_address)

@app.get("/api/projects")
@limiter.limit("30/minute")
async def get_projects():
    ...
```

### 12.6 Docker Security Hardening

- Run backend as non-root user
- Add health checks to all Docker services
- Set resource limits (memory, CPU)
- Enable PostgreSQL connection pooling (PgBouncer)

---

## 10. Verification Plan

### Automated Tests

| Phase | Test Suite | Command |
|-------|-----------|---------|
| Phase 6 | Bug fix verification | `pytest backend/tests/test_core/test_permissions.py -v` |
| Phase 7 | Schema detection integration | `pytest backend/tests/test_sheets/test_schema_detect.py -v` |
| Phase 8 | Planner unit tests | `pytest backend/tests/test_core/test_planner.py -v` |
| Phase 9 | Analytics API | `pytest backend/tests/test_api/test_analytics.py -v` |
| Phase 12 | Migration up/down | `alembic upgrade head && alembic downgrade base` |
| All | Full suite | `cd backend && pytest -v --tb=short` |
| Frontend | Build verification | `cd frontend && npm run build` |

### Manual Verification

| Step | Expected Result |
|------|----------------|
| Login with non-admin Google account | No admin button shown, no access to /admin |
| Login with `ADMIN_EMAILS` account | Admin button visible, full /admin access |
| Create project via Auto-Detect | All tabs detected, fields selectable, schema generated |
| Deselect a tab | Tab removed from schema_config, not shown in chat |
| Send chat message | WebSocket connects, agent responds, tools visualized |
| Trigger a write operation | Toast notification when queue job completes |
| Check admin dashboard | All charts render with real data |

---

## 11. Risk Mitigation

| Risk | Impact | Mitigation |
|------|--------|------------|
| LLM hallucination in schema detection | Wrong columns targeted | Admin review step before saving + visual field editor |
| Google Sheets API quota exhaustion | Users see errors | Queue throttle + Redis caching + batch operations |
| OAuth token expiry during long sessions | API calls fail | NextAuth auto-refresh + graceful re-auth prompt |
| Agentic planner infinite loops | Hung sessions | Hard 8-iteration limit + timeout per step |
| Database migration data loss | Production outage | Test migrations on staging first + automated backups |
| WebSocket connection drops | Lost messages | Auto-reconnect + message queue with retry |

---

## 12. Implementation Priority Matrix

| Priority | Phase | Deliverable | Estimated Effort | Dependencies |
|----------|-------|-------------|------------------|--------------|
| **P0** | Phase 6 | Critical bug fixes & hardcoding removal | **2-3 days** | None |
| **P0** | Phase 7.1-7.4 | Dynamic admin onboarding (core) | **3-4 days** | Phase 6 |
| **P1** | Phase 8.1-8.2 | Agentic planner (basic) | **4-5 days** | Phase 6 |
| **P1** | Phase 9.1-9.2 | Admin dashboard enhancements | **3-4 days** | Phase 6 |
| **P1** | Phase 11.3 | Queue completion feedback | **1-2 days** | Phase 6 |
| **P2** | Phase 7.3-7.5 | Visual schema editor + field API | **3-4 days** | Phase 7 core |
| **P2** | Phase 8.3-8.6 | Advanced orchestration features | **4-5 days** | Phase 8 basic |
| **P2** | Phase 9.3-9.6 | Per-project analytics + exports + alerts | **4-5 days** | Phase 9 core |
| **P2** | Phase 10 | User dashboard | **3-4 days** | Phase 6, 9 |
| **P3** | Phase 11.1-11.2 | Chat UX polish | **3-4 days** | Phase 6 |
| **P3** | Phase 11.4 | Enhanced visualizations | **2-3 days** | Phase 9 |
| **P3** | Phase 12 | Production hardening | **4-5 days** | All prior phases |

**Total estimated: ~38-48 working days (8-10 weeks)**

### Recommended Execution Order

```
Week 1-2:  Phase 6 (Bug fixes) → Phase 7 Core (Auto-detect improvements)
Week 3-4:  Phase 8 Basic (Agentic planner) → Phase 11.3 (Queue feedback)
Week 5-6:  Phase 9 Core (Admin dashboard) → Phase 7 Advanced (Visual editor)
Week 7-8:  Phase 10 (User dashboard) → Phase 8 Advanced (Orchestration)
Week 9-10: Phase 9 Advanced (Analytics) → Phase 12 (Production hardening)
Ongoing:   Phase 11 (UX polish — incremental)
```

---

## Appendix A: Environment Variables Reference

All configuration is sourced from environment variables (via `.env` file):

| Variable | Used By | Default | Description |
|----------|---------|---------|-------------|
| `DATABASE_URL` | Backend | `postgresql+asyncpg://...` | PostgreSQL connection string |
| `REDIS_URL` | Backend, Worker | `redis://localhost:6379` | Redis connection string |
| `DEEPSEEK_API_KEY` | Backend | `mock-deepseek-key` | DeepSeek API key |
| `GOOGLE_CLIENT_ID` | Backend, Frontend | `mock-google-id` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Backend, Frontend | `mock-google-secret` | Google OAuth secret |
| `JWT_SECRET` | Backend, Frontend | `mock-jwt-secret-...` | HS256 JWT signing key |
| `NEXTAUTH_SECRET` | Frontend | — | NextAuth encryption key |
| `NEXTAUTH_URL` | Frontend | `http://localhost:3000` | NextAuth base URL |
| `CORS_ORIGINS` | Backend | `*` (MUST change for prod) | Allowed CORS origins |
| `ADMIN_EMAILS` | Backend | `ruhail.rizwan@tmcltd.com` | Comma-separated admin emails |
| `DEFAULT_SPREADSHEET_ID` | Backend | `17mr...` | Default spreadsheet (legacy) |
| `DEFAULT_SHEET_TAB` | Backend | `SD` | Default tab (legacy) |
| `DEFAULT_SHEET_LABEL` | Backend | `FF Migration Tracker` | Default label (legacy) |
| `DB_PASSWORD` | Docker Compose | — | PostgreSQL password |
| `VPS_HOST` | CI/CD | — | Hetzner VPS IP |
| `VPS_USER` | CI/CD | — | SSH username |
| `VPS_SSH_KEY` | CI/CD | — | SSH private key |

## Appendix B: Database Schema Evolution Plan

### Current Schema (No migrations)

Tables created via `init_db()` → `metadata.create_all()`. Any change requires `drop_db()` + recreate.

### Planned Migrations (Phase 12)

| Migration | Description |
|-----------|-------------|
| `001_initial` | Baseline from current models |
| `002_user_preferences` | Add `preferences` JSONB column to `users` table |
| `003_chat_history` | New `chat_messages` table for persistent conversations |
| `004_bookmarks` | New `bookmarks` table for pinned RICEFW IDs |
| `005_notifications` | New `notifications` table for admin alerts |
| `006_analytics_cache` | New `analytics_snapshots` table for pre-computed metrics |
