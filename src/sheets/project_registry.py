"""
src/sheets/project_registry.py
──────────────────────────────
Phase 1 — Admin-Defined Project Registry

Manages a persistent list of projects in the Global Config Sheet under the
"MigrationBot Projects" tab. Caches active projects in Streamlit session state
to minimize API calls.
"""

import streamlit as st
from typing import List, Dict, Any, Optional
from googleapiclient.errors import HttpError
from src.sheets_auth import build_sheets_service

# ── Constants ─────────────────────────────────────────────────────────────────

PROJECTS_TAB = "MigrationBot Projects"
PROJECTS_HEADERS = [
    "project_name",
    "spreadsheet_id",
    "default_tab",
    "company_prefix",
    "is_active"
]

# ── Internal Helpers ──────────────────────────────────────────────────────────

def _get_config_sheet_id() -> Optional[str]:
    """Retrieve the global configuration spreadsheet ID from Streamlit secrets."""
    return st.secrets.get("app", {}).get("config_sheet_id")


def _ensure_projects_tab(service: Any, config_sheet_id: str) -> None:
    """
    Ensure the projects tab exists in the configuration spreadsheet.
    Creates the tab and writes the default headers if missing.
    """
    try:
        meta = service.spreadsheets().get(
            spreadsheetId=config_sheet_id,
            fields="sheets.properties"
        ).execute()
        tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]

        if PROJECTS_TAB not in tabs:
            # Create the tab
            service.spreadsheets().batchUpdate(
                spreadsheetId=config_sheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": PROJECTS_TAB}}}]}
            ).execute()
            # Initialize with header row
            service.spreadsheets().values().update(
                spreadsheetId=config_sheet_id,
                range=f"{PROJECTS_TAB}!1:1",
                valueInputOption="RAW",
                body={"values": [PROJECTS_HEADERS]}
            ).execute()
    except Exception as e:
        # Log to Streamlit but allow progression if fallback is handled
        st.warning(f"Could not verify or create projects tab: {e}")


def get_default_project() -> Dict[str, str]:
    """
    Construct the fallback project configuration using the default sheet settings
    defined in secrets.toml.
    """
    return {
        "project_name": st.secrets["app"].get("default_sheet_label", "Default Tracker"),
        "spreadsheet_id": st.secrets["app"]["spreadsheet_id"],
        "default_tab": st.secrets["app"]["sheet_tab_name"],
        "company_prefix": "",
        "is_active": "TRUE",
    }

# ── Public API ────────────────────────────────────────────────────────────────

def load_all_projects(token_dict: dict) -> List[Dict[str, str]]:
    """
    Fetch all projects (active and inactive) from the global config sheet.
    Falls back to the default project in secrets if no registry exists or is empty.
    """
    config_sheet_id = _get_config_sheet_id()
    if not config_sheet_id:
        try:
            return [get_default_project()]
        except KeyError:
            return []

    try:
        service = build_sheets_service(token_dict)
        _ensure_projects_tab(service, config_sheet_id)

        result = service.spreadsheets().values().get(
            spreadsheetId=config_sheet_id,
            range=f"{PROJECTS_TAB}!A:E",
        ).execute()
        rows = result.get("values", [])

        if len(rows) < 2:
            try:
                return [get_default_project()]
            except KeyError:
                return []

        projects = []
        # Header is row 0; data starts at row 1
        for row in rows[1:]:
            while len(row) < len(PROJECTS_HEADERS):
                row.append("")
            
            p = {
                "project_name": row[0].strip(),
                "spreadsheet_id": row[1].strip(),
                "default_tab": row[2].strip(),
                "company_prefix": row[3].strip(),
                "is_active": row[4].strip().upper() or "TRUE"
            }
            if p["spreadsheet_id"]:
                projects.append(p)

        return projects
    except Exception:
        try:
            return [get_default_project()]
        except KeyError:
            return []


def load_projects(token_dict: dict) -> List[Dict[str, str]]:
    """
    Fetch all active projects and cache the result in st.session_state["project_registry"].
    Reuses cached values if available.
    """
    if "project_registry" in st.session_state and st.session_state["project_registry"] is not None:
        return st.session_state["project_registry"]

    all_projects = load_all_projects(token_dict)
    active_projects = [p for p in all_projects if p.get("is_active", "TRUE").upper() == "TRUE"]
    st.session_state["project_registry"] = active_projects
    return active_projects


def save_project(token_dict: dict, project: dict) -> bool:
    """
    Upsert a project row in the registry tab (matching by spreadsheet_id).
    Invalidates the session cache upon successful execution.
    """
    config_sheet_id = _get_config_sheet_id()
    if not config_sheet_id:
        return False

    try:
        service = build_sheets_service(token_dict)
        _ensure_projects_tab(service, config_sheet_id)

        result = service.spreadsheets().values().get(
            spreadsheetId=config_sheet_id,
            range=f"{PROJECTS_TAB}!A:E",
        ).execute()
        rows = result.get("values", [])

        target_id = project["spreadsheet_id"].strip()
        row_idx = -1

        if len(rows) > 1:
            for idx, r in enumerate(rows[1:], start=2):
                if len(r) > 1 and r[1].strip() == target_id:
                    row_idx = idx
                    break

        new_row = [
            project.get("project_name", "").strip(),
            target_id,
            project.get("default_tab", "").strip(),
            project.get("company_prefix", "").strip(),
            project.get("is_active", "TRUE").strip().upper(),
        ]

        if row_idx != -1:
            service.spreadsheets().values().update(
                spreadsheetId=config_sheet_id,
                range=f"{PROJECTS_TAB}!A{row_idx}:E{row_idx}",
                valueInputOption="RAW",
                body={"values": [new_row]}
            ).execute()
        else:
            service.spreadsheets().values().append(
                spreadsheetId=config_sheet_id,
                range=f"{PROJECTS_TAB}!A:E",
                valueInputOption="RAW",
                body={"values": [new_row]}
            ).execute()

        invalidate_cache()
        return True
    except Exception:
        return False


def delete_project(token_dict: dict, spreadsheet_id: str) -> bool:
    """
    Remove a project row from the configuration sheet by its spreadsheet_id.
    Invalidates the session cache upon successful execution.
    """
    config_sheet_id = _get_config_sheet_id()
    if not config_sheet_id:
        return False

    try:
        service = build_sheets_service(token_dict)
        _ensure_projects_tab(service, config_sheet_id)

        result = service.spreadsheets().values().get(
            spreadsheetId=config_sheet_id,
            range=f"{PROJECTS_TAB}!A:E",
        ).execute()
        rows = result.get("values", [])

        if len(rows) <= 1:
            return False

        new_rows = [rows[0]]  # retain header
        deleted = False

        for r in rows[1:]:
            if len(r) > 1 and r[1].strip() == spreadsheet_id.strip():
                deleted = True
                continue
            new_rows.append(r)

        if not deleted:
            return False

        service.spreadsheets().values().clear(
            spreadsheetId=config_sheet_id,
            range=f"{PROJECTS_TAB}!A:E",
        ).execute()

        service.spreadsheets().values().update(
            spreadsheetId=config_sheet_id,
            range=f"{PROJECTS_TAB}!A1",
            valueInputOption="RAW",
            body={"values": new_rows}
        ).execute()

        invalidate_cache()
        return True
    except Exception:
        return False


def get_project_for_sheet(spreadsheet_id: str) -> Optional[Dict[str, str]]:
    """
    Lookup a project from the cached session registry by its spreadsheet_id.
    Returns None if the registry is not cached or project is not found.
    """
    cache = st.session_state.get("project_registry")
    if not cache:
        return None
    for p in cache:
        if p.get("spreadsheet_id") == spreadsheet_id:
            return p
    return None


def invalidate_cache() -> None:
    """Clear the cached project registry from Streamlit session state."""
    st.session_state.pop("project_registry", None)
