"""
src/data_quality.py
───────────────────
Phase 6 — Data Quality Checker

Evaluates spreadsheet rows for blank values, staleness against audit log timestamps,
and flags logical contradictions (e.g. Completed but no signoff date).
"""

import datetime
from typing import List, Dict, Any, Optional

class DataQualityChecker:
    """
    Analyzes row data to compile metrics on blank fields, detect stale rows
    without recent modifications, and run consistency rule checks.
    """
    
    def __init__(self, headers: List[str], rows: List[List[str]]):
        self.headers = [h.strip() for h in headers]
        self.rows = rows
        # Normalized mapping for case-insensitive lookup
        self._header_idx = {h.lower(): i for i, h in enumerate(self.headers)}

    def _get_col_idx(self, field: str) -> Optional[int]:
        """Resolve a column name to its 0-based index."""
        return self._header_idx.get(field.lower().strip())

    def blank_field_counts(self, fields: List[str]) -> Dict[str, int]:
        """
        Count blank rows for each specified column name.
        Returns a dict mapping field name -> blank count.
        """
        counts = {}
        for f in fields:
            idx = self._get_col_idx(f)
            if idx is None:
                counts[f] = len(self.rows)  # if column not found, assume all are blank
                continue
                
            blank_cnt = 0
            for row in self.rows:
                if idx >= len(row) or not row[idx].strip():
                    blank_cnt += 1
            counts[f] = blank_cnt
        return counts

    def stale_items(self, audit_entries: List[Dict[str, Any]], threshold_days: int = 30) -> List[Dict[str, Any]]:
        """
        Identify active (non-completed) items that have not had modifications
        in the audit log for threshold_days.
        """
        latest_mutation: Dict[str, datetime.datetime] = {}
        for entry in audit_entries:
            rid = entry.get("ricefw_id", "").strip().upper()
            ts_str = entry.get("timestamp", "")
            if not rid or not ts_str:
                continue
            try:
                # Expecting format like "2026-06-08T15:10:41.123456"
                ts = datetime.datetime.fromisoformat(ts_str.split(".")[0])
                if rid not in latest_mutation or ts > latest_mutation[rid]:
                    latest_mutation[rid] = ts
            except Exception:
                continue

        now = datetime.datetime.utcnow()
        stale = []
        id_idx = self._get_col_idx("RICEFW ID")
        status_idx = self._get_col_idx("Dev Status")
        module_idx = self._get_col_idx("Module")

        if id_idx is None:
            return []

        for row in self.rows:
            if id_idx >= len(row) or not row[id_idx].strip():
                continue
            rid = row[id_idx].strip().upper()
            status = row[status_idx].strip().lower() if (status_idx is not None and status_idx < len(row)) else ""
            module = row[module_idx].strip() if (module_idx is not None and module_idx < len(row)) else ""

            # Exclude completed/archived items
            if status in ("complete", "completed", "done", "closed", "retired"):
                continue

            last_ts = latest_mutation.get(rid)
            if last_ts:
                delta_days = (now - last_ts).days
                if delta_days >= threshold_days:
                    stale.append({
                        "ricefw_id": rid,
                        "module": module,
                        "status": row[status_idx].strip() if (status_idx is not None and status_idx < len(row)) else "",
                        "last_active": last_ts.strftime("%Y-%m-%d"),
                        "days_inactive": delta_days
                    })
            else:
                stale.append({
                    "ricefw_id": rid,
                    "module": module,
                    "status": row[status_idx].strip() if (status_idx is not None and status_idx < len(row)) else "",
                    "last_active": "Never (no logs)",
                    "days_inactive": 999
                })

        # Sort by longest inactive first
        stale.sort(key=lambda x: -x["days_inactive"])
        return stale

    def consistency_checks(self, valid_emails: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Check for logical errors and mismatch anomalies in the data.
        Returns a list of alerts: [{"severity": str, "message": str, "ids": list}].
        """
        alerts = []
        id_idx = self._get_col_idx("RICEFW ID")
        status_idx = self._get_col_idx("Dev Status")
        signoff_idx = self._get_col_idx("Sign-Off Date")
        completion_idx = self._get_col_idx("Completion Date")
        assigned_idx = self._get_col_idx("Assigned To")
        required_idx = self._get_col_idx("Required")

        if id_idx is None:
            return []

        # 1. Status is Completed/Done but Sign-Off Date is blank
        completed_no_signoff = []
        if status_idx is not None and signoff_idx is not None:
            for row in self.rows:
                if len(row) <= max(status_idx, signoff_idx, id_idx):
                    continue
                rid = row[id_idx].strip()
                status = row[status_idx].strip().lower()
                signoff = row[signoff_idx].strip()
                if rid and status in ("completed", "complete", "done") and not signoff:
                    completed_no_signoff.append(rid)
        if completed_no_signoff:
            alerts.append({
                "severity": "warning",
                "message": "Completed items missing 'Sign-Off Date'",
                "ids": completed_no_signoff
            })

        # 2. Status is Completed/Done but Completion Date is blank
        completed_no_completion = []
        if status_idx is not None and completion_idx is not None:
            for row in self.rows:
                if len(row) <= max(status_idx, completion_idx, id_idx):
                    continue
                rid = row[id_idx].strip()
                status = row[status_idx].strip().lower()
                completion = row[completion_idx].strip()
                if rid and status in ("completed", "complete", "done") and not completion:
                    completed_no_completion.append(rid)
        if completed_no_completion:
            alerts.append({
                "severity": "warning",
                "message": "Completed items missing 'Completion Date'",
                "ids": completed_no_completion
            })

        # 3. Item is Required=Yes but Dev Status is empty
        required_no_status = []
        if required_idx is not None and status_idx is not None:
            for row in self.rows:
                if len(row) <= max(required_idx, status_idx, id_idx):
                    continue
                rid = row[id_idx].strip()
                required = row[required_idx].strip().lower()
                status = row[status_idx].strip()
                if rid and required in ("yes", "true") and not status:
                    required_no_status.append(rid)
        if required_no_status:
            alerts.append({
                "severity": "error",
                "message": "Required items with blank 'Dev Status'",
                "ids": required_no_status
            })

        # 4. Assigned to email is unregistered (not in Permissions registry)
        unregistered_assignee = []
        if assigned_idx is not None and valid_emails:
            valid_set = {email.lower().strip() for email in valid_emails}
            for row in self.rows:
                if len(row) <= max(assigned_idx, id_idx):
                    continue
                rid = row[id_idx].strip()
                assignee = row[assigned_idx].strip().lower()
                if rid and assignee and assignee not in valid_set and "@" in assignee:
                    unregistered_assignee.append(rid)
        if unregistered_assignee:
            alerts.append({
                "severity": "warning",
                "message": "Assigned to users not registered in permissions",
                "ids": unregistered_assignee
            })

        return alerts

    def completeness_score(self) -> float:
        """
        Evaluate critical cell fill rate across standard RICEFW columns.
        Returns a score from 0.0 to 100.0.
        """
        critical_fields = ["RICEFW ID", "Module", "Type", "Description", "Dev Status", "Assigned To"]
        indices = [self._get_col_idx(f) for f in critical_fields]
        valid_indices = [idx for idx in indices if idx is not None]

        if not self.rows or not valid_indices:
            return 100.0

        total_cells = len(self.rows) * len(valid_indices)
        filled_cells = 0

        for row in self.rows:
            for idx in valid_indices:
                if idx < len(row) and row[idx].strip():
                    filled_cells += 1

        return round((filled_cells / total_cells) * 100.0, 1)
