"""Failure classification for tool results.

A tool call can fail for reasons a *different* tool call would fix — a column
name that doesn't exist in this sheet, an ID that was never there — and for
reasons no retry will ever fix: Redis is down, the user lacks the permission,
Google is rate-limiting, the Google session expired.

`dispatch_tool` used to collapse both into `{"ok": False, "error": str(e)}`, and
`agentic_loop` appended the same "formulate a corrected tool call" note to every
one of them. Two things went wrong as a result during the 2026-08 incident:

  * the model treated a Redis outage as a bad argument and spent its remaining
    iterations rewriting a call that could not succeed, and
  * the user was shown the raw exception text — "You can't write against a read
    only replica" — and reasonably concluded the problem was permissions on
    their own spreadsheet.

So a failure now carries a `error_kind`, guidance chosen for that kind, and a
`user_message` written for someone who has never heard of Redis.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger("tool_errors")

# Failure classes. The distinction that actually matters is whether a corrected
# tool call could plausibly succeed — only INVALID_REQUEST and NOT_FOUND qualify.
INVALID_REQUEST = "invalid_request"
NOT_FOUND = "not_found"
AMBIGUOUS_ID = "ambiguous_id"
PERMISSION = "permission"
AUTH = "auth"
RATE_LIMIT = "rate_limit"
INFRASTRUCTURE = "infrastructure"
UPSTREAM = "upstream"
UNKNOWN = "unknown"


class AmbiguousRowError(Exception):
    """A primary ID matched more than one row, so no single row can be addressed.

    Raised by the read path, where returning a sentinel would just be re-checked by
    every caller. The write path returns `ambiguous_id_result()` instead, because the
    worker reads `{"ok": ...}` dicts rather than catching (queue/worker.py:157).
    Both routes end up classified as AMBIGUOUS_ID.
    """

    def __init__(self, primary_id: str, row_nums: Any):
        self.primary_id = primary_id
        self.row_nums = list(row_nums)
        super().__init__(_ambiguity_detail(primary_id, self.row_nums))


def _ambiguity_detail(primary_id: str, row_nums: Any) -> str:
    nums = list(row_nums)
    rows = ", ".join(str(n) for n in nums)
    return f"'{primary_id}' matches {len(nums)} rows in this tab (rows {rows})."

# What the model is told after each class of failure. Note that only the first
# two invite a retry; the rest explicitly forbid one, because a retry there just
# burns iterations against the 8-iteration cap and delays telling the user.
MODEL_GUIDANCE: Dict[str, str] = {
    INVALID_REQUEST: (
        "The arguments did not match this sheet's structure. Check the column names in the "
        "schema you were given and retry once with corrected arguments."
    ),
    NOT_FOUND: (
        "The record or column named does not exist in this tab. Use search_rows to locate the "
        "correct one before retrying, or tell the user it isn't there."
    ),
    AMBIGUOUS_ID: (
        "That ID appears on more than one row, so there is no way to tell which one was meant. "
        "Do NOT retry the identical call and do NOT guess a row — nothing you can put in the "
        "arguments resolves this. Tell the user the ID is duplicated, name the row numbers, and "
        "ask which row they mean or suggest they fix the duplicate in the sheet."
    ),
    PERMISSION: (
        "The user is not permitted to do this. Do NOT retry — say plainly which action was "
        "denied and that it requires elevated access."
    ),
    AUTH: (
        "The user's Google session is no longer valid. Do NOT retry — tell them to sign out "
        "and sign in again."
    ),
    RATE_LIMIT: (
        "Google's rate limit was hit. Do NOT retry in this turn — tell the user to try again "
        "in a moment."
    ),
    INFRASTRUCTURE: (
        "This is a backend outage, NOT a problem with the request. Do NOT retry and do NOT "
        "rephrase the call. Tell the user the service is temporarily unavailable and that "
        "their change was NOT saved."
    ),
    UPSTREAM: (
        "Google Sheets rejected the operation. Do NOT repeat the identical call — explain "
        "what failed to the user."
    ),
    UNKNOWN: (
        "Do NOT repeat the identical call. If the arguments could be at fault, correct them "
        "once; otherwise report the failure to the user."
    ),
}

# Shown to the user. Deliberately free of infrastructure nouns — "Redis",
# "replica" and "asyncpg" mean nothing to someone editing a tracker.
#
# Two kinds carry their detail through instead of replacing it:
#
#   UNKNOWN, because classification found nothing to say, so the raw exception text
#   is the *only* information available and suppressing it would leave the user with
#   a bare "Something went wrong".
#
#   AMBIGUOUS_ID, because the row numbers ARE the message. "That ID matches more than
#   one row" without saying which rows leaves the user no way to act; with them, they
#   can go look at rows 47 and 214 and decide which one they meant.
#
# See _CARRIES_DETAIL and _MAX_DETAIL_CHARS below.
USER_MESSAGE: Dict[str, str] = {
    INVALID_REQUEST: "That didn't match the sheet's structure.",
    NOT_FOUND: "I couldn't find that record in this tab.",
    AMBIGUOUS_ID: "That ID is on more than one row, so I didn't change anything.",
    PERMISSION: "You don't have permission to do that.",
    AUTH: "Your Google session expired — please sign out and back in.",
    RATE_LIMIT: "Google Sheets is rate-limiting us. Try again in a moment.",
    INFRASTRUCTURE: "The service is temporarily unavailable, so your change was not saved. Please try again shortly.",
    UPSTREAM: "Google Sheets rejected that operation.",
    UNKNOWN: "Something went wrong.",
}


# Toasts are one line; a Google HttpError's repr can run to several hundred
# characters of URL and HTML, so an appended detail is capped.
_MAX_DETAIL_CHARS = 180

# The kinds whose detail is information the user needs, not infrastructure noise.
_CARRIES_DETAIL = frozenset({UNKNOWN, AMBIGUOUS_ID})


def user_message_for(kind: str, detail: str) -> str:
    """The message a human should see for this failure.

    For most classified kinds the curated sentence is strictly better than the
    exception text — "You can't write against a read only replica" reads as a
    spreadsheet permissions problem, which it isn't. For the kinds in
    _CARRIES_DETAIL there is either no curated knowledge to substitute (UNKNOWN)
    or the detail is the actionable part (AMBIGUOUS_ID's row numbers), so it is
    carried through instead.
    """
    base = USER_MESSAGE.get(kind, USER_MESSAGE[UNKNOWN])
    if kind not in _CARRIES_DETAIL or not detail:
        return base
    trimmed = detail if len(detail) <= _MAX_DETAIL_CHARS else detail[: _MAX_DETAIL_CHARS - 1] + "…"
    # rstrip the sentence-ending period so the join doesn't read "wrong.: detail".
    return f"{base.rstrip('.')}: {trimmed}"


def _http_status(exc: Exception) -> int:
    """Best-effort status extraction from a googleapiclient HttpError."""
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    try:
        return int(status)
    except (TypeError, ValueError):
        return 0


def classify_error(exc: Exception) -> str:
    """Map an exception to one of the failure classes above.

    Imports are local so this module stays importable in test contexts that stub
    out the Google or Redis layers.
    """
    # --- Our own: an ID that addresses more than one row ---
    # Checked before the builtin-exception rule at the bottom, which would otherwise
    # be reachable if this ever grows a ValueError base.
    if isinstance(exc, AmbiguousRowError):
        return AMBIGUOUS_ID

    # --- Backend infrastructure: Redis and Postgres ---
    try:
        import redis.exceptions as redis_exc
        # ReadOnlyError is a ResponseError, NOT a ConnectionError — it has to be
        # named explicitly. Missing that is what let a replica-promoted Redis
        # surface to the user as a spreadsheet permissions error.
        if isinstance(exc, (redis_exc.ReadOnlyError, redis_exc.ConnectionError, redis_exc.TimeoutError)):
            return INFRASTRUCTURE
    except ImportError:
        pass

    try:
        from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
        if isinstance(exc, (OperationalError, InterfaceError, DBAPIError)):
            return INFRASTRUCTURE
    except ImportError:
        pass

    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return INFRASTRUCTURE

    # --- Google: expired credentials, then HTTP status ---
    try:
        from google.auth.exceptions import GoogleAuthError, RefreshError
        if isinstance(exc, (RefreshError, GoogleAuthError)):
            return AUTH
    except ImportError:
        pass

    try:
        from googleapiclient.errors import HttpError
        if isinstance(exc, HttpError):
            status = _http_status(exc)
            if status == 401:
                return AUTH
            if status == 403:
                # Sheets returns 403 both for "no access to this spreadsheet" and
                # for quota exhaustion; the reason string is what separates them.
                text = str(exc).lower()
                if "rate" in text or "quota" in text:
                    return RATE_LIMIT
                return PERMISSION
            if status == 404:
                return NOT_FOUND
            if status == 429:
                return RATE_LIMIT
            if status >= 500:
                return UPSTREAM
            if status >= 400:
                return INVALID_REQUEST
            return UPSTREAM
    except ImportError:
        pass

    # --- Caller's fault: bad column, missing key, unparseable value ---
    if isinstance(exc, (KeyError, ValueError, TypeError, IndexError)):
        return INVALID_REQUEST

    return UNKNOWN


def error_result(exc: Exception, tool_name: str = "") -> Dict[str, Any]:
    """Build the failure dict a tool returns, classified rather than bare."""
    kind = classify_error(exc)
    detail = str(exc) or exc.__class__.__name__
    if kind in (INFRASTRUCTURE, UPSTREAM, AUTH):
        # These are operational problems worth finding in the logs later; the
        # argument-level ones are routine and stay at the caller's log level.
        logger.error(f"Tool '{tool_name}' failed [{kind}]: {exc.__class__.__name__}: {detail}")
    return {
        "ok": False,
        "error": detail,
        "error_kind": kind,
        "user_message": user_message_for(kind, detail),
    }


def ambiguous_id_result(primary_id: str, row_nums: Any, tool_name: str = "") -> Dict[str, Any]:
    """The refusal a write tool returns when its target ID is not unique.

    Same shape as `error_result` so the worker's `res.get("ok")` / `res.get("error")`
    reading is unchanged, but classified rather than a bare string — a bare error here
    would be re-read by the model as a bad argument and retried against the 8-iteration
    cap, which is exactly what the classification layer exists to prevent.
    """
    detail = _ambiguity_detail(primary_id, row_nums)
    return {
        "ok": False,
        "error": detail,
        "error_kind": AMBIGUOUS_ID,
        "user_message": user_message_for(AMBIGUOUS_ID, detail),
        "ambiguous_rows": list(row_nums),
    }


def failure_note(tool_name: str, tool_result: Dict[str, Any]) -> str:
    """The note appended to a failed tool's content before it re-enters the model's
    context. Replaces the old unconditional 'formulate a corrected tool call'."""
    kind = tool_result.get("error_kind") or UNKNOWN
    detail = tool_result.get("error", "Unknown error")
    guidance = MODEL_GUIDANCE.get(kind, MODEL_GUIDANCE[UNKNOWN])
    return f"\n[System Note]: Tool '{tool_name}' failed ({kind}): '{detail}'. {guidance}"
