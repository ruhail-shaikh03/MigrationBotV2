import json
import time
import base64
import uuid
import pandas as pd
import streamlit as st
from googleapiclient.errors import HttpError
from google.auth.exceptions import RefreshError
from streamlit_oauth import OAuth2Component
from streamlit_cookies_controller import CookieController
from src.llm.deepseek_client import get_deepseek_client
from src.llm.tools import TOOLS, SYSTEM_PROMPT, SYSTEM_PROMPT_COMPACT, VALID_MODULES
from src.sheets.executor import SheetsExecutor
from src.sheets.column_map import resolve_column, get_column_map_json
from src.sheets.dynamic_column_mapper import ensure_column_map
from src.sheets.sheet_registry import (
    get_active_sheet, set_active_sheet, reset_to_default_sheet,
    get_allowed_sheets, register_sheet, fetch_sheet_tabs,
    parse_sheet_id, is_admin, get_default_sheet,
)
from src.permissions import ensure_permissions, get_checker
from src.audit import ensure_audit_logger, get_audit_logger

# ── Page config (must be first Streamlit call) ───────────────────────────────

st.set_page_config(
    page_title="MigrationBot",
    page_icon="🤖",
    layout="centered",
)

# ── Logo ──────────────────────────────────────────────────────────────────────
try:
    st.logo("logo.png", size="large")
except Exception:
    pass

# ── OAuth2 component (defined first — used by auth gate below) ───────────────

oauth2 = OAuth2Component(
    client_id=st.secrets["auth"]["client_id"],
    client_secret=st.secrets["auth"]["client_secret"],
    authorize_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
    token_endpoint="https://oauth2.googleapis.com/token",
)

# ── Cookie manager ────────────────────────────────────────────────────────────
# CookieController is async under the hood. On the very first render after a
# tab refresh it returns None even when the cookie exists — the st.rerun()
# below gives it a second pass to actually hydrate the value.

cookie_manager = CookieController()

if "token_dict" not in st.session_state:
    saved = cookie_manager.get("mb_auth_token")
    if saved:
        st.session_state.token_dict    = saved
        st.session_state.token_issued_at = time.time()  # treat restored token as fresh
        st.rerun()

# ── Auth gate ─────────────────────────────────────────────────────────────────

if "token_dict" not in st.session_state:
    st.title("🤖 MigrationBot")
    st.markdown(
        "Your AI assistant for the **S/4HANA WRICEF Migration Control Sheet**. "
        "Read, update, search, and report — all in plain English."
    )
    result = oauth2.authorize_button(
        name="🔑 Sign in with Google",
        icon="https://www.google.com/favicon.ico",
        redirect_uri=st.secrets["auth"]["redirect_uri"],
        scope="openid email profile https://www.googleapis.com/auth/spreadsheets",
        key="google_auth",
        extras_params={"prompt": "consent", "access_type": "offline"},
    )
    if result and "token" in result:
        st.session_state.token_dict      = result["token"]
        st.session_state.token_issued_at = time.time()
        cookie_manager.set("mb_auth_token", result["token"], max_age=86400)  # 24 h
        st.rerun()
    else:
        st.stop()

token_dict = st.session_state.token_dict

# ── Token expiry guard ────────────────────────────────────────────────────────
# Proactive logout after 55 min (5 min before Google's 3600 s limit).
# The cookie keeps users logged in across tab refreshes, but we still
# enforce a session time limit to avoid stale tokens causing silent 401s.

TOKEN_LIFETIME_SECS = 55 * 60

if "token_issued_at" not in st.session_state:
    st.session_state.token_issued_at = time.time()

if time.time() - st.session_state.token_issued_at > TOKEN_LIFETIME_SECS:
    st.warning("Your session has expired. Please sign in again.")
    cookie_manager.remove("mb_auth_token")
    st.session_state.clear()
    st.rerun()

# ── Decode user info from the JWT id_token ────────────────────────────────────

def _decode_jwt_payload(token_dict: dict) -> dict:
    try:
        id_token = token_dict.get("id_token", "")
        payload  = id_token.split(".")[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return {}

_jwt        = _decode_jwt_payload(token_dict)
user_email  = _jwt.get("email",      "unknown@unknown.com").lower()
user_name   = _jwt.get("given_name", "there")

# ── Session ID (for audit log — F15) ─────────────────────────────────────────

if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())

# ── Active sheet (F12) ────────────────────────────────────────────────────────

active_sheet = get_active_sheet()

# ── Executor — cached, rebuilt on token or sheet change ──────────────────────

_executor_key = (
    token_dict.get("access_token", ""),
    active_sheet["spreadsheet_id"],
    active_sheet["sheet_tab_name"],
)

if st.session_state.get("executor_key") != _executor_key:
    try:
        st.session_state.executor     = SheetsExecutor(
            token_dict,
            spreadsheet_id=active_sheet["spreadsheet_id"],
            sheet_tab_name=active_sheet["sheet_tab_name"],
        )
        st.session_state.executor_key = _executor_key
    except Exception as e:
        st.error(f"Could not connect to Google Sheets: {e}")
        st.stop()

executor: SheetsExecutor = st.session_state.executor

# ── F11: Dynamic column map ───────────────────────────────────────────────────

try:
    ensure_column_map(executor)
except (HttpError, RefreshError) as e:
    st.error(f"Sheet access error during column analysis: {e}")
    cookie_manager.remove("mb_auth_token")
    st.session_state.clear()
    st.rerun()

# ── F13: Permissions ──────────────────────────────────────────────────────────

ensure_permissions(token_dict, user_email)

# ── F15: Audit logger ────────────────────────────────────────────────────────
ensure_audit_logger(token_dict)

# Store user_email in session_state so audit.py can read it without importing
st.session_state['user_email'] = user_email




# ── Orchestration ─────────────────────────────────────────────────────────────
#
# F14 — Agentic tool loop
#
# _handle() now loops until DeepSeek stops calling tools (task complete)
# or MAX_ITERATIONS is hit (safety cap).
#
# Model strategy per the TDD:
#   - deepseek-chat  for tool selection (no DSML leakage, reliable tool_calls)
#   - deepseek-chat  for final response composition (fast streaming)
#   - deepseek-reasoner is reserved for complex conditional reasoning:
#     injected only on iteration 0 when the query contains conditional keywords
#     ("if", "only if", "check first", "depending on") — these benefit from
#     chain-of-thought. All other iterations use deepseek-chat.
#
# F15 — Audit logging
#   _dispatch_tool calls get_audit_logger().log_*() after every write operation.
#   Old values are fetched before writes so before/after is recorded.

MAX_ITERATIONS = 8

_CONDITIONAL_KEYWORDS = ("if ", "only if", "check first", "depending on",
                          "provided that", "unless", "when ")

_TOOL_LABELS = {
    "get_row":     "Reading row…",
    "update_cell": "Updating cell…",
    "format_row":  "Formatting row…",
    "add_row":     "Adding row…",
    "bulk_update": "Running bulk update…",
    "search_rows": "Searching sheet…",
    "summarize":   "Running report…",
}

_ITERATION_LABELS = [
    "Analysing your request…",
    "Following up…",
    "Checking results…",
    "Continuing…",
    "Almost there…",
]


def _stream_chunks(stream):
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content


def _pick_model(iteration: int, messages: list) -> str:
    """
    Use deepseek-reasoner only on iteration 0 for conditional queries.
    All other calls use deepseek-chat to avoid DSML leakage.
    """
    if iteration == 0:
        user_text = " ".join(
            m.get("content", "") for m in messages if m.get("role") == "user"
        ).lower()
        if any(kw in user_text for kw in _CONDITIONAL_KEYWORDS):
            return "deepseek-reasoner"
    return "deepseek-chat"


def _build_system_msg(iteration: int) -> dict:
    """
    Full system prompt (with column map) on iteration 0.
    Compact prompt on subsequent iterations — column map is already in context.
    """
    if iteration == 0:
        return {
            "role": "system",
            "content": SYSTEM_PROMPT.format(
                valid_modules=VALID_MODULES,
                column_map_json=get_column_map_json(),
            ),
        }
    return {
        "role": "system",
        "content": SYSTEM_PROMPT_COMPACT.format(valid_modules=VALID_MODULES),
    }


def _handle(messages: list, executor: SheetsExecutor) -> str:
    client    = get_deepseek_client()
    iteration = 0

    # Replace system message with iteration-appropriate version
    non_system = [m for m in messages if m.get("role") != "system"]

    while iteration < MAX_ITERATIONS:
        current_messages = [_build_system_msg(iteration), *non_system]
        model  = _pick_model(iteration, current_messages)
        label  = _ITERATION_LABELS[min(iteration, len(_ITERATION_LABELS) - 1)]

        with st.spinner(label):
            response = client.chat.completions.create(
                model=model,
                messages=current_messages,
                tools=TOOLS,
                tool_choice="auto",
                max_tokens=1024,
            )
        msg = response.choices[0].message
        iteration += 1

        # Guard against DSML leakage from deepseek-reasoner
        if not msg.tool_calls and msg.content and "<｜｜DSML｜｜" in msg.content:
            # Reasoner leaked tool markup as text — retry with chat model
            with st.spinner("Retrying with chat model…"):
                response = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=current_messages,
                    tools=TOOLS,
                    tool_choice="auto",
                    max_tokens=1024,
                )
            msg = response.choices[0].message

        # No tool call → DeepSeek is done; stream the final reply
        if not msg.tool_calls:
            stream = client.chat.completions.create(
                model="deepseek-chat",
                messages=current_messages,
                stream=True,
                max_tokens=256,
            )
            return st.write_stream(_stream_chunks(stream))

        # Execute all tool calls for this iteration
        tool_results = []
        for tc in msg.tool_calls:
            tool_label = _TOOL_LABELS.get(tc.function.name, f"Running {tc.function.name}…")
            args       = json.loads(tc.function.arguments)
            with st.spinner(tool_label):
                result = _dispatch_tool(tc.function.name, args, executor)
            tool_results.append({
                "tool_call_id": tc.id,
                "role":         "tool",
                "content":      json.dumps(result),
            })

        # Append this iteration's exchange to the running message list
        non_system = [*non_system, msg.model_dump(), *tool_results]

    # Safety cap reached — ask DeepSeek to summarise what was completed
    stream = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            _build_system_msg(1),   # compact prompt
            *non_system,
            {
                "role":    "user",
                "content": (
                    "You have reached the maximum number of steps. "
                    "Summarise what you completed and what could not be finished."
                ),
            },
        ],
        stream=True,
        max_tokens=256,
    )
    return st.write_stream(_stream_chunks(stream))


def _dispatch_tool(name: str, args: dict, executor: SheetsExecutor) -> dict:
    # ── F13: permission check ─────────────────────────────────────────────────
    checker = get_checker()
    if checker:
        allowed, reason = checker.can_execute(name, args)
        if not allowed:
            return {"ok": False, "error": reason}

    audit = get_audit_logger()

    try:
        # ── get_row ───────────────────────────────────────────────────────────
        if name == "get_row":
            return executor.get_row(**args)

        # ── update_cell ───────────────────────────────────────────────────────
        if name == "update_cell":
            results = []
            for upd in args.get("updates", []):
                col = resolve_column(upd["field"]) or upd["field"]

                # F15: capture old value before writing
                old_val = ""
                if audit:
                    try:
                        row_data = executor.get_row(args["ricefw_id"], fields=[col])
                        old_val  = row_data.get("data", {}).get(col, "")
                    except Exception:
                        pass

                result = executor.update_cell(args["ricefw_id"], col, upd["value"])
                results.append(result)

                if audit:
                    audit.log_update_cell(
                        spreadsheet_id = executor.spreadsheet_id,
                        sheet_tab      = executor.SHEET_NAME,
                        ricefw_id      = args["ricefw_id"],
                        field          = col,
                        old_value      = old_val,
                        new_value      = upd["value"],
                        result         = result,
                    )
            return {"updates": results}

        # ── format_row ────────────────────────────────────────────────────────
        if name == "format_row":
            result = executor.format_row(**args)
            if audit:
                audit.log_format_row(executor.spreadsheet_id, executor.SHEET_NAME,
                                     args, result)
            return result

        # ── add_row ───────────────────────────────────────────────────────────
        if name == "add_row":
            next_id = executor.next_ricefw_id(args["module"])
            result  = executor.add_row(next_id, **args)
            if audit:
                audit.log_add_row(executor.spreadsheet_id, executor.SHEET_NAME,
                                  next_id, args, result)
            return result

        # ── bulk_update ───────────────────────────────────────────────────────
        if name == "bulk_update":
            args["set_field"] = resolve_column(args["set_field"]) or args["set_field"]
            if args.get("filter_by") and args["filter_by"].get("field"):
                args["filter_by"]["field"] = (
                    resolve_column(args["filter_by"]["field"])
                    or args["filter_by"]["field"]
                )
            result = executor.bulk_update(**args)
            if audit:
                audit.log_bulk_update(executor.spreadsheet_id, executor.SHEET_NAME,
                                      args, result)
            return result

        # ── search_rows ───────────────────────────────────────────────────────
        if name == "search_rows":
            return executor.search_rows(**args)

        # ── summarize ─────────────────────────────────────────────────────────
        if name == "summarize":
            return executor.summarize(**args)

        return {"ok": False, "error": f"Unknown tool: {name}"}

    except Exception as ex:
        return {"ok": False, "error": str(ex)}


# ── Sidebar report rendering ───────────────────────────────────────────────────

def _run_sidebar_report(report_type: str, scope_module: str | None,
                        executor: SheetsExecutor) -> None:
    if report_type == "Count by Dev Status":
        _render_count_report(executor.summarize(
            "count_by_field", group_by_field="Dev Status", scope_module=scope_module))
    elif report_type == "Count by Module":
        _render_count_report(executor.summarize("count_by_field", group_by_field="Module"))
    elif report_type == "Count by Assigned To":
        _render_count_report(executor.summarize(
            "count_by_field", group_by_field="Assigned To", scope_module=scope_module))
    elif report_type == "Completion Rate (Migrate?)":
        _render_completion_report(executor.summarize(
            "completion_rate", completion_field="Migrate?",
            completion_value="Yes", scope_module=scope_module))
    elif report_type == "Blank Dev Status":
        _render_blank_report(executor.summarize(
            "blank_fields", blank_field="Dev Status", scope_module=scope_module))
    elif report_type == "Overdue Items":
        _render_overdue_report(executor.summarize("overdue", scope_module=scope_module))


def _render_count_report(data: dict) -> None:
    if not data.get("ok"):
        st.error(data.get("error", "Unknown error"))
        return
    st.caption(f"**{data['field']}** — {data['scope']} — {data['total_rows']} rows")
    rows = data.get("breakdown", [])
    if rows:
        df = pd.DataFrame(rows)
        df.columns = [data["field"], "Count"]
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("No data.")


def _render_completion_report(data: dict) -> None:
    if not data.get("ok"):
        st.error(data.get("error", "Unknown error"))
        return
    pct = data["completion_pct"]
    st.metric(
        label=f"{data['field']} = '{data['target_value']}'",
        value=f"{pct}%",
        delta=f"{data['completed']} of {data['total_rows']} rows",
    )
    st.progress(int(pct))


def _render_blank_report(data: dict) -> None:
    if not data.get("ok"):
        st.error(data.get("error", "Unknown error"))
        return
    st.metric(
        label=f"Blank '{data['field']}'",
        value=f"{data['blank_count']} rows",
        delta=f"{data['blank_pct']}% of {data['total_rows']}",
    )
    if data["ids"]:
        st.caption("IDs missing this field:")
        st.code(", ".join(data["ids"][:20]))


def _render_overdue_report(data: dict) -> None:
    if not data.get("ok"):
        st.error(data.get("error", "Unknown error"))
        return
    st.metric(
        label="Overdue items",
        value=data["overdue_count"],
        delta=f"of {data['total_rows']} in scope",
    )
    items = data.get("items", [])
    if items:
        df = pd.DataFrame(items).rename(columns={
            "id":           "RICEFW ID",
            "go_live_date": "Go-Live",
            "dev_status":   "Status",
            "days_overdue": "Days Late",
        })
        st.dataframe(
            df.sort_values("Days Late", ascending=False),
            use_container_width=True,
            hide_index=True,
        )



# ── Chat history ──────────────────────────────────────────────────────────────

if "messages" not in st.session_state:
    st.session_state.messages = []

# ── Sidebar ───────────────────────────────────────────────────────────────────

def _do_logout():
    cookie_manager.remove("mb_auth_token")
    st.session_state.clear()

with st.sidebar:
    st.success("✅ Authenticated")
    st.markdown(f"**Connected as:**  \n{user_email}")
    st.button("Sign out", on_click=_do_logout)

    # ── F12: Sheet selector ───────────────────────────────────────────────────
    st.divider()
    st.subheader("📋 Active Sheet")

    allowed     = get_allowed_sheets()
    default     = get_default_sheet()
    sheet_names = [s["name"] for s in allowed] if allowed else [default.get("sheet_label", "Default")]
    current_id  = active_sheet["spreadsheet_id"]
    current_idx = next(
        (i for i, s in enumerate(allowed) if s["spreadsheet_id"] == current_id), 0
    ) if allowed else 0

    if allowed and len(allowed) > 1:
        selected_name = st.selectbox(
            "Sheet", options=sheet_names, index=current_idx,
            label_visibility="collapsed",
        )
        selected = next(s for s in allowed if s["name"] == selected_name)

        if st.button("Switch Sheet", use_container_width=True):
            with st.spinner("Connecting to sheet…"):
                try:
                    tabs = fetch_sheet_tabs(token_dict, selected["spreadsheet_id"])
                    st.session_state["pending_sheet"] = {
                        "spreadsheet_id": selected["spreadsheet_id"],
                        "name":           selected["name"],
                        "tabs":           [t["title"] for t in tabs],
                    }
                except PermissionError as e:
                    st.error(str(e))
                except Exception as e:
                    st.error(f"Could not connect: {e}")

    if "pending_sheet" in st.session_state:
        pending    = st.session_state["pending_sheet"]
        tab_choice = st.selectbox("Select tab", options=pending["tabs"], key="tab_picker")
        c1, c2 = st.columns(2)
        if c1.button("✅ Confirm", use_container_width=True):
            set_active_sheet(pending["spreadsheet_id"], tab_choice, pending["name"])
            st.session_state.pop("pending_sheet", None)
            st.session_state.messages = []
            st.rerun()
        if c2.button("Cancel", use_container_width=True):
            st.session_state.pop("pending_sheet", None)
            st.rerun()

    if is_admin(user_email):
        with st.expander("➕ Register new sheet"):
            new_url  = st.text_input("Google Sheet URL", placeholder="https://docs.google.com/spreadsheets/d/…")
            reg_name = st.text_input("Display name",     placeholder="e.g. Q2 Tracker")
            if st.button("Register", use_container_width=True):
                sid = parse_sheet_id(new_url)
                if not sid:
                    st.error("Could not parse a spreadsheet ID from that URL.")
                elif not reg_name.strip():
                    st.error("Please provide a display name.")
                else:
                    with st.spinner("Verifying access…"):
                        try:
                            fetch_sheet_tabs(token_dict, sid)
                            register_sheet(reg_name.strip(), sid)
                            st.success(f"Registered: {reg_name}")
                            st.rerun()
                        except PermissionError as e:
                            st.error(str(e))
                        except Exception as e:
                            st.error(f"Registration failed: {e}")

    st.caption(f"Active: **{active_sheet.get('sheet_label', active_sheet['sheet_tab_name'])}**")
    if active_sheet["spreadsheet_id"] != default["spreadsheet_id"]:
        if st.button("↩ Back to default sheet", use_container_width=True):
            reset_to_default_sheet()
            st.session_state.messages = []
            st.rerun()

    # ── Admin link ────────────────────────────────────────────────────────────
    if is_admin(user_email):
        st.divider()
        st.page_link("pages/admin.py", label="🔧 Admin panel", icon="🔧")

    # ── Quick Reports ─────────────────────────────────────────────────────────
    st.divider()
    st.subheader("📊 Quick Reports")

    report_type = st.selectbox(
        "Report type",
        options=[
            "Count by Dev Status",
            "Count by Module",
            "Count by Assigned To",
            "Completion Rate (Migrate?)",
            "Blank Dev Status",
            "Overdue Items",
        ],
        label_visibility="collapsed",
    )
    scope = st.selectbox(
        "Module scope",
        options=["All modules", "FI", "MM", "SD", "PM", "QM",
                 "PP", "TRM", "HCM", "IM", "CO", "FM", "PS"],
        label_visibility="collapsed",
    )
    scope_module = None if scope == "All modules" else scope

    if st.button("Run Report", use_container_width=True):
        with st.spinner("Fetching…"):
            try:
                _run_sidebar_report(report_type, scope_module, executor)
            except HttpError as e:
                if e.status_code == 401:
                    st.error("Session expired — please sign in again.")
                    cookie_manager.remove("mb_auth_token")
                    st.session_state.clear()
                    st.rerun()
                else:
                    st.error(f"Sheets API error: {e}")
            except Exception as e:
                st.error(f"Report error: {e}")

# ── Header ─────────────────────────────────────────────────────────────────────

st.title("🤖 MigrationBot")
st.caption("S/4HANA WRICEF Migration Tracker · powered by DeepSeek")

# ── Welcome screen ────────────────────────────────────────────────────────────

EXAMPLE_PROMPTS = [
    "What's the dev status of SD-045?",
    "Set FI-012 status to Ready for Dev",
    "Show me all MM objects with no dev status",
    "Highlight PM-023 red",
    "How many items are in each dev status?",
    "Which SD items are past their go-live date?",
    "Mark SD-010 and SD-011 as migrated",
    "What's our completion rate for Migrate? = Yes?",
]

if not st.session_state.messages:
    st.markdown(f"### Welcome back, {user_name} 👋")
    st.markdown(
        "Ask me anything about the migration tracker in plain English — "
        "I can read rows, update cells, search, bulk-edit, and run reports."
    )
    st.markdown("**Try one of these to get started:**")
    cols = st.columns(2)
    for i, prompt in enumerate(EXAMPLE_PROMPTS):
        if cols[i % 2].button(prompt, key=f"eg_{i}", use_container_width=True):
            st.session_state.prefill = prompt
            st.rerun()
    st.divider()

# ── Render chat history ────────────────────────────────────────────────────────

for msg in st.session_state.messages:
    if msg["role"] in ("user", "assistant"):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

# ── Handle input ──────────────────────────────────────────────────────────────

prefill    = st.session_state.pop("prefill", None)
user_input = st.chat_input("Ask about any WRICEF object…") or prefill

if user_input:
    with st.chat_message("user"):
        st.markdown(user_input)

    system_msg = {
        "role": "system",
        "content": SYSTEM_PROMPT.format(
            valid_modules=VALID_MODULES,
            column_map_json=get_column_map_json(),
        ),
    }
    messages = [system_msg, *st.session_state.messages[-12:],
                {"role": "user", "content": user_input}]

    with st.chat_message("assistant"):
        try:
            reply = _handle(messages, executor)
        except HttpError as e:
            if e.status_code == 401:
                st.error("Your session has expired. Signing you out…")
                cookie_manager.remove("mb_auth_token")
                st.session_state.clear()
                st.rerun()
            else:
                reply = f"⚠️ Sheets API error ({e.status_code}): {e.reason}"
                st.markdown(reply)
        except RefreshError:
            st.error("Google token expired. Signing you out…")
            cookie_manager.remove("mb_auth_token")
            st.session_state.clear()
            st.rerun()

    st.session_state.messages.append({"role": "user",      "content": user_input})
    st.session_state.messages.append({"role": "assistant", "content": reply})
    st.rerun()
