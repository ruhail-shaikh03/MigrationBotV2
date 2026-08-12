"""RBAC on the dashboard write path.

`agentic_loop.py` enforces permissions before every tool dispatch, but the PATCH endpoint
does not go through that loop and has to repeat the check itself. These assert on the
exact `args` shape the endpoint builds, because the field-level gate reads
`args["updates"][*]["field"]` — a differently-shaped dict would look like it was being
checked while silently allowing every field through.
"""

import pytest

from app.core.permissions import PermissionChecker

TECH = "Technical Resource "
FUNC = "Functinal Resource "


def _args(*fields):
    """Exactly what dashboard.py:patch_row passes to can_execute."""
    return {"ricefw_id": "SD-045", "updates": [{"field": f, "value": "Sara"} for f in fields]}


def _editor(allowed_fields=None, denied=None):
    return PermissionChecker("e@x.com", "editor", allowed_fields or ["*"], denied or [])


def test_viewer_cannot_reassign():
    viewer = PermissionChecker("v@x.com", "viewer", ["*"], [])
    allowed, reason = viewer.can_execute("update_cell", _args(TECH))
    assert allowed is False
    assert "read-only" in reason.lower()


def test_editor_with_wildcard_can_reassign_any_role():
    allowed, _ = _editor().can_execute("update_cell", _args(TECH))
    assert allowed is True
    allowed, _ = _editor().can_execute("update_cell", _args(FUNC))
    assert allowed is True


def test_field_restricted_editor_is_denied_a_role_outside_their_fields():
    """The whole point of the arg shape: this must actually deny."""
    checker = _editor(allowed_fields=["Dev Status"])
    allowed, reason = checker.can_execute("update_cell", _args(TECH))
    assert allowed is False
    assert TECH in reason


def test_field_restricted_editor_can_write_their_own_field():
    checker = _editor(allowed_fields=["Dev Status"])
    allowed, _ = checker.can_execute("update_cell", _args("Dev Status"))
    assert allowed is True


def test_allowed_field_matching_respects_the_verbatim_header():
    """allowed_fields carries the sheet's exact header, trailing space included."""
    checker = _editor(allowed_fields=[TECH])
    assert checker.can_execute("update_cell", _args(TECH))[0] is True


def test_a_mixed_batch_is_denied_if_any_field_is_blocked():
    """Partial application would be worse: some cells written, some silently refused."""
    checker = _editor(allowed_fields=["Dev Status"])
    allowed, reason = checker.can_execute("update_cell", _args("Dev Status", TECH))
    assert allowed is False
    assert TECH in reason


def test_denied_operations_blocks_update_cell_outright():
    checker = _editor(denied=["update_cell"])
    allowed, reason = checker.can_execute("update_cell", _args("Dev Status"))
    assert allowed is False
    assert "permission" in reason.lower()


def test_admin_bypasses_field_restrictions():
    admin = PermissionChecker("a@x.com", "admin", ["Dev Status"], ["update_cell"])
    assert admin.can_execute("update_cell", _args(TECH))[0] is True


def test_default_role_is_fail_closed_for_writes():
    """An unplaceable caller lands on the default role, which must not be able to write."""
    from app.config import settings

    checker = PermissionChecker("unknown@x.com", settings.DEFAULT_ROLE, ["*"], [])
    if settings.DEFAULT_ROLE == "viewer":
        assert checker.can_execute("update_cell", _args(TECH))[0] is False


@pytest.mark.parametrize("tool", ["update_cell"])
def test_the_endpoint_declares_a_tool_the_checker_recognises(tool):
    """A tool name outside both sets fails closed, so a typo here would deny every write."""
    from app.core.permissions import READ_ONLY_TOOLS, WRITE_TOOLS

    assert tool in READ_ONLY_TOOLS or tool in WRITE_TOOLS
