import pytest
from unittest.mock import MagicMock, patch
from src.sheets.executor import SheetsExecutor

@pytest.fixture
def mock_token():
    return {"access_token": "fake_access", "refresh_token": "fake_refresh"}

@patch("src.sheets.executor.build_sheets_service")
def test_detect_header_row_1(mock_build, mock_token):
    # Setup mock service response for first 5 rows
    # Row 1 contains canonical headers, Row 2 data
    mock_service = MagicMock()
    mock_build.return_value = mock_service
    
    mock_values_get = mock_service.spreadsheets().values().get
    mock_values_get.return_value.execute.return_value = {
        "values": [
            ["RICEFW ID", "Module", "Description", "Type"],
            ["FFC-SD-001", "SD", "Test desc", "R"]
        ]
    }
    
    executor = SheetsExecutor(mock_token, "fake_spread_id", "SD")
    assert executor._header_row_num == 1
    assert executor.DATA_START_ROW == 2

@patch("src.sheets.executor.build_sheets_service")
def test_detect_header_row_2(mock_build, mock_token):
    # Row 1 has section info, Row 2 has headers
    mock_service = MagicMock()
    mock_build.return_value = mock_service
    
    mock_values_get = mock_service.spreadsheets().values().get
    mock_values_get.return_value.execute.return_value = {
        "values": [
            ["WRIICEF Project Tracker", "", "", ""],
            ["RICEFW ID", "Module", "Description", "Type"],
            ["FFC-SD-001", "SD", "Test desc", "R"]
        ]
    }
    
    executor = SheetsExecutor(mock_token, "fake_spread_id", "SD")
    assert executor._header_row_num == 2
    assert executor.DATA_START_ROW == 3

@patch("src.sheets.executor.build_sheets_service")
def test_detect_header_not_found(mock_build, mock_token):
    # No canonical markers found, should fall back to 1
    mock_service = MagicMock()
    mock_build.return_value = mock_service
    
    mock_values_get = mock_service.spreadsheets().values().get
    mock_values_get.return_value.execute.return_value = {
        "values": [
            ["garbage", "garbage2"],
            ["garbage3", "garbage4"]
        ]
    }
    
    executor = SheetsExecutor(mock_token, "fake_spread_id", "SD")
    assert executor._header_row_num == 1
    assert executor.DATA_START_ROW == 2

@patch("src.sheets.executor.build_sheets_service")
def test_detect_header_dashboard(mock_build, mock_token):
    # Dashboard tab (no rows or only empty values)
    mock_service = MagicMock()
    mock_build.return_value = mock_service
    
    mock_values_get = mock_service.spreadsheets().values().get
    mock_values_get.return_value.execute.return_value = {}
    
    executor = SheetsExecutor(mock_token, "fake_spread_id", "Dashboard")
    assert executor._header_row_num == 1
    assert executor.DATA_START_ROW == 2

@patch("src.sheets.executor.build_sheets_service")
def test_next_id_prefixed(mock_build, mock_token):
    mock_service = MagicMock()
    mock_build.return_value = mock_service
    
    # Mock get_all_ids inside executor
    executor = SheetsExecutor(mock_token, "fake_spread_id", "SD")
    
    with patch.object(executor, "get_all_ids", return_value=["FFC-SD-001", "FFC-SD-002"]):
        with patch.object(executor, "detect_prefix", return_value="FFC"):
            next_id = executor.next_ricefw_id("SD")
            assert next_id == "FFC-SD-003"

@patch("src.sheets.executor.build_sheets_service")
def test_next_id_unprefixed(mock_build, mock_token):
    mock_service = MagicMock()
    mock_build.return_value = mock_service
    
    executor = SheetsExecutor(mock_token, "fake_spread_id", "SD")
    
    with patch.object(executor, "get_all_ids", return_value=["SD-001", "SD-002"]):
        with patch.object(executor, "detect_prefix", return_value=""):
            next_id = executor.next_ricefw_id("SD")
            assert next_id == "SD-003"

@patch("src.sheets.executor.build_sheets_service")
def test_next_id_empty(mock_build, mock_token):
    mock_service = MagicMock()
    mock_build.return_value = mock_service
    
    executor = SheetsExecutor(mock_token, "fake_spread_id", "SD")
    
    with patch.object(executor, "get_all_ids", return_value=[]):
        next_id = executor.next_ricefw_id("SD", prefix="FFC")
        assert next_id == "FFC-SD-001"

@patch("src.sheets.executor.build_sheets_service")
def test_next_id_mixed_modules(mock_build, mock_token):
    mock_service = MagicMock()
    mock_build.return_value = mock_service
    
    executor = SheetsExecutor(mock_token, "fake_spread_id", "SD")
    
    # Should only increment for module SD, ignoring MM
    with patch.object(executor, "get_all_ids", return_value=["FFC-SD-001", "FFC-MM-005", "FFC-SD-002"]):
        next_id = executor.next_ricefw_id("SD", prefix="FFC")
        assert next_id == "FFC-SD-003"

@patch("src.sheets.executor.build_sheets_service")
def test_detect_prefix_standard(mock_build, mock_token):
    mock_service = MagicMock()
    mock_build.return_value = mock_service
    
    executor = SheetsExecutor(mock_token, "fake_spread_id", "SD")
    
    with patch.object(executor, "get_all_ids", return_value=["FFC-SD-001", "FFC-SD-002"]):
        prefix = executor.detect_prefix()
        assert prefix == "FFC"

@patch("src.sheets.executor.build_sheets_service")
def test_detect_prefix_none(mock_build, mock_token):
    mock_service = MagicMock()
    mock_build.return_value = mock_service
    
    executor = SheetsExecutor(mock_token, "fake_spread_id", "SD")
    
    with patch.object(executor, "get_all_ids", return_value=["SD-001", "SD-002"]):
        prefix = executor.detect_prefix()
        assert prefix == ""
