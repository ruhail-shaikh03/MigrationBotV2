import datetime
from typing import List, Dict, Any, Optional

from app.core.overdue import is_finished_status
from app.core.schema import default_critical_headers, get_people_columns


class DataQualityChecker:
    """
    Analyzes row data to compile metrics on blank fields, detect stale rows
    without recent modifications, and run consistency rule checks. Uses schema_config.

    Every column this class looks at comes from `schema_config`. It previously carried
    literal fallbacks from one customer's WRICEF tracker — `"RICEFW ID"`, `"Dev Status"`,
    `"Technical Resource "`, `"Sign-Off Date"` — which on any other sheet resolved to
    nothing and made the checks silently pass. A check that cannot find its column now
    says so (`skipped`), because "no alerts" and "not checked" must not look the same.
    """
    def __init__(self, headers: List[str], rows: List[List[str]], schema_config: Optional[dict] = None):
        self.headers = [h.strip() for h in headers]
        self.rows = rows
        self._header_idx = {h.lower(): i for i, h in enumerate(self.headers)}
        self.schema = schema_config or {}
        # Populated by consistency_checks: the checks that had no column to run against.
        self.skipped: List[str] = []

    def _get_col_idx(self, field: Optional[str]) -> Optional[int]:
        if not field:
            return None
        return self._header_idx.get(field.lower().strip())

    def _schema_header(self, *keys: str) -> Optional[str]:
        """The first schema-declared header among `keys` that this sheet actually has.

        Returns None rather than a guess: a caller that gets None must skip its check,
        which is the whole point — inventing a header name is how the SAP literals used
        to make an unrelated tracker look clean.
        """
        for key in keys:
            value = self.schema.get(key)
            if isinstance(value, str) and self._get_col_idx(value) is not None:
                return value
        return None

    def _date_header(self, kind: str) -> Optional[str]:
        """A date column by its schema role (`signoff`, `completion`), if declared."""
        value = (self.schema.get("date_columns") or {}).get(kind)
        if isinstance(value, str) and self._get_col_idx(value) is not None:
            return value
        return None

    def _people_headers(self) -> List[str]:
        """Every people column this sheet declares, newest schema shape first.

        `people_columns` replaced the single `assignee_column` in §7.4; un-migrated
        projects still carry only the latter, so both are read.
        """
        headers = [
            p["header"] for p in get_people_columns(self.schema)
            if p.get("header") and self._get_col_idx(p["header"]) is not None
        ]
        if headers:
            return headers
        single = self._schema_header("assignee_column")
        return [single] if single else []

    def blank_field_counts(self, fields: List[str]) -> Dict[str, int]:
        """Count blank rows for each specified column name."""
        counts = {}
        for f in fields:
            idx = self._get_col_idx(f)
            if idx is None:
                counts[f] = len(self.rows)
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
            rid = entry.get("ricefw_id")
            if not isinstance(rid, str):
                continue
            rid = rid.strip().upper()
            ts_str = entry.get("timestamp")
            if not rid or not ts_str:
                continue
            try:
                if isinstance(ts_str, datetime.datetime):
                    ts = ts_str.replace(tzinfo=None)
                else:
                    ts = datetime.datetime.fromisoformat(str(ts_str).split("+")[0].split(".")[0])
                if rid not in latest_mutation or ts > latest_mutation[rid]:
                    latest_mutation[rid] = ts
            except Exception:
                continue

        # Fixed: Avoid utcnow() deprecation in Python 3.12+
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        stale = []
        
        primary_id_col = self._schema_header("primary_id_column")
        status_col = self._schema_header("status_column")
        module_col = self._schema_header("module_column")

        id_idx = self._get_col_idx(primary_id_col)
        status_idx = self._get_col_idx(status_col)
        module_idx = self._get_col_idx(module_col)

        if id_idx is None:
            return []

        for row in self.rows:
            if id_idx >= len(row) or not row[id_idx].strip():
                continue
            rid = row[id_idx].strip().upper()
            status = row[status_idx].strip().lower() if (status_idx is not None and status_idx < len(row)) else ""
            module = row[module_idx].strip() if (module_idx is not None and module_idx < len(row)) else ""

            # Exclude finished items. The vocabulary lives in `core/overdue.py`, shared
            # with the dashboard and `summarize(overdue)` — three copies of "what counts
            # as done" is how they drifted apart before (§16.6).
            if is_finished_status(status):
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

        stale.sort(key=lambda x: -x["days_inactive"])
        return stale

    def consistency_checks(self, valid_emails: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Check for logical errors and mismatch anomalies in the data using schema settings.
        """
        alerts: List[Dict[str, Any]] = []
        self.skipped = []

        primary_id_col = self._schema_header("primary_id_column")
        status_col = self._schema_header("status_column")
        signoff_col = self._date_header("signoff")
        completion_col = self._date_header("completion")
        people_cols = self._people_headers()
        # Not a schema role anywhere — it only ever existed as a literal on the reference
        # tracker, so it is treated as an optional column that most sheets will not have.
        required_col = "Required" if self._get_col_idx("Required") is not None else None

        id_idx = self._get_col_idx(primary_id_col)
        status_idx = self._get_col_idx(status_col)
        signoff_idx = self._get_col_idx(signoff_col)
        completion_idx = self._get_col_idx(completion_col)
        required_idx = self._get_col_idx(required_col)

        if id_idx is None:
            # Without an ID column there is nothing to name in an alert. Say so rather
            # than returning an empty list a caller would read as "no problems found".
            self.skipped = ["every check — no primary ID column is declared for this tab"]
            return []

        alerts.extend(self._duplicate_id_alerts(id_idx, primary_id_col))

        # 1. Status reads as finished but Sign-Off Date is blank
        completed_no_signoff = []
        if status_idx is None or signoff_idx is None:
            self.skipped.append("finished-without-sign-off — needs a status and a sign-off date column")
        else:
            for row in self.rows:
                if len(row) <= max(status_idx, signoff_idx, id_idx):
                    continue
                rid = row[id_idx].strip()
                signoff = row[signoff_idx].strip()
                if rid and is_finished_status(row[status_idx]) and not signoff:
                    completed_no_signoff.append(rid)
        if completed_no_signoff:
            alerts.append({
                "severity": "warning",
                "message": f"Completed items missing '{signoff_col}'",
                "ids": completed_no_signoff
            })

        # 2. Status reads as finished but Completion Date is blank
        completed_no_completion = []
        if status_idx is None or completion_idx is None:
            self.skipped.append("finished-without-completion-date — needs a status and a completion date column")
        else:
            for row in self.rows:
                if len(row) <= max(status_idx, completion_idx, id_idx):
                    continue
                rid = row[id_idx].strip()
                completion = row[completion_idx].strip()
                if rid and is_finished_status(row[status_idx]) and not completion:
                    completed_no_completion.append(rid)
        if completed_no_completion:
            alerts.append({
                "severity": "warning",
                "message": f"Completed items missing '{completion_col}'",
                "ids": completed_no_completion
            })

        # 3. Item is Required but its status is empty
        required_no_status = []
        if required_idx is None or status_idx is None:
            self.skipped.append("required-without-status — needs a 'Required' and a status column")
        else:
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
                "message": f"Required items with blank '{status_col}'",
                "ids": required_no_status
            })

        # 4. Assigned user is unregistered (not in Permissions registry). Every people
        # column is checked, not just one: a sheet can name a functional consultant and a
        # developer in separate columns, and only looking at one hides half the sheet.
        unregistered_assignee = []
        if not people_cols:
            self.skipped.append("unregistered-assignee — no people column is declared for this tab")
        elif valid_emails:
            valid_set = {email.lower().strip() for email in valid_emails}
            for header in people_cols:
                assigned_idx = self._get_col_idx(header)
                if assigned_idx is None:
                    continue
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
                "message": (
                    f"Assigned users in {', '.join(repr(h) for h in people_cols)} "
                    "not registered in permissions"
                ),
                "ids": sorted(set(unregistered_assignee))
            })

        if self.skipped:
            # Surfaced as an alert rather than logged, so a reader cannot mistake a
            # partial run for a clean one — the failure mode `core/health.py` was built
            # to avoid reproducing (§10.4).
            alerts.append({
                "severity": "info",
                "message": "Checks not run on this tab: " + "; ".join(self.skipped),
                "ids": []
            })

        return alerts

    def _duplicate_id_alerts(self, id_idx: int, primary_id_col: Optional[str]) -> List[Dict[str, Any]]:
        """IDs that name more than one row.

        The same condition `core/health.py:assess_tab` surfaces on the dashboard, carried
        here so the agent's `data_quality` tool sees it too. It is not cosmetic: a write
        against a duplicated ID is refused outright (§16.7), so a user asking why an edit
        will not apply needs this check to be able to tell them.
        """
        seen: Dict[str, List[str]] = {}
        for row in self.rows:
            if id_idx >= len(row):
                continue
            value = row[id_idx].strip()
            if value:
                seen.setdefault(value.casefold(), []).append(value)

        dupes = sorted({v[0] for v in seen.values() if len(v) > 1})
        if not dupes:
            return []
        return [{
            "severity": "error",
            "message": f"Duplicated '{primary_id_col}' values — writes against these are refused",
            "ids": dupes,
        }]

    def default_critical_fields(self) -> List[str]:
        """The columns worth measuring fill rate on, when the schema names none explicitly.

        Every entry is a header this sheet actually has, resolved from schema roles. The
        old version listed one customer's literal headers, so on any other tracker the
        score was computed over columns that did not exist — and `read.py`'s
        `blank_fields` default carried the same list a second time.
        """
        return default_critical_headers(self.schema, self.headers)

    def completeness_score(self) -> float:
        """Evaluate critical cell fill rate across columns specified in schema."""
        critical_fields = self.schema.get("critical_fields") or self.default_critical_fields()

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
