import pytest
from unittest.mock import patch, MagicMock
import streamlit as st
from src.permissions import PermissionChecker, PermissionsRegistry, ProjectPermissionsDict

@pytest.fixture
def mock_secrets():
    # Helper to mock st.secrets
    mock_data = {
        "app": {
            "admins": ["admin@example.com"],
            "config_sheet_id": "config_id"
        }
    }
    with patch.object(st, "secrets", mock_data):
        yield


def test_admin_bypass(mock_secrets):
    # Admin is always allowed to execute anything
    checker = PermissionChecker("admin@example.com", {})
    assert checker.is_admin() is True
    
    allowed, reason = checker.can_execute("update_cell", {"updates": [{"field": "Dev Status", "value": "Done"}]})
    assert allowed is True
    assert reason == ""

def test_viewer_read_ok(mock_secrets):
    # Viewer should be allowed to run get_row, search_rows, summarize, switch_module
    perms = {
        "viewer@example.com": {
            "*": {
                "role": "viewer",
                "allowed_fields": ["*"],
                "denied_operations": []
            }
        }
    }
    checker = PermissionChecker("viewer@example.com", perms)
    assert checker.is_admin() is False
    assert checker.role == "viewer"
    
    allowed, _ = checker.can_execute("get_row", {"ricefw_id": "SD-001"})
    assert allowed is True
    
    allowed, _ = checker.can_execute("switch_module", {"tab_name": "MM"})
    assert allowed is True

def test_viewer_write_blocked(mock_secrets):
    perms = {
        "viewer@example.com": {
            "*": {
                "role": "viewer",
                "allowed_fields": ["*"],
                "denied_operations": []
            }
        }
    }
    checker = PermissionChecker("viewer@example.com", perms)
    
    allowed, reason = checker.can_execute("update_cell", {"ricefw_id": "SD-001"})
    assert allowed is False
    assert "cannot run `update_cell`" in reason

def test_editor_field_restrict(mock_secrets):
    # Editor can update allowed fields, but not denied fields
    perms = {
        "editor@example.com": {
            "*": {
                "role": "editor",
                "allowed_fields": ["Dev Status"],
                "denied_operations": []
            }
        }
    }
    checker = PermissionChecker("editor@example.com", perms)
    assert checker.role == "editor"
    
    # Allowed field update
    allowed, _ = checker.can_execute("update_cell", {"updates": [{"field": "Dev Status", "value": "Done"}]})
    assert allowed is True
    
    # Denied field update
    allowed, reason = checker.can_execute("update_cell", {"updates": [{"field": "Description", "value": "New Description"}]})
    assert allowed is False
    assert "don't have write access to: **Description**" in reason

def test_editor_denied_op(mock_secrets):
    # Editor with blocked operations
    perms = {
        "editor@example.com": {
            "*": {
                "role": "editor",
                "allowed_fields": ["*"],
                "denied_operations": ["bulk_update"]
            }
        }
    }
    checker = PermissionChecker("editor@example.com", perms)
    
    allowed, reason = checker.can_execute("bulk_update", {"set_field": "Dev Status", "set_value": "Done"})
    assert allowed is False
    assert "don't have permission to run `bulk_update`" in reason

def test_project_scoped_admin_A(mock_secrets):
    # Project-specific rule: admin on Project A
    perms = {
        "user@example.com": {
            "Project A": {
                "role": "admin",
                "allowed_fields": ["*"],
                "denied_operations": []
            },
            "*": {
                "role": "viewer",
                "allowed_fields": ["*"],
                "denied_operations": []
            }
        }
    }
    
    # Active project is Project A
    checker = PermissionChecker("user@example.com", perms, active_project="Project A")
    assert checker.role == "admin"
    allowed, _ = checker.can_execute("update_cell", {"updates": [{"field": "Dev Status", "value": "Done"}]})
    assert allowed is True

def test_project_scoped_viewer_B(mock_secrets):
    # Viewer on Project B (even if wildcard is editor)
    perms = {
        "user@example.com": {
            "Project B": {
                "role": "viewer",
                "allowed_fields": ["*"],
                "denied_operations": []
            },
            "*": {
                "role": "editor",
                "allowed_fields": ["*"],
                "denied_operations": []
            }
        }
    }
    
    checker = PermissionChecker("user@example.com", perms, active_project="Project B")
    assert checker.role == "viewer"
    allowed, _ = checker.can_execute("update_cell", {"updates": [{"field": "Dev Status", "value": "Done"}]})
    assert allowed is False

def test_wildcard_fallback(mock_secrets):
    # Fallback to wildcard when project matches nothing specific
    perms = {
        "user@example.com": {
            "*": {
                "role": "editor",
                "allowed_fields": ["*"],
                "denied_operations": []
            }
        }
    }
    checker = PermissionChecker("user@example.com", perms, active_project="Project C")
    assert checker.role == "editor"

def test_specific_overrides_wildcard(mock_secrets):
    perms = {
        "user@example.com": {
            "Project B": {
                "role": "viewer",
                "allowed_fields": ["*"],
                "denied_operations": []
            },
            "*": {
                "role": "editor",
                "allowed_fields": ["*"],
                "denied_operations": []
            }
        }
    }
    checker = PermissionChecker("user@example.com", perms, active_project="Project B")
    assert checker.role == "viewer"

def test_switch_module_read_only(mock_secrets):
    # switch_module is ∈ READ_ONLY_TOOLS, so viewers can run it
    perms = {
        "viewer@example.com": {
            "*": {
                "role": "viewer",
                "allowed_fields": ["*"],
                "denied_operations": []
            }
        }
    }
    checker = PermissionChecker("viewer@example.com", perms)
    allowed, _ = checker.can_execute("switch_module", {"tab_name": "SD"})
    assert allowed is True

def test_backward_compat_4col(mock_secrets):
    # Legacy flat dict format wrapper check
    registry = PermissionsRegistry()
    registry["user@example.com"] = {
        "role": "editor",
        "allowed_fields": ["Dev Status"],
        "denied_operations": ["bulk_update"]
    }
    
    assert isinstance(registry["user@example.com"], ProjectPermissionsDict)
    assert registry["user@example.com"]["*"]["role"] == "editor"
