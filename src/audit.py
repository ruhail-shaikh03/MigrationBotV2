"""
src/audit.py
────────────
F15 — Persistent Audit Log

Writes every write-operation mutation to a dedicated tab in the config sheet.
Read operations (get_row, search_rows, summarize) are never logged — they
produce no mutations and would create noise.

Design principles from the TDD:
  - Non-blocking: audit write failures are caught and printed, never surfaced
    to the user or allowed to block the actual operation.
  - Old-value capture: update_cell fetches the current value before writing.
    bulk_update does a single batch read of all affected rows beforehand.
  - Session ID: groups all actions in one browser session for incident tracing.
  - Schema: fixed 13-column layout written to "MigrationBot Audit Log" tab.
"""

import json
import datetime
import streamlit as st
from googleapiclient.errors import HttpError
from src.sheets_auth import build_sheets_service

AUDIT_TAB = "MigrationBot Audit Log"

AUDIT_HEADERS = [
    "timestamp", "user_email", "session_id", "tool_name",
    "spreadsheet_id", "sheet_tab", "ricefw_id", "field",
    "old_value", "new_value", "args_json", "result_ok", "error",
]

# Tools whose mutations must be logged
LOGGABLE_TOOLS = {"update_cell", "bulk_update", "format_row", "add_row"}


class AuditLogger:
    """
    Writes audit rows to the MigrationBot Audit Log tab in the config sheet.

    Instantiated once per session in app.py alongside the executor.
    Falls back to no-op logging if config_sheet_id is not configured.
    """

    def __init__(self, token_dict: dict, config_sheet_id: str | None):
        self.config_sheet_id = config_sheet_id
        self._service        = None

        if config_sheet_id:
            try:
                self._service = build_sheets_service(token_dict)
                self._ensure_tab_exists()
            except Exception as e:
                # Non-fatal — audit setup failure must never crash the app
                print(f"[AUDIT INIT FAILED] {e}")
                self._service = None

    # ── Setup ─────────────────────────────────────────────────────────────────

    def _ensure_tab_exists(self) -> None:
        """Create the audit tab with headers if it doesn't already exist."""
        meta = self._service.spreadsheets().get(
            spreadsheetId=self.config_sheet_id,
            fields="sheets.properties",
        ).execute()
        tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]

        if AUDIT_TAB not in tabs:
            self._service.spreadsheets().batchUpdate(
                spreadsheetId=self.config_sheet_id,
                body={"requests": [
                    {"addSheet": {"properties": {"title": AUDIT_TAB}}}
                ]},
            ).execute()
            self._service.spreadsheets().values().update(
                spreadsheetId=self.config_sheet_id,
                range=f"{AUDIT_TAB}!1:1",
                valueInputOption="RAW",
                body={"values": [AUDIT_HEADERS]},
            ).execute()

    # ── Core log method ───────────────────────────────────────────────────────

    def log(
        self,
        tool_name:      str,
        spreadsheet_id: str,
        sheet_tab:      str,
        ricefw_id:      str  = "",
        field:          str  = "",
        old_value:      str  = "",
        new_value:      str  = "",
        args_json:      str  = "",
        result_ok:      bool = True,
        error:          str  = "",
    ) -> None:
        """
        Append one audit row. Non-blocking — all exceptions are caught.
        Silently skips if no config sheet is configured.
        """
        if not self._service or not self.config_sheet_id:
            return

        row = [
            datetime.datetime.utcnow().isoformat(),
            st.session_state.get("user_email", ""),
            st.session_state.get("session_id", ""),
            tool_name,
            spreadsheet_id,
            sheet_tab,
            ricefw_id,
            field,
            old_value,
            new_value,
            args_json,
            str(result_ok),
            error,
        ]

        try:
            self._service.spreadsheets().values().append(
                spreadsheetId=self.config_sheet_id,
                range=f"{AUDIT_TAB}!A:A",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [row]},
            ).execute()
        except Exception as e:
            # Non-blocking — log to Streamlit Cloud stdout but never surface
            print(f"[AUDIT WRITE FAILED] {e}")

    # ── Convenience wrappers per tool ─────────────────────────────────────────

    def log_update_cell(
        self,
        spreadsheet_id: str,
        sheet_tab:      str,
        ricefw_id:      str,
        field:          str,
        old_value:      str,
        new_value:      str,
        result:         dict,
    ) -> None:
        self.log(
            tool_name      = "update_cell",
            spreadsheet_id = spreadsheet_id,
            sheet_tab      = sheet_tab,
            ricefw_id      = ricefw_id,
            field          = field,
            old_value      = old_value,
            new_value      = new_value,
            args_json      = json.dumps({"ricefw_id": ricefw_id,
                                         "field": field, "value": new_value}),
            result_ok      = result.get("ok", False),
            error          = result.get("error", ""),
        )

    def log_bulk_update(
        self,
        spreadsheet_id: str,
        sheet_tab:      str,
        args:           dict,
        result:         dict,
    ) -> None:
        """Log one row per successfully updated RICEFW ID."""
        succeeded = result.get("succeeded", [])
        failed    = result.get("failed",    [])

        for rid in succeeded:
            self.log(
                tool_name      = "bulk_update",
                spreadsheet_id = spreadsheet_id,
                sheet_tab      = sheet_tab,
                ricefw_id      = rid,
                field          = args.get("set_field", ""),
                new_value      = args.get("set_value", ""),
                args_json      = json.dumps(args),
                result_ok      = True,
            )
        for f in failed:
            self.log(
                tool_name      = "bulk_update",
                spreadsheet_id = spreadsheet_id,
                sheet_tab      = sheet_tab,
                ricefw_id      = f.get("id", ""),
                field          = args.get("set_field", ""),
                new_value      = args.get("set_value", ""),
                args_json      = json.dumps(args),
                result_ok      = False,
                error          = f.get("error", ""),
            )

    def log_format_row(
        self,
        spreadsheet_id: str,
        sheet_tab:      str,
        args:           dict,
        result:         dict,
    ) -> None:
        self.log(
            tool_name      = "format_row",
            spreadsheet_id = spreadsheet_id,
            sheet_tab      = sheet_tab,
            ricefw_id      = args.get("ricefw_id", ""),
            field          = "Color",
            new_value      = f"{args.get('color','')} / {args.get('scope','')}",
            args_json      = json.dumps(args),
            result_ok      = result.get("ok", False),
            error          = result.get("error", ""),
        )

    def log_add_row(
        self,
        spreadsheet_id: str,
        sheet_tab:      str,
        ricefw_id:      str,
        args:           dict,
        result:         dict,
    ) -> None:
        self.log(
            tool_name      = "add_row",
            spreadsheet_id = spreadsheet_id,
            sheet_tab      = sheet_tab,
            ricefw_id      = ricefw_id,
            args_json      = json.dumps(args),
            result_ok      = result.get("ok", False),
            error          = result.get("error", ""),
        )

    # ── Admin: read the log back ──────────────────────────────────────────────

    def fetch_log(self, max_rows: int = 500) -> list[dict]:
        """
        Fetch up to max_rows audit rows (most recent first).
        Returns a list of dicts keyed by AUDIT_HEADERS.
        Returns [] if the log is empty or config sheet is unavailable.
        """
        if not self._service or not self.config_sheet_id:
            return []

        try:
            result = self._service.spreadsheets().values().get(
                spreadsheetId=self.config_sheet_id,
                range=f"{AUDIT_TAB}!A:M",
            ).execute()
            rows = result.get("values", [])
            if len(rows) < 2:
                return []

            # Skip header row, pad short rows, reverse so newest is first
            data = []
            for row in rows[1:]:
                while len(row) < len(AUDIT_HEADERS):
                    row.append("")
                data.append(dict(zip(AUDIT_HEADERS, row)))

            return list(reversed(data))[:max_rows]

        except Exception as e:
            print(f"[AUDIT FETCH FAILED] {e}")
            return []


# ── Session-level helper ──────────────────────────────────────────────────────

def ensure_audit_logger(token_dict: dict) -> None:
    """
    Initialise the AuditLogger once per session and store in session_state.
    Subsequent calls are instant cache hits.
    """
    if "audit_logger" in st.session_state:
        return
    config_sheet_id = st.secrets.get("app", {}).get("config_sheet_id")
    st.session_state["audit_logger"] = AuditLogger(token_dict, config_sheet_id)


def get_audit_logger() -> AuditLogger | None:
    return st.session_state.get("audit_logger")