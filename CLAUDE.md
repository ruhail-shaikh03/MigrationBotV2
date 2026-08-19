# MigrationBot Enterprise Portal

Conversational AI over S/4HANA WRICEF Migration Tracker Google Sheets. SAP team members ask
questions and issue mutations in natural language; an agentic LLM loop translates them into
tool calls against a Sheets-backed dataset.

**`TDD.md` is the authoritative architecture doc.** `implementation.md` holds the build plan.
`_legacy/` is a dead Streamlit v3.0 prototype — reference only, never a source of truth.

## Stack

| Layer | Tech |
|---|---|
| Frontend | Next.js 16 App Router, React 19, Tailwind v4, Zustand 5, NextAuth v5 |
| Backend | FastAPI (async), SQLAlchemy 2.0 + asyncpg, Pydantic v2 |
| Data | PostgreSQL 16 (RBAC, audit, projects, sessions, cached sheet rows), Alembic |
| Queue | Redis 7 (RPUSH/BLPOP FIFO, 1 req/sec throttle) |
| External | Google Sheets API v4, DeepSeek (`deepseek-chat` / `deepseek-reasoner`) via `AsyncOpenAI` |

**Frontend work: read `frontend/CLAUDE.md` → `frontend/AGENTS.md` first.** Next.js 16 has
breaking changes vs. training data; consult `node_modules/next/dist/docs/` before writing
frontend code.

## Commands

```bash
# Backend API (from backend/ — pyproject sets pythonpath=["."])
cd backend && uvicorn app.main:app --reload --port 8000

# Queue worker — REQUIRED for any write to reach Sheets
cd backend && python -m app.queue.worker

# Schema migrations — the backend container runs this at startup; run by hand only to
# inspect. `alembic check` diffs the migrated schema against app/models and is what CI
# uses to prove they agree. Needs a live Postgres.
cd backend && alembic upgrade head && alembic check

# Tests — must run from backend/, not repo root
cd backend && pytest -v --tb=short

# Frontend
cd frontend && npm run dev      # also: build, lint, start

# Full stack (postgres, redis, backend, worker, frontend, caddy)
docker compose up -d --build
```

## Strict Invariants

**Async everywhere.** `asyncpg` + `AsyncSessionLocal` + `AsyncOpenAI` + `redis.asyncio`.
Never call a sync client on the event loop. The one sanctioned bridge is
`sheets/retry.py:_with_retry()`, which dispatches the blocking `googleapiclient` call
through a module-level `ThreadPoolExecutor` via `run_in_executor` — route new Google API
calls through it rather than calling `service.…execute()` directly.

**OAuth tokens only — never service accounts.** Every Sheets call uses the signed-in user's
own Google access token, forwarded from NextAuth through the HS256 JWT. There is no service
account and none should be introduced; it would silently destroy per-user audit attribution.

**RBAC is enforced at tool dispatch, not the UI.** `PermissionChecker.can_execute()`
(`core/permissions.py`) runs in `agentic_loop.py` before every tool call. Frontend admin
gating (`GET /api/me` → `is_admin`) is cosmetic. A new tool that skips the checker is a
privilege-escalation bug.

**Writes always go through the Redis queue.** `update_cell`, `bulk_update`, `format_row`,
`add_row` enqueue via `queue/producer.py:enqueue_write_job()`; only `queue/worker.py` touches
the Sheets write API, at 1 req/sec. Never write to Sheets from a request handler — it
bypasses throttling, audit logging, and cache reconciliation.

**Never iterate rows with `get_row`.** It is a single-object lookup. For anything over one
row use `search_rows` (AND filters, 3 match types) or `summarize` (count_by_field,
completion_rate, blank_fields, overdue). Looping `get_row` is the classic way to blow the
Sheets quota and stall the agentic loop against its 8-iteration cap.

**All reads go through three tiers, and `get_tab_matrix` is the only seam.**
`sheets/rows_cache.py:get_tab_matrix()` is the sole read boundary — 7 call sites across
`read.py:_cached_matrix` (which feeds every agent tool), `api/dashboard.py`, `api/aliases.py`
and `api/digest.py`. It tries, in order: Redis (60s, `migrationbot:rows:{id}:{tab}`, collapses
one person's burst of clicks) → Postgres `sheet_records` (`sheets/records_cache.py`, no expiry)
→ a live Sheets scan. Add caching behaviour *there*, not at a call site.

**The Postgres tier is invalidated by a fact, not a timer.** `sheet_sync_state` holds an
`is_dirty` flag per `(spreadsheet_id, tab)`, set by a Drive push notification
(`api/webhooks.py`), by `queue/worker.py` after a successful write, or by a
`files.get(fields=modifiedTime)` that no longer matches. So an *unchanged* tab costs zero
Sheets calls no matter how long it stays unchanged — which is the point of the table.
Earlier revisions of this file described a `sheet_records` table and a `sheets/sync.py` that
had never been written; the table is now real, but `sheets/sync.py` still does not exist and
there is no background sync — refills happen lazily, on the next real request, with that
user's own OAuth token.

**Every cache failure degrades to a live scan, never an error.** Redis down, Postgres down,
Drive scope missing, `modifiedTime` unreadable — all mean "assume stale" and fall through.
A read must never fail because a cache did. If a *dashboard* read looks stale suspect Redis;
if an *agent* read looks stale suspect `is_dirty` not being set.

**Sheets has no cell-change webhook.** Drive `files.watch` fires per *file* and its payload
never says which cell or even which tab changed, so a notification only ever marks every tab
of that spreadsheet dirty. Anything that claims to know which cell changed is wrong.

## Code Style

- **Pydantic v2** for settings (`config.py`) and queue payloads (`queue/schemas.py:WriteJobPayload`).
- **SQLAlchemy 2.0** only: `Mapped[...]` + `mapped_column(...)` on a `DeclarativeBase`. No legacy `Column()`.
- **Tailwind v4** via `@import` in `globals.css` — no `tailwind.config.js` theme extension.
- **Zustand** (`store/useChatStore.ts`) for chat state. `activeTab` defaults to `""`, not a module name.
- Tool schemas are OpenAI function-calling dicts in `core/tool_schemas.py:TOOLS`.

## Gotchas

- **Header whitespace is real data.** Detected schemas contain trailing spaces
  (e.g. `"assignee_column": "Technical Resource "`). Never `.strip()` a header when using it
  as a lookup key — resolve through `core/column_mapper.py:resolve_column()` instead.
- **The backend JWT outlives the Google token it carries.** `auth.ts:withApiToken` sets `exp` to
  24h but embeds a `google_access_token` that expires in ~1h. It now also embeds
  `google_refresh_token`, so `sheets/client.py:build_sheets_service` self-refreshes instead of
  dying mid-connection — expect Sheets 401s only when that refresh itself fails, not FastAPI 401s.
  The minted JWT is cached on the NextAuth token and re-signed only near expiry or when the
  Google token rotates. Don't reintroduce per-call signing: it changes `session.apiToken`'s
  identity on every session read and tears down the chat WebSocket (TDD §4.1).
- **Only Caddy publishes a port.** Postgres, Redis, the backend and the frontend are reachable
  only over the compose network; `DB_PASSWORD` and `REDIS_PASSWORD` are required and compose
  aborts without them. Re-adding a `ports:` mapping puts that service on the public internet —
  Docker's iptables rules bypass UFW, which is how the 2026-08 host compromise happened (TDD §15.1).
- **A primary ID does not identify a row.** 27 of 412 rows on the reference tracker share an
  ID. Write paths resolve with `read.py:find_all_row_nums` (or `meta.py:get_id_row_map`, which
  returns `Dict[str, List[int]]`) and **refuse** on more than one match via
  `errors.py:ambiguous_id_result` — never take the first. The one caller allowed to pin a row is
  the dashboard, which passes `row_number` because the user clicked it; the worker still verifies
  that row holds the ID. A new write tool that calls `find_row_num` is the §16.7 bug again.
- **Tool failures are classified, not stringified.** `core/errors.py:classify_error` assigns an
  `error_kind` and a user-safe `user_message`; `failure_note()` picks the guidance the model sees.
  Returning a bare `{"ok": False, "error": str(e)}` from a new tool regresses this — the model
  will retry outages as if they were bad arguments (TDD §8.2).
- **Sheets rate limits** hit the write path hardest (worker, 1/sec) and any full-tab scan
  (`read.py:_fetch_all_rows` pages 2000 rows at a time up to `_MAX_SCAN_ROWS` = 20000).
  `_with_retry()` backs off on 429/500/503, max 4 attempts.
- **Default RBAC is fail-closed** (since Phase 4). `get_user_permissions()` returns
  `settings.DEFAULT_ROLE` — `"viewer"` unless a deployment overrides it — for any caller it
  can't place: no `project_id`, no matching user row, or no `permissions` row. It previously
  returned *editor with `["*"]`*, handing an unknown user full write access. See the
  `migrationbot-rbac-audit` skill before changing this.
- **Write completion round-trips as of Phase 5.** `worker.py:process_job` calls
  `events.py:publish_queue_update` on every terminal state (done, failed, dead-lettered);
  `chat.py:forward_queue_updates` relays it as a `queue_update` WebSocket frame that
  `useWebSocket.ts` turns into a DOM `CustomEvent` for `chat/page.tsx` to toast on.
  `GET /api/jobs/{job_id}` (`api/jobs.py`) also lets a client poll a job's last known
  state directly, scoped to the job's owner or an admin. (Previously fire-and-forget —
  see git history around the Phase 5 reliability PR if you need the old behavior.)
- **The worker survives restarts mid-job.** `start_worker` picks jobs up with `BLMOVE`
  into `PROCESSING_KEY` rather than `BLPOP`, so a crash between pickup and completion
  leaves the job recoverable instead of silently dropped; `recover_stale_jobs()` re-queues
  it (bounded by `MAX_ATTEMPTS`) or dead-letters it on the next worker startup.
