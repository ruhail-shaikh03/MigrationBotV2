# Gemini Context: MigrationBot Development Guide

Welcome! Before you begin planning, developing, or debugging MigrationBot, thoroughly review this context.

## 1. Architectural Overview
MigrationBot is a Streamlit-based conversational AI agent for interacting with S/4HANA WRICEF Google Sheets.
- **Frontend/Backend:** Streamlit (Synchronous execution only; avoid `asyncio`).
- **LLM:** DeepSeek via OpenAI-compatible endpoints (`deepseek-chat` and `deepseek-reasoner`).
- **Storage:** Google Sheets (Tracker sheets for data; Config sheet for RBAC & Audit Logs).
- **Auth:** Google OAuth2 natively requesting Sheets permissions. No service accounts.

## 2. Core Modules & Systems
- **Agentic Loop (`app.py`):** The `_handle()` loop chains tool calls (up to 8 iterations). It leverages `deepseek-reasoner` on iter 0 for conditionals, and `deepseek-chat` for all other processing to avoid DSML leakage.
- **Google Sheets Executor (`src/sheets/executor.py`):** Caches header rows, sheet IDs, and RICEFW ID layouts in session memory. **Never run single-cell fetches in loops**; use bulk reads. Relies on `DATA_START_ROW = 3`.
- **RBAC (`src/permissions.py`):** Validates permissions strictly at the tool dispatch layer inside the loop. Modifying permissions requires interacting with the config sheet.
- **Audit Logging (`src/audit.py`):** Non-blocking. Records `old_value` and `new_value` for all mutations.
- **Dynamic Columns (`src/sheets/dynamic_column_mapper.py`):** Uses LLM to alias natural language to exact, typo-ridden sheet headers.

## 3. Session State Management
If you modify UI behavior, caching, or auth, respect these `st.session_state` keys:
- **Auth:** `token_dict` (contains access/refresh tokens), `token_issued_at` (55-min guard).
- **Core:** `executor` (Sheets API wrapper), `executor_key` (cache invalidation tuple).
- **Context:** `messages` (chat log), `column_map` (alias dict), `active_sheet` (current tracker).
- **Security:** `checker` (PermissionChecker), `audit_logger` (AuditLogger).

## 4. Development Rules & Best Practices
1. **Never use Asyncio:** Streamlit and `AsyncOpenAI` conflict. Always use synchronous clients and `st.write_stream()`.
2. **Clear Caches on State Change:** If you change the active sheet, you MUST pop `executor`, `column_map`, and their associated keys from `session_state` so they rebuild on the next rerun.
3. **Respect RBAC:** If adding a new tool, define if it's a `WRITE_TOOL` or `READ_ONLY_TOOL` in `src/permissions.py`.
4. **Audit Write Tools:** If a new tool mutates data, add an audit log wrapper in `src/audit.py` capturing the `old_value` and `new_value`.
5. **No Service Accounts:** Never attempt to bypass OAuth tokens. Use `token_dict` from the session state to build `googleapiclient.discovery` services.
6. **Technical Debt to Avoid:** Do not rely on `is_admin` in `sheet_registry.py` (it is stale). Use `PermissionChecker`.

## 5. Typical Workflows
- **To add a tool:** Update `src/llm/tools.py` schema -> Add execution logic to `SheetsExecutor` -> Route in `app.py:_dispatch_tool` -> Update `permissions.py` -> Update `audit.py` -> Re-test loop.
- **To modify the UI:** Edit `app.py` or `pages/admin.py`. Rely on `st.rerun()` when explicit state sync is needed.
