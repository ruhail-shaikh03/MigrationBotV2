import pytest
import datetime
from src.data_quality import DataQualityChecker

def test_blank_counts():
    headers = ["RICEFW ID", "Module", "Dev Status", "Assigned To"]
    rows = [
        ["FFC-SD-001", "SD", "In Progress", "user1@example.com"],
        ["FFC-SD-002", "SD", "", "user2@example.com"],
        ["FFC-SD-003", "SD", "", ""],
        ["FFC-SD-004", "SD", "Done", ""]
    ]
    
    checker = DataQualityChecker(headers, rows)
    counts = checker.blank_field_counts(["Dev Status", "Assigned To"])
    assert counts["Dev Status"] == 2
    assert counts["Assigned To"] == 2

def test_stale_items():
    headers = ["RICEFW ID", "Module", "Dev Status"]
    rows = [
        ["FFC-SD-001", "SD", "In Progress"],
        ["FFC-SD-002", "SD", "Completed"],  # Completed should be ignored
        ["FFC-SD-003", "SD", "New"]
    ]
    
    # Current date mock: 2026-06-08
    # FFC-SD-001 last update was 2026-04-01 (older than 30 days)
    # FFC-SD-003 has no audit log
    audit_entries = [
        {"ricefw_id": "FFC-SD-001", "timestamp": "2026-04-01T12:00:00.000000"},
        {"ricefw_id": "FFC-SD-002", "timestamp": "2026-04-01T12:00:00.000000"}
    ]
    
    checker = DataQualityChecker(headers, rows)
    stale = checker.stale_items(audit_entries, threshold_days=30)
    
    # SD-003 has "Never (no logs)"
    # SD-001 has ~68 days inactive (since 2026-06-08 is current date, we compare with datetime.datetime.utcnow())
    assert len(stale) == 2
    stale_ids = [s["ricefw_id"] for s in stale]
    assert "FFC-SD-001" in stale_ids
    assert "FFC-SD-003" in stale_ids

def test_consistency_completed_no_signoff():
    headers = ["RICEFW ID", "Dev Status", "Sign-Off Date"]
    rows = [
        ["FFC-SD-001", "Completed", ""],
        ["FFC-SD-002", "Completed", "2026-06-01"],
        ["FFC-SD-003", "In Progress", ""]
    ]
    
    checker = DataQualityChecker(headers, rows)
    alerts = checker.consistency_checks()
    
    completed_no_signoff_alerts = [a for a in alerts if "Completed items missing 'Sign-Off Date'" in a["message"]]
    assert len(completed_no_signoff_alerts) == 1
    assert completed_no_signoff_alerts[0]["ids"] == ["FFC-SD-001"]

def test_consistency_start_no_end():
    # The requirement is: "Completion Date blank when Completed" or similar
    # Let's test checking completion date when completed
    headers = ["RICEFW ID", "Dev Status", "Completion Date"]
    rows = [
        ["FFC-SD-001", "Completed", ""],
        ["FFC-SD-002", "Completed", "2026-06-01"],
        ["FFC-SD-003", "In Progress", ""]
    ]
    
    checker = DataQualityChecker(headers, rows)
    alerts = checker.consistency_checks()
    
    completed_no_completion_alerts = [a for a in alerts if "Completed items missing 'Completion Date'" in a["message"]]
    assert len(completed_no_completion_alerts) == 1
    assert completed_no_completion_alerts[0]["ids"] == ["FFC-SD-001"]

def test_completeness_score():
    headers = ["RICEFW ID", "Module", "Type", "Description", "Dev Status", "Assigned To"]
    # 6 critical columns. 1 row. All 6 filled = 100%
    rows = [
        ["FFC-SD-001", "SD", "R", "Description", "Done", "user@example.com"]
    ]
    checker = DataQualityChecker(headers, rows)
    assert checker.completeness_score() == 100.0

def test_completeness_score_partial():
    headers = ["RICEFW ID", "Module", "Type", "Description", "Dev Status", "Assigned To"]
    # 6 critical columns. 1 row. 3 filled, 3 blank = 50%
    rows = [
        ["FFC-SD-001", "SD", "R", "", "", ""]
    ]
    checker = DataQualityChecker(headers, rows)
    assert checker.completeness_score() == 50.0
