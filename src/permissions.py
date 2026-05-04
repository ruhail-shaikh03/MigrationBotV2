"""
src/permissions.py
──────────────────
F13 — Role-Based Access Control

Three tiers:
  Admin  — full access. Defined by secrets["app"]["admins"] list.
  Editor — read + write, but only to allowed_fields. Default for all users.
  Viewer — get_row, search_rows, summarize only. No writes.

Permission store: "MigrationBot Permissions" tab in the config sheet
(secrets["app"]["config_sheet_id"]).

Falls back to the default policy from secrets if the config sheet is
unavailable or the tab doesn't exist yet — the app always stays usable.

Session storage: st.session_state["checker"] holds the PermissionChecker
instance. st.session_state["permissions_raw"] holds the raw dict for the
admin UI to read and write.
"""

import json
import streamlit as st
from googleapiclient.errors import HttpError
from src.sheets_auth import build_sheets_service

# ── Constants ─────────────────────────────────────────────────────────────────

PERMISSIONS_TAB = "MigrationBot Permissions"

PERMISSIONS_HEADERS = ["email", "role", "allowed_fields", "denied_operations"]

READ_ONLY_TOOLS = {"get_row", "search_rows", "summarize"}
WRITE_TOOLS     = {"update_cell", "bulk_update", "format_row", "add_row"}

# ── Default policy ────────────────────────────────────────────────────────────

def _default_policy() -> dict:
    """
    Return the default permission policy from secrets.
    Used when no config sheet is configured or the tab is empty.
    """
    return {
        "role":               "editor",
        "allowed_fields":     ["*"],
        "denied_operations":  [],
    }


# ── Config sheet helpers ──────────────────────────────────────────────────────

def _get_config_sheet_id() -> str | None:
    return st.secrets.get("app", {}).get("config_sheet_id")


def _ensure_tab_exists(service, config_sheet_id: str) -> None:
    """Create the permissions tab with headers if it doesn't exist."""
    meta = service.spreadsheets().get(
        spreadsheetId=config_sheet_id,
        fields="sheets.properties"
    ).execute()
    tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]

    if PERMISSIONS_TAB not in tabs:
        service.spreadsheets().batchUpdate(
            spreadsheetId=config_sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": PERMISSIONS_TAB}}}]}
        ).execute()
        service.spreadsheets().values().update(
            spreadsheetId=config_sheet_id,
            range=f"{PERMISSIONS_TAB}!1:1",
            valueInputOption="RAW",
            body={"values": [PERMISSIONS_HEADERS]}
        ).execute()


def load_permissions(token_dict: dict) -> dict:
    """
    Load permissions from the config sheet.
    Returns a dict keyed by lowercase email:
        {
          "sara@tmcltd.ai": {
              "role": "admin",
              "allowed_fields": ["*"],
              "denied_operations": []
          },
          ...
        }
    Returns an empty dict if no config sheet is configured.
    """
    config_sheet_id = _get_config_sheet_id()
    if not config_sheet_id:
        return {}

    try:
        service = build_sheets_service(token_dict)
        _ensure_tab_exists(service, config_sheet_id)

        result = service.spreadsheets().values().get(
            spreadsheetId=config_sheet_id,
            range=f"{PERMISSIONS_TAB}!A:D",
        ).execute()
        rows = result.get("values", [])

        if len(rows) < 2:
            return {}   # headers only — no rules yet

        permissions = {}
        for row in rows[1:]:   # skip header row
            # Pad short rows
            while len(row) < 4:
                row.append("")

            email, role, allowed_fields_raw, denied_ops_raw = row[:4]
            email = email.strip().lower()
            if not email:
                continue

            # allowed_fields can be "*" or comma-separated column names
            if allowed_fields_raw.strip() == "*" or not allowed_fields_raw.strip():
                allowed_fields = ["*"]
            else:
                allowed_fields = [f.strip() for f in allowed_fields_raw.split(",") if f.strip()]

            # denied_operations is comma-separated tool names
            if denied_ops_raw.strip():
                denied_ops = [op.strip() for op in denied_ops_raw.split(",") if op.strip()]
            else:
                denied_ops = []

            permissions[email] = {
                "role":              role.strip().lower() or "editor",
                "allowed_fields":    allowed_fields,
                "denied_operations": denied_ops,
            }

        return permissions

    except HttpError as e:
        if e.status_code in (403, 404):
            # Config sheet not accessible — non-fatal
            return {}
        raise
    except Exception:
        return {}


def save_permissions(token_dict: dict, permissions: dict) -> bool:
    """
    Write the permissions dict back to the config sheet.
    Returns True on success, False on failure.
    """
    config_sheet_id = _get_config_sheet_id()
    if not config_sheet_id:
        return False

    try:
        service = build_sheets_service(token_dict)
        _ensure_tab_exists(service, config_sheet_id)

        rows = [PERMISSIONS_HEADERS]
        for email, perm in permissions.items():
            allowed = (
                "*" if perm["allowed_fields"] == ["*"]
                else ", ".join(perm["allowed_fields"])
            )
            denied = ", ".join(perm.get("denied_operations", []))
            rows.append([email, perm["role"], allowed, denied])

        # Clear and rewrite
        service.spreadsheets().values().clear(
            spreadsheetId=config_sheet_id,
            range=f"{PERMISSIONS_TAB}!A:D",
        ).execute()
        service.spreadsheets().values().update(
            spreadsheetId=config_sheet_id,
            range=f"{PERMISSIONS_TAB}!A1",
            valueInputOption="RAW",
            body={"values": rows},
        ).execute()
        return True

    except Exception:
        return False


# ── PermissionChecker ─────────────────────────────────────────────────────────

class PermissionChecker:
    """
    Evaluates whether the signed-in user is allowed to execute a given tool
    with given arguments.

    Resolution order:
      1. Admin list from secrets → always allowed
      2. Permissions dict from config sheet → role + field/op rules
      3. Default policy from secrets → editor with full access
    """

    def __init__(self, email: str, permissions: dict):
        self.email = email.lower().strip()

        # Admins from secrets always win — even if config sheet says otherwise
        admins = [a.lower() for a in st.secrets.get("app", {}).get("admins", [])]
        if self.email in admins:
            self.role            = "admin"
            self.allowed_fields  = ["*"]
            self.denied_ops      = set()
            return

        # Look up in config sheet permissions
        perm = permissions.get(self.email, None)

        # Group fallback: match "group:prefix" keys
        if perm is None:
            domain_prefix = self.email.split("@")[0].split(".")[0]
            for key, val in permissions.items():
                if key.startswith("group:") and domain_prefix.startswith(key[6:]):
                    perm = val
                    break

        # Final fallback: default policy
        if perm is None:
            perm = _default_policy()

        self.role           = perm.get("role", "editor")
        self.allowed_fields = perm.get("allowed_fields", ["*"])
        self.denied_ops     = set(perm.get("denied_operations", []))

    def is_admin(self) -> bool:
        return self.role == "admin"

    def can_execute(self, tool_name: str, args: dict) -> tuple[bool, str]:
        """
        Returns (allowed: bool, reason: str).
        reason is empty when allowed, human-readable when denied.
        """
        # Admins bypass all checks
        if self.role == "admin":
            return True, ""

        # Viewers: read-only tools only
        if self.role == "viewer":
            if tool_name not in READ_ONLY_TOOLS:
                return False, (
                    f"You have read-only access and cannot run `{tool_name}`. "
                    "Contact an admin to request write access."
                )
            return True, ""

        # Editors: check denied operations
        if tool_name in self.denied_ops:
            return False, (
                f"You don't have permission to run `{tool_name}`. "
                "Contact an admin if you need this access."
            )

        # Editors: check field-level access for update_cell
        if tool_name == "update_cell" and self.allowed_fields != ["*"]:
            blocked = [
                upd["field"] for upd in args.get("updates", [])
                if upd.get("field") not in self.allowed_fields
            ]
            if blocked:
                fields = ", ".join(blocked)
                return False, (
                    f"You don't have write access to: **{fields}**. "
                    f"Your allowed fields are: {', '.join(self.allowed_fields)}."
                )

        # Editors: check field-level access for bulk_update
        if tool_name == "bulk_update" and self.allowed_fields != ["*"]:
            field = args.get("set_field", "")
            if field not in self.allowed_fields:
                return False, (
                    f"You don't have write access to **{field}**. "
                    f"Your allowed fields are: {', '.join(self.allowed_fields)}."
                )

        return True, ""


# ── Session-level helpers ─────────────────────────────────────────────────────

def ensure_permissions(token_dict: dict, user_email: str) -> None:
    """
    Load permissions from the config sheet and build the PermissionChecker
    for this user, storing both in st.session_state.

    Runs once per session. Subsequent calls are instant cache hits.
    """
    if "checker" in st.session_state:
        return

    permissions = load_permissions(token_dict)
    st.session_state["permissions_raw"] = permissions
    st.session_state["checker"]         = PermissionChecker(user_email, permissions)


def get_checker() -> PermissionChecker | None:
    return st.session_state.get("checker")