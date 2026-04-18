import asyncio
import streamlit as st
from googleapiclient.errors import HttpError
from src.sheets_auth import build_sheets_service

class SheetsExecutor:
    SHEET_NAME     = None   # loaded from secrets at runtime
    DATA_START_ROW = 3      # Row 1 = section headers, Row 2 = column headers
    COLOR_INDEX    = 50     # 0-based index of your Color column — adjust if needed

    COLOR_MAP = {
        "red":   {"red": 0.96, "green": 0.80, "blue": 0.80},
        "green": {"red": 0.78, "green": 0.93, "blue": 0.78},
        "amber": {"red": 1.0,  "green": 0.90, "blue": 0.60},
        "blue":  {"red": 0.78, "green": 0.87, "blue": 0.96},
        "white": {"red": 1.0,  "green": 1.0,  "blue": 1.0 },
    }

    def __init__(self, access_token: str):
        self.service = build_sheets_service(access_token)
        self.spreadsheet_id = st.secrets["app"]["spreadsheet_id"]
        self.SHEET_NAME     = st.secrets["app"]["sheet_tab_name"]
        self._sheet_id_cache: int | None = None

    # ── Internal helpers ────────────────────────────────────────────

    def _get_sheet_id(self) -> int:
        if self._sheet_id_cache is not None:
            return self._sheet_id_cache
        meta = self.service.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id
        ).execute()
        for sheet in meta["sheets"]:
            if sheet["properties"]["title"] == self.SHEET_NAME:
                self._sheet_id_cache = sheet["properties"]["sheetId"]
                return self._sheet_id_cache
        raise ValueError(f"Sheet tab '{self.SHEET_NAME}' not found. "
                         f"Check sheet_tab_name in secrets.toml.")

    def _idx_to_col_letter(self, idx: int) -> str:
        result = ""
        while idx >= 0:
            result = chr(idx % 26 + ord("A")) + result
            idx = idx // 26 - 1
        return result

    def _get_header_row(self) -> list[str]:
        result = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f"{self.SHEET_NAME}!2:2"
        ).execute()
        return result.get("values", [[]])[0]

    def _resolve_col_index(self, field: str) -> int | None:
        headers    = self._get_header_row()
        field_low  = field.lower().strip()
        for i, h in enumerate(headers):
            if h.lower().strip() == field_low:
                return i
        return None

    # ── Public API ──────────────────────────────────────────────────

    def find_row(self, ricefw_id: str) -> int | None:
        result = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f"{self.SHEET_NAME}!B{self.DATA_START_ROW}:B"
        ).execute()
        for i, row in enumerate(result.get("values", [])):
            if row and row[0].strip().upper() == ricefw_id.upper():
                return self.DATA_START_ROW + i
        return None

    def get_all_ids(self) -> list[str]:
        result = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f"{self.SHEET_NAME}!B{self.DATA_START_ROW}:B"
        ).execute()
        return [r[0] for r in result.get("values", []) if r]

    def get_row(self, ricefw_id: str, fields: list[str] | None = None) -> dict:
        row_num = self.find_row(ricefw_id)
        if row_num is None:
            return {"ok": False, "error": f"{ricefw_id} not found in sheet"}
        result = self.service.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id,
            range=f"{self.SHEET_NAME}!{row_num}:{row_num}"
        ).execute()
        headers  = self._get_header_row()
        values   = result.get("values", [[]])[0]
        row_data = {h: (values[i] if i < len(values) else "") for i, h in enumerate(headers)}
        if fields:
            row_data = {k: v for k, v in row_data.items() if k in fields}
        return {"ok": True, "ricefw_id": ricefw_id, "data": row_data}

    def update_cell(self, ricefw_id: str, field: str, value: str) -> dict:
        row_num = self.find_row(ricefw_id)
        if row_num is None:
            return {"ok": False, "error": f"{ricefw_id} not found"}
        col_idx = self._resolve_col_index(field)
        if col_idx is None:
            return {"ok": False, "error": f"Column '{field}' not found in header row"}
        col_letter = self._idx_to_col_letter(col_idx)
        cell_range = f"{self.SHEET_NAME}!{col_letter}{row_num}"
        self.service.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=cell_range,
            valueInputOption="USER_ENTERED",
            body={"values": [[value]]}
        ).execute()
        return {"ok": True, "updated_range": cell_range, "value": value}

    def format_row(self, ricefw_id: str, color: str,
                   scope: str = "color_column_only") -> dict:
        row_num = self.find_row(ricefw_id)
        if row_num is None:
            return {"ok": False, "error": f"{ricefw_id} not found"}
        rgb = self.COLOR_MAP.get(color, self.COLOR_MAP["white"])
        start_col, end_col = (0, 51) if scope == "entire_row" \
                              else (self.COLOR_INDEX, self.COLOR_INDEX)
        body = {"requests": [{
            "repeatCell": {
                "range": {
                    "sheetId": self._get_sheet_id(),
                    "startRowIndex": row_num - 1,
                    "endRowIndex":   row_num,
                    "startColumnIndex": start_col,
                    "endColumnIndex":   end_col + 1
                },
                "cell":   {"userEnteredFormat": {"backgroundColor": rgb}},
                "fields": "userEnteredFormat.backgroundColor"
            }
        }]}
        self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id, body=body
        ).execute()
        return {"ok": True, "formatted": ricefw_id, "color": color, "scope": scope}

    def next_ricefw_id(self, module: str) -> str:
        all_ids = self.get_all_ids()
        nums = []
        for i in all_ids:
            parts = i.split("-")
            if len(parts) == 2 and parts[0] == module and parts[1].isdigit():
                nums.append(int(parts[1]))
        return f"{module}-{(max(nums) + 1) if nums else 1:03d}"

    def add_row(self, ricefw_id: str, module: str, type: str,
                description: str, assigned_to: str = "",
                fields: dict | None = None) -> dict:
        headers = self._get_header_row()
        row = [""] * len(headers)
        field_map = {
            "RICEFW ID": ricefw_id,
            "Module": module,
            "Type": type,
            "Description": description,
            "Assigned To": assigned_to,
        }
        if fields:
            field_map.update(fields)
        for col_name, val in field_map.items():
            if col_name in headers:
                row[headers.index(col_name)] = val
        self.service.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=f"{self.SHEET_NAME}!A:A",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]}
        ).execute()
        return {"ok": True, "added": ricefw_id}