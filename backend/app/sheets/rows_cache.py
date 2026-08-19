"""The row cache: a short-lived Redis window over a durable Postgres read model.

The dashboard changed the read profile. Chat asks a question, the agent runs one or two
tool calls, and each is a fresh Sheets scan - acceptable at that rate. A grid is
different: every sort, filter, page and panel switch would re-scan the whole tab, and
`read.py:_fetch_all_rows` pages up to `_MAX_SCAN_ROWS` (20 000) rows per scan. Left
uncached, a few minutes of a PM clicking around burns the Sheets quota that the write
worker also depends on.

Three tiers, tried in order:

1. **Redis**, 60 seconds. Collapses one person's burst of clicks. Purely a hot window - it
   knows nothing about whether the sheet changed, only how long ago it was read.
2. **Postgres** (`records_cache.py`), no expiry. Serves rows until something says the sheet
   changed: a Drive push notification, our own worker completing a write, or a `modifiedTime`
   that no longer matches. This is what makes an unchanged tab cost nothing to read again,
   however long it stays unchanged.
3. **Google Sheets**, live. The system of record, and the fallback for every failure in the
   two tiers above.

Earlier revisions of this docstring said this was deliberately *not* the `sheet_records`
Postgres cache that CLAUDE.md and several skill files described, because at the time no such
table had ever existed - those documents described a cache that had only ever been imagined.
It exists now, and tiers 1 and 2 above are it. What remains true is the design principle that
made the Redis-only version defensible: every failure degrades to a live scan, and no tier is
ever authoritative over the spreadsheet.
"""

import json
import logging
from typing import Any, List, Optional, Tuple

import redis.asyncio as aioredis

from app.config import settings
from app.sheets import records_cache
from app.sheets.meta import get_header_row
from app.sheets.read import _fetch_all_rows

logger = logging.getLogger("rows_cache")

# Bounded on purpose, and the bound is small.
#
# Every failure path here degrades to a live Sheets scan, so the only thing a long timeout
# buys is a longer wait before doing the work anyway. With the defaults, a Redis outage
# cost ~8 seconds per call (connect retries with backoff) — tolerable when only the
# dashboard used this, once per page load, and not tolerable now that the agent's reads
# come through here too: eight tool calls in one turn would spend over a minute achieving
# nothing while the socket looked hung.
redis_client = aioredis.from_url(
    settings.REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=0.5,
    socket_timeout=2.0,
    retry_on_timeout=False,
)

CACHE_PREFIX = "migrationbot:rows"
# Short on purpose. This exists to collapse the burst of reads from one person working a
# dashboard, not to serve stale data: a sheet edited in Google directly should show up
# within a minute without anyone reaching for an invalidate button.
CACHE_TTL_SECONDS = 60


def _key(spreadsheet_id: str, active_tab: str) -> str:
    return f"{CACHE_PREFIX}:{spreadsheet_id}:{active_tab}"


async def _redis_get(key: str) -> Optional[Tuple[List[str], List[List[str]], bool]]:
    try:
        cached = await redis_client.get(key)
        if cached:
            payload = json.loads(cached)
            return payload["headers"], payload["rows"], payload["truncated"]
    except Exception as e:
        logger.warning(f"Row cache read failed for {key}, falling back: {e}")
    return None


async def _redis_set(
    key: str, headers: List[str], rows: List[List[str]], truncated: bool
) -> None:
    try:
        await redis_client.set(
            key,
            json.dumps({"headers": headers, "rows": rows, "truncated": truncated}, ensure_ascii=False),
            ex=CACHE_TTL_SECONDS,
        )
    except Exception as e:
        logger.warning(f"Row cache write failed for {key}: {e}")


async def get_tab_matrix(
    service: Any,
    spreadsheet_id: str,
    active_tab: str,
    data_start_row: int,
) -> Tuple[List[str], List[List[str]], bool]:
    """Return (headers, rows, truncated) for a tab, from the cheapest tier that can answer.

    `headers` come back stripped, matching `meta.get_header_row`; callers index them the
    same way the rest of the read path does (`{h.lower().strip(): i}`) so a header with a
    real trailing space still resolves (§7.2).

    Neither cache tier is allowed to take the read down with it - every failure in Redis or
    Postgres degrades to a live Sheets scan, because a cache is an optimisation and the
    spreadsheet is always still there to be read.
    """
    key = _key(spreadsheet_id, active_tab)

    hot = await _redis_get(key)
    if hot is not None:
        return hot

    durable = await records_cache.read_tab(service, spreadsheet_id, active_tab, data_start_row)
    if durable is not None:
        headers, rows, truncated = durable
        await _redis_set(key, headers, rows, truncated)
        return headers, rows, truncated

    # Read the version stamp *before* the scan, not after. An edit landing mid-scan then
    # shows up as a mismatch on the next freshness check and costs one redundant re-scan;
    # stamping afterwards would instead record that edit as already captured, and serve rows
    # that never contained it. See records_cache.store_tab.
    from app.sheets.drive import get_modified_time

    modified_time = await get_modified_time(service, spreadsheet_id)

    header_row_num = data_start_row - 1
    headers = await get_header_row(service, spreadsheet_id, active_tab, header_row_num)
    rows, truncated = await _fetch_all_rows(service, spreadsheet_id, active_tab, data_start_row)

    await records_cache.store_tab(
        spreadsheet_id=spreadsheet_id,
        tab=active_tab,
        data_start_row=data_start_row,
        headers=headers,
        rows=rows,
        truncated=truncated,
        drive_modified_time=modified_time,
    )
    await _redis_set(key, headers, rows, truncated)

    return headers, rows, truncated


async def invalidate_tab(
    spreadsheet_id: str,
    active_tab: Optional[str] = None,
    client: Any = None,
) -> None:
    """Drop the cached matrix after a write, so the next read reflects it.

    Without this a user reassigns a row, the worker applies it a second later, and the
    dashboard keeps showing the old value for up to a minute — which reads as the write
    having silently failed. Called from the worker on successful completion.

    `active_tab` is optional so a caller that doesn't know the tab can clear the whole
    spreadsheet. Failures are logged and swallowed: a stale cache entry expires on its own,
    and a write must never be reported as failed because invalidation could not run.

    Both tiers are cleared. Dropping only the Redis key would leave the Postgres tier
    claiming the tab is clean, and the next read would be served pre-write rows from it with
    no TTL to eventually rescue them - strictly worse than the staleness this function
    exists to prevent.
    """
    conn = client or redis_client
    try:
        if active_tab:
            await conn.delete(_key(spreadsheet_id, active_tab))
        else:
            async for key in conn.scan_iter(match=f"{CACHE_PREFIX}:{spreadsheet_id}:*"):
                await conn.delete(key)
    except Exception as e:
        logger.warning(f"Row cache invalidation failed for {spreadsheet_id}/{active_tab}: {e}")

    await records_cache.mark_dirty(spreadsheet_id, active_tab)
