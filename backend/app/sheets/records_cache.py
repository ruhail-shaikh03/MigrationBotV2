"""The durable Postgres tier of the row cache: `sheet_records` + `sheet_sync_state`.

The Redis tier in `rows_cache.py` answers freshness with a timer. Sixty seconds after a
scan it assumes the data is stale whether or not anything changed, so a tracker nobody has
touched in a month still costs a full paginated scan every minute that somebody is looking
at it. This tier replaces the timer with a fact: rows are served until something says they
changed - a Drive push notification, our own worker finishing a write, or a `modifiedTime`
that no longer matches what we stored.

The contract is deliberately narrow. `read_tab` returns a matrix only when the tab is
*provably* clean and returns None for every other case, including every failure. None means
"scan it live", which is what this whole layer replaced, so the worst outcome of anything
going wrong here is the performance the application had before it existed. Postgres being
slow, unreachable, or empty must never surface to a user as an error - the same rule
`rows_cache` already applies to Redis.

**No advisory lock.** An earlier draft guarded refills with `pg_advisory_xact_lock` so that
concurrent readers of a cold tab produced one scan instead of several. That means holding a
Postgres transaction open across a multi-second Google Sheets scan, which trades a cheap,
harmless duplicate read for a lock held on an external network call - a much worse failure
mode under load. Refills are idempotent (each one replaces the tab wholesale in a single
transaction), so racing refills converge on the same rows; the only cost is a redundant
scan, and the Redis tier in front already collapses the common bursts.
"""

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

from sqlalchemy import delete, insert, select, update

from app.config import settings
from app.core.schema import get_tab_schema
from app.db.engine import AsyncSessionLocal
from app.models.project import Project
from app.models.sheet_record import SheetRecord
from app.models.sheet_sync_state import SheetSyncState
from app.models.sheet_watch_channel import SheetWatchChannel

logger = logging.getLogger("records_cache")

# How long a confirmed-fresh tab is trusted without asking Drive again. This exists so that
# a burst of reads arriving with Redis cold costs one metadata call rather than one per read.
# Short, because it is the one window in which this tier can still serve genuinely stale data.
FRESHNESS_GRACE_SECONDS = 30

# Rows per INSERT. A tracker can reach _MAX_SCAN_ROWS (20 000) and asyncpg has to bind every
# parameter in the statement, so the refill is chunked rather than sent as one enormous insert.
_INSERT_CHUNK = 1000


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def read_tab(
    sheets_service: Any,
    spreadsheet_id: str,
    tab: str,
    data_start_row: int,
) -> Optional[Tuple[List[str], List[List[str]], bool]]:
    """`(headers, rows, truncated)` if the tab is provably clean, else None.

    None is not an error signal - it is the normal answer for a cold, dirty, or
    unverifiable tab, and the caller responds by scanning live and calling `store_tab`.
    """
    try:
        async with AsyncSessionLocal() as db:
            state = await db.get(SheetSyncState, (spreadsheet_id, tab))
            if state is None or state.is_dirty:
                return None

            # Every cached row_number is an offset from the start row it was scanned at. A
            # caller arriving with a different one is asking about a differently-shaped tab
            # (re-detection can move the header block), so the cached rows are not answers to
            # its question even though they are current.
            if state.data_start_row != data_start_row:
                return None

            if not await _confirm_fresh(db, sheets_service, spreadsheet_id, state):
                return None

            result = await db.execute(
                select(SheetRecord.data)
                .where(
                    SheetRecord.spreadsheet_id == spreadsheet_id,
                    SheetRecord.tab == tab,
                )
                .order_by(SheetRecord.row_number)
            )
            rows = [row[0] for row in result.all()]
            return list(state.headers or []), rows, bool(state.truncated)
    except Exception as e:
        logger.warning(
            f"Row record cache read failed for {spreadsheet_id}/{tab}, "
            f"falling back to Sheets: {e}"
        )
        return None


def _clean_without_drive(state: Any, channel: Any, now: datetime) -> bool:
    """Whether freshness is already established without spending an API call.

    Pure, and separated out because it is the decision that determines whether this whole
    layer actually saves anything: get it wrong in the permissive direction and the cache
    serves stale rows, get it wrong in the strict direction and every read still costs a
    Drive call.

    Two ways to be sure without asking:

    * A live push channel. Drive has undertaken to tell us when the file changes, so silence
      is informative. This is the steady state and the entire point of registering one.
    * A very recent confirmation. Collapses a burst of reads arriving with Redis cold into
      one metadata call rather than one per read.
    """
    if channel is not None and channel.expiration > now:
        return True
    if (
        state.checked_at is not None
        and (now - state.checked_at).total_seconds() < FRESHNESS_GRACE_SECONDS
    ):
        return True
    return False


async def _confirm_fresh(
    db: Any, sheets_service: Any, spreadsheet_id: str, state: SheetSyncState
) -> bool:
    """Whether the tab's stored rows still match the spreadsheet."""
    now = _now()

    channel = (
        await db.execute(
            select(SheetWatchChannel).where(
                SheetWatchChannel.spreadsheet_id == spreadsheet_id
            )
        )
    ).scalar_one_or_none()
    if _clean_without_drive(state, channel, now):
        await _maybe_renew_channel(db, sheets_service, channel, now)
        return True

    from app.sheets.drive import get_modified_time

    modified = await get_modified_time(sheets_service, spreadsheet_id)
    if modified is None:
        # Could not ask - no Drive scope on this session, an outage, a quota refusal. We do
        # not know, so we assume stale. That is one wasted scan, versus serving a stale row
        # to somebody making a decision from it.
        return False
    if modified != state.drive_modified_time:
        return False

    state.checked_at = now
    await db.commit()
    return True


async def _maybe_renew_channel(
    db: Any, sheets_service: Any, channel: Any, now: datetime
) -> None:
    """Extend a push channel that is close to expiring, using whoever happens to be reading.

    Drive channels expire - ours after a day - and this application has no scheduler, no
    cron and no periodic task infrastructure of any kind. Without renewal the whole push
    mechanism would decay silently after 24 hours and quietly become a `modifiedTime` poll,
    which is the sort of regression nobody notices because nothing breaks.

    Rather than introduce a scheduler for one job, renewal rides on ordinary traffic: any
    read that finds a channel inside the renewal margin extends it. A project anyone looks
    at stays watched indefinitely; one nobody looks at lapses, and the first read after that
    pays for a `modifiedTime` check instead. That is the correct priority order.

    The new channel is registered *before* the old one is stopped, so there is never a window
    with no channel at all. Two readers racing here produce one extra channel that nothing
    has a row for; the receiver ignores notifications whose channel ID it does not recognise,
    so the loser leaks until it expires rather than causing duplicate invalidations.

    Every failure is silent by design. Renewal is opportunistic - a read must not fail, or
    even slow down noticeably, because a channel could not be extended.
    """
    if channel is None:
        return
    try:
        from app.sheets.client import drive_service_for
        from app.sheets.drive import (
            WATCH_RENEW_MARGIN_SECONDS,
            push_configured,
            register_watch,
            stop_watch,
            webhook_address,
        )

        if (channel.expiration - now).total_seconds() > WATCH_RENEW_MARGIN_SECONDS:
            return
        if not push_configured():
            return

        drive = drive_service_for(sheets_service)
        if drive is None:
            return

        renewed = await register_watch(
            drive, channel.spreadsheet_id, webhook_address(), settings.DRIVE_WEBHOOK_TOKEN
        )
        if renewed is None:
            return

        old_channel_id, old_resource_id = channel.channel_id, channel.resource_id
        channel.channel_id = renewed["channel_id"]
        channel.resource_id = renewed["resource_id"]
        channel.token = renewed["token"]
        channel.expiration = renewed["expiration"]
        await db.commit()

        await stop_watch(drive, old_channel_id, old_resource_id)
        logger.info(
            f"Renewed Drive watch for {channel.spreadsheet_id} until "
            f"{renewed['expiration'].isoformat()}."
        )
    except Exception as e:
        logger.warning(f"Drive watch renewal failed: {e}")


def _build_records(
    spreadsheet_id: str,
    tab: str,
    data_start_row: int,
    rows: List[List[str]],
    primary_idx: int,
    now: datetime,
) -> List[dict]:
    """Turn a scanned matrix into rows for `sheet_records`.

    Pure, so the parts that are easy to get quietly wrong can be tested directly: rows are
    ragged (Sheets omits trailing empty cells, so a row can be shorter than the ID column),
    the ID is normalised the way `find_row_num` does it, and a blank ID must become NULL
    rather than an empty string that would later match another blank.

    Nothing here deduplicates. Two rows sharing an ID produce two records, which is the
    whole reason `primary_id` is indexed non-uniquely - see §16.7.
    """
    records = []
    for offset, row in enumerate(rows):
        primary_id = None
        if primary_idx < len(row) and row[primary_idx] is not None:
            primary_id = str(row[primary_idx]).strip().upper() or None
        records.append({
            "spreadsheet_id": spreadsheet_id,
            "tab": tab,
            "row_number": data_start_row + offset,
            "primary_id": primary_id,
            "data": row,
            "synced_at": now,
        })
    return records


async def store_tab(
    spreadsheet_id: str,
    tab: str,
    data_start_row: int,
    headers: List[str],
    rows: List[List[str]],
    truncated: bool,
    drive_modified_time: Optional[str],
) -> None:
    """Replace the tab's cached rows with a freshly scanned matrix.

    `drive_modified_time` must be the value read *before* the scan started. Reading it
    afterwards would record a version that includes edits landing mid-scan, marking rows we
    never read as current; reading it first means such an edit shows up as a mismatch on the
    next check and triggers another refill. Erring toward a redundant scan is the safe
    direction, and the only one that cannot serve a value the sheet does not hold.

    Failures are logged and swallowed. A cache that could not be written is a slower next
    read, never a failed current one.
    """
    try:
        async with AsyncSessionLocal() as db:
            # One transaction: a reader either sees the whole previous scan or the whole new
            # one, never a tab mid-replacement.
            #
            # `db.begin()` must come before *any* statement on this session. A SQLAlchemy 2.0
            # session autobegins on its first execute, and `begin()` on an already-begun
            # session raises InvalidRequestError — which, with the blanket `except` below,
            # would be swallowed into a log line and turn this whole function into a silent
            # no-op. The cache would never populate, `read_tab` would always miss, and every
            # unit test would still pass because they mock this function.
            async with db.begin():
                primary_idx = await _primary_id_index(db, spreadsheet_id, tab)
                now = _now()
                records = _build_records(
                    spreadsheet_id, tab, data_start_row, rows, primary_idx, now
                )

                await db.execute(
                    delete(SheetRecord).where(
                        SheetRecord.spreadsheet_id == spreadsheet_id,
                        SheetRecord.tab == tab,
                    )
                )
                for start in range(0, len(records), _INSERT_CHUNK):
                    await db.execute(insert(SheetRecord), records[start:start + _INSERT_CHUNK])

                state = await db.get(SheetSyncState, (spreadsheet_id, tab))
                if state is None:
                    state = SheetSyncState(spreadsheet_id=spreadsheet_id, tab=tab)
                    db.add(state)
                state.headers = list(headers or [])
                state.row_count = len(records)
                state.truncated = bool(truncated)
                state.data_start_row = data_start_row
                state.drive_modified_time = drive_modified_time
                state.is_dirty = False
                state.synced_at = now
                state.checked_at = now
                state.last_error = None
    except Exception as e:
        logger.warning(f"Row record cache write failed for {spreadsheet_id}/{tab}: {e}")


async def _primary_id_index(db: Any, spreadsheet_id: str, tab: str) -> int:
    """The 0-based column index of the tab's ID column.

    Resolved from `primary_id_position` (a column letter) exactly as `find_row_num` and
    `read._cached_matrix` do, never from `primary_id_column` (a header name) - the two
    disagree on a mis-detected schema, and this index decides which cell gets indexed as a
    row's identity.
    """
    from app.sheets.read import _column_letter_to_index

    try:
        project = (
            await db.execute(select(Project).where(Project.spreadsheet_id == spreadsheet_id))
        ).scalar_one_or_none()
        if project is None:
            return _column_letter_to_index("B")
        tab_schema = get_tab_schema(project.schema_config or {}, tab)
        return _column_letter_to_index(tab_schema.get("primary_id_position", "B"))
    except Exception:
        return _column_letter_to_index("B")


async def mark_dirty(spreadsheet_id: str, tab: Optional[str] = None) -> None:
    """Flag a tab - or every tab of a spreadsheet - as needing a re-scan.

    Rows are kept rather than deleted. They are already known to be superseded, so they are
    not served, but keeping them means the table always holds a last-known-good copy and a
    refill replaces rather than repopulates.

    `tab` is optional because Drive notifications are file-level: a push tells us the
    spreadsheet changed and cannot tell us which tab, so the honest response is to mark all
    of them. Mirrors `rows_cache.invalidate_tab`, which is optional-tab for the same reason.
    """
    try:
        async with AsyncSessionLocal() as db:
            stmt = (
                update(SheetSyncState)
                .where(SheetSyncState.spreadsheet_id == spreadsheet_id)
                .values(is_dirty=True)
            )
            if tab:
                stmt = stmt.where(SheetSyncState.tab == tab)
            await db.execute(stmt)
            await db.commit()
    except Exception as e:
        logger.warning(
            f"Row record cache invalidation failed for {spreadsheet_id}/{tab}: {e}"
        )
