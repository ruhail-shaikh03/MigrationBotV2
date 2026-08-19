"""The Drive push-notification receiver.

This is the third unauthenticated route in the application and the only one that changes
state, so what it refuses matters as much as what it does. It authenticates the *sender* by
a shared secret, because Drive posts with no user credentials and no way to obtain any.

The endpoint is deliberately cheap: a notification tells us a file changed and nothing more
- not which cell, not even which tab - so the handler clears caches and stops. That is also
what keeps it safe to leave open: the worst a forged notification achieves is making us
re-read a sheet we already had.

TestClient is constructed without its context manager on purpose, so FastAPI's lifespan -
and therefore `init_db()`'s connection attempt - never runs.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

TOKEN = "shared-secret-value"
ENDPOINT = "/api/webhooks/drive"

client = TestClient(app)


def _session_returning(channel):
    """An AsyncSessionLocal stand-in whose one query resolves to `channel`."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = channel
    session = AsyncMock()
    session.execute.return_value = result

    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=context)


def _headers(token=TOKEN, state="update", channel_id="chan-1"):
    return {
        "X-Goog-Channel-Token": token,
        "X-Goog-Resource-State": state,
        "X-Goog-Channel-ID": channel_id,
        "X-Goog-Message-Number": "2",
    }


# --- authenticating the sender -----------------------------------------------

def test_an_unconfigured_deployment_refuses_notifications_outright():
    """No token configured means no channel was ever registered, so nothing legitimate can
    be arriving. Accepting it would be an unauthenticated state change."""
    with patch.object(settings, "DRIVE_WEBHOOK_TOKEN", ""):
        response = client.post(ENDPOINT, headers=_headers())

    assert response.status_code == 403


def test_a_wrong_token_is_rejected():
    with patch.object(settings, "DRIVE_WEBHOOK_TOKEN", TOKEN):
        response = client.post(ENDPOINT, headers=_headers(token="not-the-token"))

    assert response.status_code == 403


def test_a_missing_token_is_rejected():
    with patch.object(settings, "DRIVE_WEBHOOK_TOKEN", TOKEN):
        response = client.post(
            ENDPOINT, headers={"X-Goog-Resource-State": "update", "X-Goog-Channel-ID": "c"}
        )

    assert response.status_code == 403


# --- what a notification means -----------------------------------------------

def test_the_sync_handshake_is_ignored():
    """Drive sends exactly one of these when a channel opens. It means the channel works,
    not that the file changed — acting on it would re-scan every tab at registration time."""
    invalidate = AsyncMock()
    with patch.object(settings, "DRIVE_WEBHOOK_TOKEN", TOKEN), \
         patch("app.api.webhooks.invalidate_tab", invalidate):
        response = client.post(ENDPOINT, headers=_headers(state="sync"))

    assert response.status_code == 200
    assert response.json()["ignored"] == "sync"
    assert invalidate.await_count == 0


def test_a_notification_for_an_unknown_channel_is_a_quiet_success():
    """Most likely a channel we replaced and failed to stop, still notifying until it
    expires. Not an error, and not ours to act on — and answering 5xx would make Drive
    retry it."""
    invalidate = AsyncMock()
    with patch.object(settings, "DRIVE_WEBHOOK_TOKEN", TOKEN), \
         patch("app.api.webhooks.AsyncSessionLocal", _session_returning(None)), \
         patch("app.api.webhooks.invalidate_tab", invalidate):
        response = client.post(ENDPOINT, headers=_headers())

    assert response.status_code == 200
    assert response.json()["ignored"] == "unknown-channel"
    assert invalidate.await_count == 0


def test_a_valid_notification_invalidates_the_whole_spreadsheet():
    """File-level granularity: the payload cannot say which tab changed, so marking all of
    them is correct rather than merely convenient."""
    channel = MagicMock()
    channel.spreadsheet_id = "sheet-1"
    invalidate = AsyncMock()

    with patch.object(settings, "DRIVE_WEBHOOK_TOKEN", TOKEN), \
         patch("app.api.webhooks.AsyncSessionLocal", _session_returning(channel)), \
         patch("app.api.webhooks.invalidate_tab", invalidate):
        response = client.post(ENDPOINT, headers=_headers())

    assert response.status_code == 200
    assert response.json()["spreadsheet_id"] == "sheet-1"
    # No tab argument — that is the point.
    invalidate.assert_awaited_once_with("sheet-1")


def test_a_database_failure_answers_200_rather_than_inviting_a_retry_storm():
    """Drive retries 5xx. A transient blip must not turn one edit into a queue of them."""
    with patch.object(settings, "DRIVE_WEBHOOK_TOKEN", TOKEN), \
         patch("app.api.webhooks.AsyncSessionLocal",
               MagicMock(side_effect=ConnectionError("postgres is down"))), \
         patch("app.api.webhooks.invalidate_tab", AsyncMock()):
        response = client.post(ENDPOINT, headers=_headers())

    assert response.status_code == 200
    assert response.json()["ok"] is False


# --- the route's own posture -------------------------------------------------

def test_the_endpoint_requires_no_authorization_header():
    """Asserted deliberately: this route's lack of Depends(get_current_user) is a design
    decision, not an omission, and a future refactor that 'fixes' it would silently stop
    every notification."""
    with patch.object(settings, "DRIVE_WEBHOOK_TOKEN", TOKEN), \
         patch("app.api.webhooks.AsyncSessionLocal", _session_returning(None)), \
         patch("app.api.webhooks.invalidate_tab", AsyncMock()):
        response = client.post(ENDPOINT, headers=_headers())

    assert response.status_code != 401
    assert response.status_code == 200
