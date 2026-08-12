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
PERMISSION = "permission"
AUTH = "auth"
RATE_LIMIT = "rate_limit"
INFRASTRUCTURE = "infrastructure"
UPSTREAM = "upstream"
UNKNOWN = "unknown"

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
USER_MESSAGE: Dict[str, str] = {
    INVALID_REQUEST: "That didn't match the sheet's structure.",
    NOT_FOUND: "I couldn't find that record in this tab.",
    PERMISSION: "You don't have permission to do that.",
    AUTH: "Your Google session expired — please sign out and back in.",
    RATE_LIMIT: "Google Sheets is rate-limiting us. Try again in a moment.",
    INFRASTRUCTURE: "The service is temporarily unavailable, so your change was not saved. Please try again shortly.",
    UPSTREAM: "Google Sheets rejected that operation.",
    UNKNOWN: "Something went wrong.",
}


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
        "user_message": USER_MESSAGE.get(kind, USER_MESSAGE[UNKNOWN]),
    }


def failure_note(tool_name: str, tool_result: Dict[str, Any]) -> str:
    """The note appended to a failed tool's content before it re-enters the model's
    context. Replaces the old unconditional 'formulate a corrected tool call'."""
    kind = tool_result.get("error_kind") or UNKNOWN
    detail = tool_result.get("error", "Unknown error")
    guidance = MODEL_GUIDANCE.get(kind, MODEL_GUIDANCE[UNKNOWN])
    return f"\n[System Note]: Tool '{tool_name}' failed ({kind}): '{detail}'. {guidance}"
