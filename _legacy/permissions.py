"""
src/permissions.py
──────────────────
F13 — Role-Based Access Control

Three tiers:
  Admin  — full access. Defined by secrets["app"]["admins"] list.
  Editor — read + write, but only to allowed_fields. Default for all users.
  Viewer — get_row, search_rows, summarize, switch_module only. No writes.

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
from typing import Dict, List, Set, Any, Optional, Tuple
from googleapiclient.errors import HttpError
from src.sheets_auth import build_sheets_service

# ── Constants ─────────────────────────────────────────────────────────────────

PERMISSIONS_TAB = "MigrationBot Permissions"

PERMISSIONS_HEADERS = ["email", "project", "role", "allowed_fields", "denied_operations"]

READ_ONLY_TOOLS = {"get_row", "search_rows", "summarize", "switch_module"}
WRITE_TOOLS     = {"update_cell", "bulk_update", "format_row", "add_row"}


# ── Custom Classes for Backward Compatibility ─────────────────────────────────

class ProjectPermissionsDict(dict):
    """
    A dictionary mapping project_name -> permission_dict.
    For backward compatibility, if a legacy key (like 'role', 'allowed_fields', 'denied_operations')
    is accessed directly on this dict, it delegates to the '*' wildcard project.
    """
    def __getitem__(self, key: str) -> Any:
        if key in ("role", "allowed_fields", "denied_operations"):
            wildcard_dict = super().get("*", {})
            if key == "role":
                return wildcard_dict.get("role", "editor")
            elif key == "allowed_fields":
                return wildcard_dict.get("allowed_fields", ["*"])
            elif key == "denied_operations":
                return wildcard_dict.get("denied_operations", [])
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default


class PermissionsRegistry(dict):
    """
    A dictionary mapping email -> ProjectPermissionsDict.
    For backward compatibility, if a value being set is a legacy flat dict,
    it automatically wraps it into a ProjectPermissionsDict with a '*' entry.
    """
    def __setitem__(self, key: str, value: Any) -> None:
        if isinstance(value, dict) and "role" in value:
            project_dict = ProjectPermissionsDict()
            project_dict["*"] = value
            value = project_dict
        super().__setitem__(key, value)


# ── Default policy ────────────────────────────────────────────────────────────

def _default_policy() -> Dict[str, Any]:
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

def _get_config_sheet_id() -> Optional[str]:
    """Retrieve the config spreadsheet ID from st.secrets."""
    return st.secrets.get("app", {}).get("config_sheet_id")


def _ensure_tab_exists(service: Any, config_sheet_id: str) -> None:
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


def load_permissions(token_dict: dict) -> PermissionsRegistry:
    """
    Load permissions from the config sheet.
    Supports backwards compatibility for 4-column layout by mapping project to "*".
    Returns a PermissionsRegistry keyed by lowercase email.
    """
    config_sheet_id = _get_config_sheet_id()
    if not config_sheet_id:
        return PermissionsRegistry()

    try:
        service = build_sheets_service(token_dict)
        _ensure_tab_exists(service, config_sheet_id)

        # Read A:E to include the 'project' column
        result = service.spreadsheets().values().get(
            spreadsheetId=config_sheet_id,
            range=f"{PERMISSIONS_TAB}!A:E",
        ).execute()
        rows = result.get("values", [])

        if len(rows) < 2:
            return PermissionsRegistry()   # headers only — no rules yet

        # Detect headers to verify structure and handle order/missing columns
        headers = [h.strip().lower() for h in rows[0]]
        has_project_col = "project" in headers

        project_idx = headers.index("project") if has_project_col else -1
        email_idx = headers.index("email") if "email" in headers else 0
        role_idx = headers.index("role") if "role" in headers else 1
        allowed_fields_idx = headers.index("allowed_fields") if "allowed_fields" in headers else 2
        denied_ops_idx = headers.index("denied_operations") if "denied_operations" in headers else 3

        permissions = PermissionsRegistry()

        for row in rows[1:]:   # skip header row
            if not row:
                continue

            # Pad short rows to match header count
            while len(row) < len(headers):
                row.append("")

            email = row[email_idx].strip().lower()
            if not email:
                continue

            project = row[project_idx].strip() if (has_project_col and project_idx != -1) else "*"
            if not project:
                project = "*"

            role = row[role_idx].strip().lower() or "editor"
            allowed_fields_raw = row[allowed_fields_idx]
            denied_ops_raw = row[denied_ops_idx]

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

            if email not in permissions:
                permissions[email] = ProjectPermissionsDict()

            permissions[email][project] = {
                "role":              role,
                "allowed_fields":    allowed_fields,
                "denied_operations": denied_ops,
            }

        return permissions

    except HttpError as e:
        if e.status_code in (403, 404):
            # Config sheet not accessible — non-fatal
            return PermissionsRegistry()
        raise
    except Exception:
        return PermissionsRegistry()


def save_permissions(token_dict: dict, permissions: dict) -> bool:
    """
    Write the permissions dict back to the config sheet in 5-column format.
    Returns True on success, False on failure.
    """
    config_sheet_id = _get_config_sheet_id()
    if not config_sheet_id:
        return False

    try:
        service = build_sheets_service(token_dict)
        _ensure_tab_exists(service, config_sheet_id)

        rows = [PERMISSIONS_HEADERS]
        for email, proj_perms in permissions.items():
            # If for some reason it's a legacy flat dict, wrap it
            if isinstance(proj_perms, dict) and "role" in proj_perms:
                proj_perms = {"*": proj_perms}

            for project, perm in proj_perms.items():
                allowed = (
                    "*" if perm.get("allowed_fields") == ["*"]
                    else ", ".join(perm.get("allowed_fields", ["*"]))
                )
                denied = ", ".join(perm.get("denied_operations", []))
                rows.append([email, project, perm.get("role", "editor"), allowed, denied])

        # Clear and rewrite
        service.spreadsheets().values().clear(
            spreadsheetId=config_sheet_id,
            range=f"{PERMISSIONS_TAB}!A:E",
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

    Resolves roles hierarchically:
      1. Admin list from secrets → always allowed
      2. Project-specific permissions from config sheet
      3. Wildcard (*) permissions from config sheet
      4. Default policy from secrets → editor with full access
    """

    def __init__(self, email: str, permissions: dict, active_project: Optional[str] = None):
        self.email = email.lower().strip()

        # Admins from secrets always win — even if config sheet says otherwise
        admins = [a.lower() for a in st.secrets.get("app", {}).get("admins", [])]
        if self.email in admins:
            self.role            = "admin"
            self.allowed_fields  = ["*"]
            self.denied_ops      = set()
            return

        # Look up in config sheet permissions
        user_perms = permissions.get(self.email, None)

        # Group fallback: match "group:prefix" keys
        if user_perms is None:
            domain_prefix = self.email.split("@")[0].split(".")[0]
            for key, val in permissions.items():
                if key.startswith("group:") and domain_prefix.startswith(key[6:]):
                    user_perms = val
                    break

        perm = None
        if user_perms is not None:
            if isinstance(user_perms, dict) and "role" in user_perms:
                # Legacy flat dict format
                perm = user_perms
            else:
                # Nested project-specific dictionary
                if active_project and active_project in user_perms:
                    perm = user_perms[active_project]
                elif "*" in user_perms:
                    perm = user_perms["*"]

        # Final fallback: default policy
        if perm is None:
            perm = _default_policy()

        self.role           = perm.get("role", "editor")
        self.allowed_fields = perm.get("allowed_fields", ["*"])
        self.denied_ops     = set(perm.get("denied_operations", []))

    def is_admin(self) -> bool:
        """Return True if the user has admin role."""
        return self.role == "admin"

    def can_execute(self, tool_name: str, args: dict) -> Tuple[bool, str]:
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

def ensure_permissions(token_dict: dict, user_email: str, active_project: Optional[str] = None) -> None:
    """
    Load permissions from the config sheet and build the PermissionChecker
    for this user, storing both in st.session_state.

    Runs once per session. Subsequent calls are instant cache hits.
    """
    if "checker" in st.session_state:
        return

    # Attempt auto-detection of active project from session state if not supplied
    if active_project is None:
        active_sheet = st.session_state.get("active_sheet")
        if active_sheet:
            active_project = active_sheet.get("sheet_label")

    permissions = load_permissions(token_dict)
    st.session_state["permissions_raw"] = permissions
    st.session_state["checker"]         = PermissionChecker(user_email, permissions, active_project)


def get_checker() -> Optional[PermissionChecker]:
    """Retrieve the cached PermissionChecker instance from st.session_state."""
    return st.session_state.get("checker")