import json
import time
import pandas as pd
import streamlit as st
import sys
import streamlit as st
from googleapiclient.errors import HttpError
from google.auth.exceptions import RefreshError
from src.llm.deepseek_client import get_deepseek_client
from src.llm.tools import TOOLS, SYSTEM_PROMPT, VALID_MODULES
# ADD these two lines after your existing imports
import uuid
from src.permissions import ensure_permissions, get_checker
from streamlit_oauth import OAuth2Component
from src.sheets.executor import SheetsExecutor
from src.sheets.column_map import resolve_column, get_column_map_json
from src.sheets.dynamic_column_mapper import ensure_column_map
from src.sheets.sheet_registry import (
    get_active_sheet, set_active_sheet, reset_to_default_sheet,
    get_allowed_sheets, register_sheet, fetch_sheet_tabs,
    fetch_sheet_name, parse_sheet_id, is_admin, get_default_sheet,
)
from streamlit_cookies_controller import CookieController


# ── OAuth2 component (must be defined before any use) ────────────────────────
# ── 1. Page config MUST BE FIRST ─────────────────────────────────────────────
st.set_page_config(
    page_title="MigrationBot",
    page_icon="🤖",
    layout="centered",
)

try:
    st.logo("logo.png", size="large")
except Exception:
    pass

# ── 2. Initialize Controllers ────────────────────────────────────────────────
cookie_manager = CookieController()

oauth2 = OAuth2Component(
    client_id=st.secrets["auth"]["client_id"],
    client_secret=st.secrets["auth"]["client_secret"],
    authorize_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
    token_endpoint="https://oauth2.googleapis.com/token",
)

# ── 3. Auth Gate & Cookie Restoration ────────────────────────────────────────
if "token_dict" not in st.session_state:
    # Give the browser a split-second to mount and send the cookie data back to Python
    time.sleep(0.2) 
    saved_token = cookie_manager.get("google_auth_token")

    if saved_token:
        # We found the cookie! Load it and reload the page.
        st.session_state.token_dict = saved_token
        st.rerun()
    else:
        # No cookie found. Show the login screen.
        st.title("🤖 MigrationBot")
        st.markdown(
            "Your AI assistant for the **S/4HANA WRICEF Migration Control Sheet**. "
            "Read, update, search, and report — all in plain English."
        )

        result = oauth2.authorize_button(
            name="🔑 Sign in with Google",
            icon="https://www.google.com/favicon.ico",
            redirect_uri="https://ff-wricef-migration-bot-v1.streamlit.app/",
            scope="openid email profile https://www.googleapis.com/auth/spreadsheets",
            key="google_auth",
            extras_params={"prompt": "consent", "access_type": "offline"}
        )

        if result and "token" in result:
            raw_token = result["token"]
            
            # 1. Extract name and email NOW while we have the massive id_token
            try:
                import base64
                payload = raw_token.get("id_token", "").split(".")[1]
                payload += "=" * ((4 - len(payload) % 4) % 4)
                decoded = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
                extracted_email = decoded.get("email", "Unknown User")
                extracted_name = decoded.get("given_name", "there")
            except Exception:
                extracted_email = "Unknown User"
                extracted_name = "there"

            # 2. Build a "Diet Token" that easily fits in the 4KB browser limit
            diet_token = {
                "access_token": raw_token.get("access_token"),
                "refresh_token": raw_token.get("refresh_token"),
                "token_type": raw_token.get("token_type"),
                "expires_at": raw_token.get("expires_at"),
                "user_email": extracted_email,
                "first_name": extracted_name
            }
            
            st.session_state.token_dict = diet_token
            cookie_manager.set("google_auth_token", diet_token)
            st.rerun()
        else:
            st.stop() # Halt the script here until they click the button

# If we made it here, the user is logged in!
token_dict = st.session_state.token_dict
# ── Token expiry guard ───────────────────────────────────────────────────────
# Google access tokens live ~3600 s. We log the user out after 55 min
# (5 min buffer) so the token never silently starts returning 401s mid-session.

TOKEN_LIFETIME_SECS = 55 * 60

if "token_issued_at" not in st.session_state:
    st.session_state.token_issued_at = time.time()

if time.time() - st.session_state.token_issued_at > TOKEN_LIFETIME_SECS:
    st.warning("Your session has expired. Please sign in again.")
    st.session_state.clear()
    st.logout()
    st.stop()

#token_dict = st.user.tokens["access"]
# ADD THIS LINE:
token_dict = st.session_state.token_dict

# ── Active sheet ─────────────────────────────────────────────────────────────
# F12: the active sheet is stored in session_state["active_sheet"].
# Defaults to the sheet in secrets.toml. Admins can switch at runtime.

active_sheet = get_active_sheet()

# ── Executor — cached so header/row caches survive reruns ────────────────────
# Rebuilt when the access token OR the active sheet changes.

_executor_key = (token_dict, active_sheet["spreadsheet_id"], active_sheet["sheet_tab_name"])

if st.session_state.get("executor_key") != _executor_key:
    st.session_state.executor     = SheetsExecutor(
        # access_token,
        token_dict, # <-- Pass the whole dictionary
        spreadsheet_id = active_sheet["spreadsheet_id"],
        sheet_tab_name = active_sheet["sheet_tab_name"],
    )
    st.session_state.executor_key = _executor_key

executor: SheetsExecutor = st.session_state.executor

# ── F11: Dynamic column map ──────────────────────────────────────────────────
# Build (or reuse) the LLM-generated alias map for the active sheet.
# Runs the two-pass DeepSeek analysis once per session per sheet,
# showing a spinner. Subsequent reruns are instant (cache hit).
# ── F11: Dynamic column map ──────────────────────────────────────────────────
# Build (or reuse) the LLM-generated alias map for the active sheet.
try:
    ensure_column_map(executor)
except (HttpError, RefreshError):
    st.warning("Your session has expired or lacks permissions. Please sign in again.")
    st.session_state.clear()
    st.logout()
    st.stop()



# ── Orchestration ─────────────────────────────────────────────────────────────
#
# Three-step flow, each step visible to the user:
#
#   1. deepseek-reasoner  — intent parsing + tool selection  (blocking, ~5 s)
#   2. Sheets API         — tool execution                   (blocking, varies)
#   3. deepseek-chat      — response composition             (streamed live)
#
# Step 3 uses deepseek-chat instead of deepseek-reasoner: composing a friendly
# sentence from a JSON result needs no chain-of-thought — chat is 3-5× faster.
# Streaming it means text appears word-by-word, so the user never sees a freeze.

_TOOL_LABELS = {
    "get_row":     "Reading row…",
    "update_cell": "Updating cell…",
    "format_row":  "Formatting row…",
    "add_row":     "Adding row…",
    "bulk_update": "Running bulk update…",
    "search_rows": "Searching sheet…",
    "summarize":   "Running report…",
}

def _stream_chunks(stream):
    """Yield text tokens from an OpenAI streaming response."""
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content

def _handle(messages: list, executor: SheetsExecutor) -> str:
    client = get_deepseek_client()

    # ── Step 1: intent parsing + tool selection ──────────────────────────────
    with st.spinner("Analysing your request…"):
        response = client.chat.completions.create(
            model="deepseek-reasoner",
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=1024,
        )
    msg = response.choices[0].message

    # No tool call → DeepSeek is asking for clarification; stream it directly
    if not msg.tool_calls:
        stream = client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            stream=True,
            max_tokens=256,
        )
        return st.write_stream(_stream_chunks(stream))

    # ── Step 2: tool execution ───────────────────────────────────────────────
    tool_results = []
    for tc in msg.tool_calls:
        label = _TOOL_LABELS.get(tc.function.name, f"Running {tc.function.name}…")
        args  = json.loads(tc.function.arguments)
        with st.spinner(label):
            result = _dispatch_tool(tc.function.name, args, executor)
        tool_results.append({
            "tool_call_id": tc.id,
            "role":         "tool",
            "content":      json.dumps(result),
        })

    # ── Step 3: stream the human-readable reply ──────────────────────────────
    stream = client.chat.completions.create(
        model="deepseek-chat",          # ← no reasoning needed here
        messages=[*messages, msg.model_dump(), *tool_results],
        stream=True,
        max_tokens=256,
    )
    return st.write_stream(_stream_chunks(stream))


def _dispatch_tool(name: str, args: dict, executor: SheetsExecutor) -> dict:
    checker = get_checker()
    if checker:
        allowed, reason = checker.can_execute(name, args)
        if not allowed:
            return {"ok": False, "error": reason}
    try:
        if name == "get_row":
            return executor.get_row(**args)

        if name == "update_cell":
            results = []
            for upd in args.get("updates", []):
                col = resolve_column(upd["field"]) or upd["field"]
                results.append(
                    executor.update_cell(args["ricefw_id"], col, upd["value"])
                )
            return {"updates": results}

        if name == "format_row":
            return executor.format_row(**args)

        if name == "add_row":
            next_id = executor.next_ricefw_id(args["module"])
            return executor.add_row(next_id, **args)

        if name == "bulk_update":
            args["set_field"] = resolve_column(args["set_field"]) or args["set_field"]
            if args.get("filter_by") and args["filter_by"].get("field"):
                args["filter_by"]["field"] = (
                    resolve_column(args["filter_by"]["field"])
                    or args["filter_by"]["field"]
                )
            return executor.bulk_update(**args)

        if name == "search_rows":
            return executor.search_rows(**args)

        if name == "summarize":
            return executor.summarize(**args)

        return {"ok": False, "error": f"Unknown tool: {name}"}

    except Exception as ex:
        return {"ok": False, "error": str(ex)}


# ── Sidebar report rendering ──────────────────────────────────────────────────

def _run_sidebar_report(report_type: str, scope_module: str | None,
                        executor: SheetsExecutor) -> None:
    if report_type == "Count by Dev Status":
        _render_count_report(executor.summarize(
            "count_by_field", group_by_field="Dev Status", scope_module=scope_module
        ))
    elif report_type == "Count by Module":
        _render_count_report(executor.summarize(
            "count_by_field", group_by_field="Module"
        ))
    elif report_type == "Count by Assigned To":
        _render_count_report(executor.summarize(
            "count_by_field", group_by_field="Assigned To", scope_module=scope_module
        ))
    elif report_type == "Completion Rate (Migrate?)":
        _render_completion_report(executor.summarize(
            "completion_rate", completion_field="Migrate?",
            completion_value="Yes", scope_module=scope_module
        ))
    elif report_type == "Blank Dev Status":
        _render_blank_report(executor.summarize(
            "blank_fields", blank_field="Dev Status", scope_module=scope_module
        ))
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


# ── Chat history ─────────────────────────────────────────────────────────────

if "messages" not in st.session_state:
    st.session_state.messages = []

# ── Sidebar ───────────────────────────────────────────────────────────────────

# with st.sidebar:
#     st.success("✅ Authenticated")
#     st.markdown(f"**Connected as:**  \n{st.user.email}")
#     st.button("Sign out", on_click=st.logout)
import base64

# --- Helper to decode the email from Google's ID token ---
user_email = st.session_state.token_dict.get("user_email", "Unknown User")


if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())

ensure_permissions(token_dict, user_email)

with st.sidebar:
    st.success("✅ Authenticated")
    st.markdown(f"**Connected as:**  \n{user_email}")
    
    def do_logout():
        st.session_state.clear()
        
    st.button("Sign out", on_click=do_logout)

    # ── F12: Sheet selector ───────────────────────────────────────────────────
    st.divider()
    st.subheader("📋 Active Sheet")

    allowed = get_allowed_sheets()
    default = get_default_sheet()
    sheet_names = [s["name"] for s in allowed] if allowed else [default["sheet_label"]]

    # Find which sheet is currently active
    current_id = active_sheet["spreadsheet_id"]
    current_idx = next(
        (i for i, s in enumerate(allowed) if s["spreadsheet_id"] == current_id), 0
    ) if allowed else 0

    if allowed and len(allowed) > 1:
        selected_name = st.selectbox(
            "Sheet", options=sheet_names, index=current_idx,
            label_visibility="collapsed"
        )
        selected = next(s for s in allowed if s["name"] == selected_name)

        # Fetch tabs for the selected sheet
        if st.button("Switch Sheet", use_container_width=True):
            with st.spinner("Connecting to sheet…"):
                try:
                    tabs = fetch_sheet_tabs(token_dict, selected["spreadsheet_id"])
                    st.session_state["pending_sheet"] = {
                        "spreadsheet_id": selected["spreadsheet_id"],
                        "name": selected["name"],
                        "tabs": [t["title"] for t in tabs],
                    }
                except PermissionError as e:
                    st.error(str(e))
                except Exception as e:
                    st.error(f"Could not connect: {e}")

    # Tab picker — shown after "Switch Sheet" is clicked
    if "pending_sheet" in st.session_state:
        pending = st.session_state["pending_sheet"]
        tab_choice = st.selectbox(
            "Select tab", options=pending["tabs"], key="tab_picker"
        )
        col1, col2 = st.columns(2)
        if col1.button("✅ Confirm", use_container_width=True):
            set_active_sheet(
                pending["spreadsheet_id"], tab_choice, pending["name"]
            )
            st.session_state.pop("pending_sheet", None)
            st.session_state.messages = []   # clear chat for new sheet context
            st.rerun()
        if col2.button("Cancel", use_container_width=True):
            st.session_state.pop("pending_sheet", None)
            st.rerun()

    # Admin: register a new sheet by URL
    if is_admin(user_email):
        with st.expander("➕ Register new sheet (Admin)"):
            new_url = st.text_input("Google Sheet URL", placeholder="https://docs.google.com/spreadsheets/d/…")
            reg_name = st.text_input("Display name", placeholder="e.g. Q2 Tracker")
            if st.button("Register", use_container_width=True):
                sid = parse_sheet_id(new_url)
                if not sid:
                    st.error("Could not parse a spreadsheet ID from that URL.")
                elif not reg_name.strip():
                    st.error("Please provide a display name.")
                else:
                    with st.spinner("Verifying access…"):
                        try:
                            fetch_sheet_tabs(token_dict, sid)   # access check
                            register_sheet(reg_name.strip(), sid)
                            st.success(f"Registered: {reg_name}")
                            st.rerun()
                        except PermissionError as e:
                            st.error(str(e))
                        except Exception as e:
                            st.error(f"Registration failed: {e}")

    # Show current active sheet label
    st.caption(f"Active: **{active_sheet.get('sheet_label', active_sheet['sheet_tab_name'])}**")
    if active_sheet["spreadsheet_id"] != default["spreadsheet_id"]:
        if st.button("↩ Back to default sheet", use_container_width=True):
            reset_to_default_sheet()
            st.session_state.messages = []
            st.rerun()

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
                    st.session_state.clear()
                    st.logout()
                else:
                    st.error(f"Sheets API error: {e}")
            except Exception as e:
                st.error(f"Report error: {e}")

# ── Header ────────────────────────────────────────────────────────────────────

st.title("🤖 MigrationBot")
st.caption("S/4HANA WRICEF Migration Tracker · powered by DeepSeek")

# ── Welcome screen (shown only before the first message) ─────────────────────

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
    #first_name = (st.user.name or "").split()[0] or "there"
    # Extract first name securely from the Google token
    first_name = st.session_state.token_dict.get("first_name", "there")
    st.markdown(f"### Welcome back, {first_name} 👋")
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

# ── Render existing chat history ──────────────────────────────────────────────

for msg in st.session_state.messages:
    if msg["role"] in ("user", "assistant"):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

# ── Handle new input (typed OR from welcome screen example button) ────────────

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
    history  = st.session_state.messages[-12:]
    messages = [system_msg, *history, {"role": "user", "content": user_input}]

    with st.chat_message("assistant"):
        try:
            # _handle streams the final response live via st.write_stream —
            # no outer spinner or st.markdown needed here.
            reply = _handle(messages, executor)
        except HttpError as e:
            if e.status_code == 401:
                st.error("Your session has expired. Signing you out…")
                st.session_state.clear()
                st.logout()
                st.stop()
            else:
                reply = f"⚠️ Sheets API error ({e.status_code}): {e.reason}"
                st.markdown(reply)

    st.session_state.messages.append({"role": "user",      "content": user_input})
    st.session_state.messages.append({"role": "assistant", "content": reply})
    st.rerun()

