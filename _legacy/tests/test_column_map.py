import pytest
from unittest.mock import patch, MagicMock
import streamlit as st
from src.sheets.column_map import resolve_column

from src.sheets.dynamic_column_mapper import ensure_column_map, invalidate_column_map, get_active_column_map

@pytest.fixture(autouse=True)
def clear_session_state():
    if "column_map" in st.session_state:
        del st.session_state["column_map"]
    if "column_map_sheet_key" in st.session_state:
        del st.session_state["column_map_sheet_key"]
    if "column_maps" in st.session_state:
        del st.session_state["column_maps"]
    yield

def test_exact_match():
    custom_map = {
        "Dev Status": ["status", "development status"],
        "Description": ["desc", "object description"]
    }
    # Case-insensitive/stripped exact match
    res = resolve_column("dev status", column_map=custom_map)
    assert res == "Dev Status"
    
    res = resolve_column("Description ", column_map=custom_map)
    assert res == "Description"

def test_alias_match():
    custom_map = {
        "Dev Status": ["status", "development status"],
        "Description": ["desc", "object description"]
    }
    res = resolve_column("status", column_map=custom_map)
    assert res == "Dev Status"
    
    res = resolve_column("desc", column_map=custom_map)
    assert res == "Description"

def test_fuzzy_match():
    custom_map = {
        "Dev Status": ["status", "development status"],
        "Description": ["desc", "object description"]
    }
    res = resolve_column("dev stat", column_map=custom_map)
    assert res == "Dev Status"

def test_not_found():
    custom_map = {
        "Dev Status": ["status", "development status"]
    }
    res = resolve_column("xyzzy", column_map=custom_map)
    assert res is None

def test_cache_key_tab_aware():
    mock_executor = MagicMock()
    mock_executor.spreadsheet_id = "spread1"
    mock_executor.SHEET_NAME = "SD"
    mock_executor._get_header_row.return_value = ["Dev Status", "Description"]
    
    # Mock LLM calls
    with patch("src.sheets.dynamic_column_mapper.build_column_map") as mock_build:
        mock_build.return_value = {
            "Dev Status": ["status"],
            "Description": ["desc"]
        }
        
        ensure_column_map(mock_executor)
        assert st.session_state["column_map_sheet_key"] == "spread1:SD"
        
        # Change tab
        mock_executor.SHEET_NAME = "MM"
        mock_executor._get_header_row.return_value = ["Dev Status", "Owner"]
        ensure_column_map(mock_executor)
        assert st.session_state["column_map_sheet_key"] == "spread1:MM"

def test_multi_tab_cache():
    mock_executor = MagicMock()
    mock_executor.spreadsheet_id = "spread1"
    mock_executor.SHEET_NAME = "SD"
    
    map_sd = {"Dev Status": ["status"]}
    map_mm = {"Owner": ["business owner"]}
    
    with patch("src.sheets.dynamic_column_mapper.build_column_map") as mock_build:
        # First call return SD map, second return MM map
        mock_build.side_effect = [map_sd, map_mm]
        
        mock_executor._get_header_row.return_value = ["Dev Status"]
        ensure_column_map(mock_executor)
        assert st.session_state["column_map"] == map_sd
        
        # Switch to MM
        mock_executor.SHEET_NAME = "MM"
        mock_executor._get_header_row.return_value = ["Owner"]
        ensure_column_map(mock_executor)
        assert st.session_state["column_map"] == map_mm
        
        # Switch back to SD - should resolve from cache without rebuild (mock_build won't be called again)
        mock_executor.SHEET_NAME = "SD"
        mock_executor._get_header_row.return_value = ["Dev Status"]
        ensure_column_map(mock_executor)
        assert st.session_state["column_map"] == map_sd
        assert mock_build.call_count == 2
