"""
pages/admin.py
──────────────
F13 — Admin Panel

Accessible only to users in secrets["app"]["admins"].
Shares st.session_state with the main app — no re-login needed.

Sections:
  1. User Permissions — view, add, edit, delete permission rows
  2. Cache Management — clear executor/column map caches
"""

import json
import base64
import pandas as pd
import streamlit as st
from src.permissions import (
    save_permissions, PermissionChecker, WRITE_TOOLS, READ_ONLY_TOOLS
)

st.set_page_config(page_title="MigrationBot Admin", page_icon="🔧", layout="wide")

# ── Auth check ────────────────────────────────────────────────────────────────
# Must be logged in via streamlit-oauth (token_dict in session_state)
# AND must be an admin.

if "token_dict" not in st.session_state:
    st.warning("Please sign in from the main page first.")
    st.page_link("app.py", label="← Go to MigrationBot", icon="🤖")
    st.stop()


def _get_email(token_dict: dict) -> str:
    try:
        id_token = token_dict.get("id_token", "")
        payload  = id_token.split(".")[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        decoded  = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        return decoded.get("email", "unknown").lower()
    except Exception:
        return "unknown"


user_email = _get_email(st.session_state.token_dict)
admins     = [a.lower() for a in st.secrets.get("app", {}).get("admins", [])]

if user_email not in admins:
    st.error(f"Access denied. `{user_email}` is not an admin.")
    st.page_link("app.py", label="← Go to MigrationBot", icon="🤖")
    st.stop()

# ── Page header ───────────────────────────────────────────────────────────────

st.title("🔧 MigrationBot Admin")
st.caption(f"Signed in as **{user_email}**")
st.page_link("app.py", label="← Back to MigrationBot", icon="🤖")

config_sheet_id = st.secrets.get("app", {}).get("config_sheet_id")
if not config_sheet_id:
    st.warning(
        "No `config_sheet_id` found in secrets. "
        "Add it to enable persistent permissions. "
        "Until then, only the `admins` list in secrets applies."
    )

tab1, tab2 = st.tabs(["👥 User Permissions", "🗄️ Cache Management"])

# ══════════════════════════════════════════════════════════════════════════════
# Tab 1 — User Permissions
# ══════════════════════════════════════════════════════════════════════════════

with tab1:
    st.subheader("User Permissions")
    st.markdown(
        "Define access for each user. Admins are always full-access regardless "
        "of what's here — edit the `admins` list in `secrets.toml` to change that."
    )

    if not config_sheet_id:
        st.info("Configure `config_sheet_id` in secrets to enable permission management.")
        st.stop()

    # Load current permissions from session (already fetched by ensure_permissions)
    permissions: dict = st.session_state.get("permissions_raw", {})

    # ── Role legend ───────────────────────────────────────────────────────────
    with st.expander("📖 Role reference"):
        col1, col2, col3 = st.columns(3)
        col1.markdown(
            "**admin**\n\nFull access to all tools and all fields. "
            "Set in `secrets.toml`, not in this table."
        )
        col2.markdown(
            "**editor** *(default)*\n\nCan read, update, search, bulk-edit, and report. "
            "Restrict with `allowed_fields` or `denied_operations`."
        )
        col3.markdown(
            "**viewer**\n\nRead-only: `get_row`, `search_rows`, `summarize` only. "
            "No writes of any kind."
        )
        st.markdown(
            f"**Write tools:** {', '.join(f'`{t}`' for t in sorted(WRITE_TOOLS))}  \n"
            f"**Read tools:** {', '.join(f'`{t}`' for t in sorted(READ_ONLY_TOOLS))}"
        )

    # ── Current permissions table ─────────────────────────────────────────────
    st.markdown("#### Current rules")

    if not permissions:
        st.info(
            "No rules yet. All authenticated users get the default editor policy "
            "(full write access). Add rows below to restrict access."
        )
    else:
        # Build display dataframe
        rows = []
        for email, perm in permissions.items():
            rows.append({
                "Email":              email,
                "Role":               perm["role"],
                "Allowed Fields":     "*" if perm["allowed_fields"] == ["*"]
                                      else ", ".join(perm["allowed_fields"]),
                "Denied Operations":  ", ".join(perm.get("denied_operations", [])),
            })
        df = pd.DataFrame(rows)

        # Colour rows by role
        def _row_colour(row):
            colours = {"admin": "background-color: #d4edda",
                       "viewer": "background-color: #fff3cd",
                       "editor": ""}
            c = colours.get(row["Role"], "")
            return [c] * len(row)

        st.dataframe(
            df.style.apply(_row_colour, axis=1),
            use_container_width=True,
            hide_index=True,
        )

    # ── Add / Edit a user ─────────────────────────────────────────────────────
    st.markdown("#### Add or edit a user")

    with st.form("add_edit_user", clear_on_submit=True):
        c1, c2 = st.columns([2, 1])
        new_email = c1.text_input(
            "Email address",
            placeholder="user@tmcltd.ai or group:fi",
        )
        new_role = c2.selectbox("Role", ["editor", "viewer"])

        c3, c4 = st.columns(2)
        new_allowed = c3.text_input(
            "Allowed fields",
            value="*",
            help="Comma-separated column names, or * for all. Only applies to editor role.",
        )
        new_denied = c4.text_input(
            "Denied operations",
            placeholder="bulk_update, add_row",
            help="Comma-separated tool names to block. Leave empty to allow all.",
        )

        submitted = st.form_submit_button("Save rule", type="primary")

        if submitted:
            if not new_email.strip():
                st.error("Email is required.")
            else:
                email_key = new_email.strip().lower()

                allowed_fields = (
                    ["*"] if new_allowed.strip() in ("", "*")
                    else [f.strip() for f in new_allowed.split(",") if f.strip()]
                )
                denied_ops = (
                    [op.strip() for op in new_denied.split(",") if op.strip()]
                    if new_denied.strip() else []
                )

                permissions[email_key] = {
                    "role":              new_role,
                    "allowed_fields":    allowed_fields,
                    "denied_operations": denied_ops,
                }
                st.session_state["permissions_raw"] = permissions

                # Rebuild the checker for the current user in case their own
                # permissions just changed
                st.session_state["checker"] = PermissionChecker(
                    user_email, permissions
                )

                if save_permissions(st.session_state.token_dict, permissions):
                    st.success(f"Saved rule for **{email_key}**.")
                else:
                    st.warning(
                        "Rule saved in session but could not write to config sheet. "
                        "Check `config_sheet_id` in secrets."
                    )
                st.rerun()

    # ── Remove a user ─────────────────────────────────────────────────────────
    if permissions:
        st.markdown("#### Remove a rule")
        remove_email = st.selectbox(
            "Select user to remove",
            options=list(permissions.keys()),
            label_visibility="collapsed",
        )
        if st.button("🗑️ Remove rule", type="secondary"):
            del permissions[remove_email]
            st.session_state["permissions_raw"] = permissions
            st.session_state["checker"] = PermissionChecker(user_email, permissions)
            if save_permissions(st.session_state.token_dict, permissions):
                st.success(f"Removed rule for **{remove_email}**.")
            else:
                st.warning("Removed from session but could not update config sheet.")
            st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# Tab 2 — Cache Management
# ══════════════════════════════════════════════════════════════════════════════

with tab2:
    st.subheader("Cache Management")
    st.markdown(
        "Use these controls to force a refresh of cached data without "
        "signing out. Useful after a column is added/renamed in the sheet, "
        "or after editing permissions outside of this panel."
    )

    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown("**Column Map**")
        st.caption("Forces F11 to re-analyse sheet headers on next load.")
        if st.button("🔄 Refresh column map", use_container_width=True):
            st.session_state.pop("column_map",          None)
            st.session_state.pop("column_map_sheet_id", None)
            st.success("Column map cleared. Will rebuild on next chat load.")

    with col2:
        st.markdown("**Permissions**")
        st.caption("Reloads the permissions table from the config sheet.")
        if st.button("🔄 Refresh permissions", use_container_width=True):
            st.session_state.pop("checker",          None)
            st.session_state.pop("permissions_raw",  None)
            from src.permissions import ensure_permissions
            ensure_permissions(st.session_state.token_dict, user_email)
            st.success("Permissions reloaded.")

    with col3:
        st.markdown("**Executor cache**")
        st.caption("Clears cached row positions and header index. Safe to run any time.")
        if st.button("🔄 Clear executor cache", use_container_width=True):
            if "executor" in st.session_state:
                ex = st.session_state.executor
                ex._header_cache   = None
                ex._col_idx_cache  = {}
                ex._id_row_cache   = {}
                ex._sheet_id_cache = None
            st.success("Executor cache cleared.")

    st.divider()

    # ── Session state inspector (admin debug tool) ────────────────────────────
    with st.expander("🔍 Session state inspector"):
        safe_state = {
            k: v for k, v in st.session_state.items()
            if k not in ("token_dict",)   # don't expose the token
        }
        st.json(safe_state, expanded=False)