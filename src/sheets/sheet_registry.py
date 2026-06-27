"""
src/sheets/sheet_registry.py
────────────────────────────
F12 — Sheet Agnostic Mode

Handles everything related to discovering and registering Google Sheets at
runtime:
  - Parsing a spreadsheet ID out of any Google Sheets URL
  - Listing available tabs inside a spreadsheet
  - Validating that the signed-in user has access (catches 403s cleanly)
  - Managing the session-level "active sheet" state

The active sheet is stored in st.session_state["active_sheet"] as:
    {"spreadsheet_id": str, "sheet_tab_name": str, "sheet_label": str}

When active_sheet changes, app.py rebuilds the SheetsExecutor with the new
values, which clears all caches and triggers a fresh column map analysis
(F11, once implemented).
"""

import re
import warnings
import streamlit as st
from googleapiclient.errors import HttpError
from src.sheets_auth import build_sheets_service


# ── URL parsing ───────────────────────────────────────────────────────────────

def parse_sheet_id(url: str) -> str | None:
    """
    Extract the spreadsheet ID from any valid Google Sheets URL.

    Handles all common formats:
      https://docs.google.com/spreadsheets/d/{ID}/edit#gid=0
      https://docs.google.com/spreadsheets/d/{ID}/edit?usp=sharing
      https://docs.google.com/spreadsheets/d/{ID}
    """
    url = url.strip()
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    return match.group(1) if match else None


# ── Sheet metadata ────────────────────────────────────────────────────────────

def fetch_sheet_tabs(token_dict: dict, spreadsheet_id: str) -> list[dict]:
    """
    Return a list of tab dicts for the given spreadsheet.
    Each dict: {"title": str, "sheet_id": int, "index": int}

    Raises:
      PermissionError  — if the user's token cannot access this sheet (403)
      ValueError       — if the spreadsheet_id is invalid (404)
      RuntimeError     — for any other API error
    """
    service = build_sheets_service(token_dict)
    try:
        meta = service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties"
        ).execute()
    except HttpError as e:
        if e.status_code == 403:
            raise PermissionError(
                "You don't have access to this sheet. "
                "Ask the owner to share it with your Google account (Editor access needed)."
            )
        if e.status_code == 404:
            raise ValueError(
                "Sheet not found. Check the URL and make sure the sheet exists."
            )
        raise RuntimeError(f"Google Sheets API error {e.status_code}: {e.reason}")

    return [
        {
            "title":    s["properties"]["title"],
            "sheet_id": s["properties"]["sheetId"],
            "index":    s["properties"]["index"],
        }
        for s in meta.get("sheets", [])
    ]


def fetch_sheet_name(token_dict: dict, spreadsheet_id: str) -> str:
    """Return the human-readable name of the spreadsheet (the document title)."""
    service = build_sheets_service(token_dict)
    try:
        meta = service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="properties.title"
        ).execute()
        return meta["properties"]["title"]
    except HttpError:
        return spreadsheet_id   # fall back to ID if title fetch fails


# ── Active sheet session state ────────────────────────────────────────────────

def get_default_sheet() -> dict:
    """
    Return the default sheet config from secrets.toml.
    This is always available as the fallback.
    """
    return {
        "spreadsheet_id": st.secrets["app"]["spreadsheet_id"],
        "sheet_tab_name": st.secrets["app"]["sheet_tab_name"],
        "sheet_label":    st.secrets["app"].get("default_sheet_label", "Default Tracker"),
    }


def get_active_sheet() -> dict:
    """
    Return the currently active sheet config from session state,
    defaulting to the secrets-configured sheet if none has been selected.
    """
    return st.session_state.get("active_sheet", get_default_sheet())


def set_active_sheet(spreadsheet_id: str, sheet_tab_name: str,
                     sheet_label: str = "") -> None:
    """
    Set the active sheet in session state and clear the executor so it is
    rebuilt with the new sheet on the next rerun.
    """
    st.session_state["active_sheet"] = {
        "spreadsheet_id": spreadsheet_id,
        "sheet_tab_name": sheet_tab_name,
        "sheet_label":    sheet_label or sheet_tab_name,
    }
    # Clear executor, column map, and audit logger — all will be rebuilt
    # on the next rerun with the new sheet context.
    st.session_state.pop("executor",            None)
    st.session_state.pop("executor_key",        None)
    st.session_state.pop("column_map",          None)
    st.session_state.pop("column_map_sheet_id", None)
    st.session_state.pop("audit_logger",        None)
    st.session_state.pop("audit_logger_key",    None)


def reset_to_default_sheet() -> None:
    """Switch back to the default sheet from secrets."""
    st.session_state.pop("active_sheet",        None)
    st.session_state.pop("executor",            None)
    st.session_state.pop("executor_key",        None)
    st.session_state.pop("column_map",          None)
    st.session_state.pop("column_map_sheet_id", None)
    st.session_state.pop("audit_logger",        None)
    st.session_state.pop("audit_logger_key",    None)


# ── Admin helpers ─────────────────────────────────────────────────────────────

def is_admin(email: str) -> bool:
    """
    Check if the given email is in the admin list from secrets.

    .. deprecated:: 1.0.0
       Use PermissionChecker.is_admin instead.
    """
    warnings.warn(
        "is_admin() in sheet_registry is deprecated and will be removed in a future version. "
        "Use PermissionChecker().is_admin instead.",
        DeprecationWarning,
        stacklevel=2
    )
    admins = st.secrets.get("app", {}).get("admins", [])
    return email.lower() in [a.lower() for a in admins]