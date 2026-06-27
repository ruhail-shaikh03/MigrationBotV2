import pytest
from unittest.mock import patch, MagicMock
import streamlit as st
from src.sheets.project_registry import (
    load_all_projects,
    load_projects,
    save_project,
    delete_project,
    invalidate_cache,
    get_project_for_sheet
)

@pytest.fixture
def mock_secrets():
    mock_data = {
        "app": {
            "config_sheet_id": "config_sheet_id",
            "spreadsheet_id": "default_spread",
            "sheet_tab_name": "SD",
            "default_sheet_label": "Default Label"
        }
    }
    with patch.object(st, "secrets", mock_data):
        yield


def test_load_empty(mock_secrets):
    mock_service = MagicMock()
    with patch("src.sheets.project_registry.build_sheets_service", return_value=mock_service):
        with patch("src.sheets.project_registry._ensure_projects_tab"):
            # Empty rows or only headers
            mock_service.spreadsheets().values().get().execute.return_value = {
                "values": [["project_name", "spreadsheet_id", "default_tab", "company_prefix", "is_active"]]
            }
            res = load_all_projects({})
            # Should fall back to the default project in secrets
            assert len(res) == 1
            assert res[0]["spreadsheet_id"] == "default_spread"

def test_load_standard(mock_secrets):
    mock_service = MagicMock()
    with patch("src.sheets.project_registry.build_sheets_service", return_value=mock_service):
        with patch("src.sheets.project_registry._ensure_projects_tab"):
            mock_service.spreadsheets().values().get().execute.return_value = {
                "values": [
                    ["project_name", "spreadsheet_id", "default_tab", "company_prefix", "is_active"],
                    ["Proj A", "id_a", "SD", "FFC", "TRUE"],
                    ["Proj B", "id_b", "MM", "FFC", "TRUE"]
                ]
            }
            res = load_all_projects({})
            assert len(res) == 2
            assert res[0]["project_name"] == "Proj A"
            assert res[1]["project_name"] == "Proj B"

def test_load_inactive_filtered(mock_secrets):
    mock_service = MagicMock()
    with patch("src.sheets.project_registry.build_sheets_service", return_value=mock_service):
        with patch("src.sheets.project_registry._ensure_projects_tab"):
            mock_service.spreadsheets().values().get().execute.return_value = {
                "values": [
                    ["project_name", "spreadsheet_id", "default_tab", "company_prefix", "is_active"],
                    ["Proj Active", "id_active", "SD", "FFC", "TRUE"],
                    ["Proj Inactive", "id_inactive", "MM", "FFC", "FALSE"]
                ]
            }
            if "project_registry" in st.session_state:
                del st.session_state["project_registry"]
            res = load_projects({})
            assert len(res) == 1
            assert res[0]["project_name"] == "Proj Active"

def test_load_all_includes_inactive(mock_secrets):
    mock_service = MagicMock()
    with patch("src.sheets.project_registry.build_sheets_service", return_value=mock_service):
        with patch("src.sheets.project_registry._ensure_projects_tab"):
            mock_service.spreadsheets().values().get().execute.return_value = {
                "values": [
                    ["project_name", "spreadsheet_id", "default_tab", "company_prefix", "is_active"],
                    ["Proj Active", "id_active", "SD", "FFC", "TRUE"],
                    ["Proj Inactive", "id_inactive", "MM", "FFC", "FALSE"]
                ]
            }
            res = load_all_projects({})
            assert len(res) == 2

def test_save_new(mock_secrets):
    mock_service = MagicMock()
    with patch("src.sheets.project_registry.build_sheets_service", return_value=mock_service):
        with patch("src.sheets.project_registry._ensure_projects_tab"):
            mock_service.spreadsheets().values().get().execute.return_value = {
                "values": [
                    ["project_name", "spreadsheet_id", "default_tab", "company_prefix", "is_active"],
                    ["Proj Active", "id_active", "SD", "FFC", "TRUE"]
                ]
            }
            proj = {"project_name": "New Proj", "spreadsheet_id": "new_id", "default_tab": "SD", "company_prefix": "FFC", "is_active": "TRUE"}
            ok = save_project({}, proj)
            assert ok is True
            # Should append new row
            mock_service.spreadsheets().values().append.assert_called_once()

def test_save_upsert(mock_secrets):
    mock_service = MagicMock()
    with patch("src.sheets.project_registry.build_sheets_service", return_value=mock_service):
        with patch("src.sheets.project_registry._ensure_projects_tab"):
            mock_service.spreadsheets().values().get().execute.return_value = {
                "values": [
                    ["project_name", "spreadsheet_id", "default_tab", "company_prefix", "is_active"],
                    ["Proj Active", "id_active", "SD", "FFC", "TRUE"]
                ]
            }
            # Update Proj Active
            proj = {"project_name": "Proj Active Updated", "spreadsheet_id": "id_active", "default_tab": "MM", "company_prefix": "FFC", "is_active": "TRUE"}
            ok = save_project({}, proj)
            assert ok is True
            # Should update row 2
            mock_service.spreadsheets().values().update.assert_called_once()

def test_delete(mock_secrets):
    mock_service = MagicMock()
    with patch("src.sheets.project_registry.build_sheets_service", return_value=mock_service):
        with patch("src.sheets.project_registry._ensure_projects_tab"):
            mock_service.spreadsheets().values().get().execute.return_value = {
                "values": [
                    ["project_name", "spreadsheet_id", "default_tab", "company_prefix", "is_active"],
                    ["Proj Active", "id_active", "SD", "FFC", "TRUE"],
                    ["Proj Delete", "id_delete", "MM", "FFC", "TRUE"]
                ]
            }
            ok = delete_project({}, "id_delete")
            assert ok is True
            mock_service.spreadsheets().values().clear.assert_called_once()
            mock_service.spreadsheets().values().update.assert_called_once()
