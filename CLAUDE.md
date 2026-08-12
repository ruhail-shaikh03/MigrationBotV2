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
| Data | PostgreSQL 16 (RBAC, audit, **and a `sheet_records` read cache**) |
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

**Reads come from Postgres, not Sheets.** `read.py:_ensure_sheet_synced()` serves
`get_row`/`search_rows`/`summarize`/`data_quality` from the `sheet_records` table
(`models/sheet_record.py`), repopulated by `sheets/sync.py:sync_sheet_to_db()` on miss and
after writes. If a read looks stale, suspect the cache before the Sheets API.

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
- **Tool failures are classified, not stringified.** `core/errors.py:classify_error` assigns an
  `error_kind` and a user-safe `user_message`; `failure_note()` picks the guidance the model sees.
  Returning a bare `{"ok": False, "error": str(e)}` from a new tool regresses this — the model
  will retry outages as if they were bad arguments (TDD §8.2).
- **Sheets rate limits** hit the write path hardest (worker, 1/sec) and the cache-fill path
  (`sync_sheet_to_db` scans up to 2000 rows). `_with_retry()` backs off on 429/500/503,
  max 4 attempts.
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
