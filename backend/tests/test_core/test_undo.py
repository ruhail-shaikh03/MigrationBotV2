"""The safety guard on undo.

An audit row records what happened *then*. Between then and now someone else may have
edited the same cell, and restoring `old_value` blindly would destroy their edit while
reporting success — the person undoing would never learn of it, the person overwritten
would never learn of it, and the audit trail would show a legitimate-looking correction.
That is the one failure this endpoint must not have, so it reads before it writes.

These tests exercise `_verify_unchanged` directly rather than through the route, because
everything around it needs Postgres and there is none on this machine. The guard is the
part with the reasoning in it.
"""

import pytest
from fastapi import HTTPException

from app.api.audit import UNDOABLE_TOOLS, _normalise, _verify_unchanged


class FakeEntry:
    def __init__(self, new_value, field="Dev Status", ricefw_id="W-1"):
        self.id = 7
        self.spreadsheet_id = "sheet-1"
        self.sheet_tab = "SD"
        self.ricefw_id = ricefw_id
        self.field = field
        self.new_value = new_value
        self.old_value = "In Progress"


class FakeProject:
    spreadsheet_id = "sheet-1"
    schema_config: dict = {}


async def _run(monkeypatch, entry, current, raises=None):
    async def fake_get_row_raw(*args, **kwargs):
        if raises:
            raise raises
        return current

    monkeypatch.setattr("app.api.audit.get_row_raw", fake_get_row_raw)
    await _verify_unchanged(entry, FakeProject(), object())


# --- what counts as "the same value" -----------------------------------------

def test_none_and_empty_string_are_the_same_cell_value():
    """The audit row says None for a blank; the sheet reports "". Same thing."""
    assert _normalise(None) == _normalise("")


def test_surrounding_whitespace_does_not_make_a_value_different():
    assert _normalise(" Completed ") == _normalise("Completed")


# --- the guard ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_untouched_cell_passes(monkeypatch):
    await _run(monkeypatch, FakeEntry("Completed"), {"Dev Status": "Completed"})


@pytest.mark.asyncio
async def test_undoing_a_write_that_filled_a_blank_is_allowed(monkeypatch):
    """old_value is blank here; that is a real undo, not a missing record."""
    entry = FakeEntry("Completed")
    entry.old_value = None
    await _run(monkeypatch, entry, {"Dev Status": "Completed"})


@pytest.mark.asyncio
async def test_a_cell_someone_else_changed_is_refused(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        await _run(monkeypatch, FakeEntry("Completed"), {"Dev Status": "Cancelled"})
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_the_refusal_names_both_values_so_the_user_can_judge(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        await _run(monkeypatch, FakeEntry("Completed"), {"Dev Status": "Cancelled"})
    detail = exc.value.detail
    assert "Cancelled" in detail and "Completed" in detail and "W-1" in detail


@pytest.mark.asyncio
async def test_a_cell_someone_else_blanked_is_refused(monkeypatch):
    """The quiet case: an empty cell is not "unchanged", it is a deletion."""
    with pytest.raises(HTTPException) as exc:
        await _run(monkeypatch, FakeEntry("Completed"), {"Dev Status": ""})
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_a_read_failure_refuses_rather_than_proceeding(monkeypatch):
    """A guard that fails open is not a guard. Retrying costs a minute; being wrong
    silently destroys someone's edit."""
    with pytest.raises(HTTPException) as exc:
        await _run(monkeypatch, FakeEntry("Completed"), None, raises=RuntimeError("429"))
    assert exc.value.status_code == 503


# --- scope -------------------------------------------------------------------

def test_row_creating_and_formatting_tools_are_not_undoable():
    """Undoing an append means deleting a row, and nothing here deletes anything."""
    assert "add_row" not in UNDOABLE_TOOLS
    assert "format_row" not in UNDOABLE_TOOLS


def test_both_cell_writing_tools_are_undoable():
    """A bulk_update audit row describes one row and one field, same as update_cell —
    so its inverse is an ordinary single-cell write, not a second bulk operation."""
    assert set(UNDOABLE_TOOLS) == {"update_cell", "bulk_update"}
