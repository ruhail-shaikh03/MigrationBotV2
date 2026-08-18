"""A short-lived Redis cache of one tab's header row + data rows.

The dashboard changed the read profile. Chat asks a question, the agent runs one or two
tool calls, and each is a fresh Sheets scan — acceptable at that rate. A grid is
different: every sort, filter, page and panel switch would re-scan the whole tab, and
`read.py:_fetch_all_rows` pages up to `_MAX_SCAN_ROWS` (20 000) rows per scan. Left
uncached, a few minutes of a PM clicking around burns the Sheets quota that the write
worker also depends on.

Deliberately *not* the `sheet_records` Postgres cache that CLAUDE.md and several skill
files describe — that table and `sheets/sync.py` do not exist and never did. A durable,
reconciled read model is a much larger piece of work; this is the proportionate step and
is honest about being a cache: short TTL, explicit invalidation after writes, and a cold
miss simply costs one normal scan.
"""

import json
import logging
from typing import Any, List, Optional, Tuple

import redis.asyncio as aioredis

from app.config import settings
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


async def get_tab_matrix(
    service: Any,
    spreadsheet_id: str,
    active_tab: str,
    data_start_row: int,
) -> Tuple[List[str], List[List[str]], bool]:
    """Return (headers, rows, truncated) for a tab, from cache when warm.

    `headers` come back stripped, matching `meta.get_header_row`; callers index them the
    same way the rest of the read path does (`{h.lower().strip(): i}`) so a header with a
    real trailing space still resolves (§7.2).

    Redis being down must not take the dashboard with it — a cache is an optimisation, so
    every Redis failure here degrades to a live Sheets scan rather than an error.
    """
    key = _key(spreadsheet_id, active_tab)
    try:
        cached = await redis_client.get(key)
        if cached:
            payload = json.loads(cached)
            return payload["headers"], payload["rows"], payload["truncated"]
    except Exception as e:
        logger.warning(f"Row cache read failed for {key}, falling back to Sheets: {e}")

    header_row_num = data_start_row - 1
    headers = await get_header_row(service, spreadsheet_id, active_tab, header_row_num)
    rows, truncated = await _fetch_all_rows(service, spreadsheet_id, active_tab, data_start_row)

    try:
        await redis_client.set(
            key,
            json.dumps({"headers": headers, "rows": rows, "truncated": truncated}, ensure_ascii=False),
            ex=CACHE_TTL_SECONDS,
        )
    except Exception as e:
        logger.warning(f"Row cache write failed for {key}: {e}")

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
    """
    conn = client or redis_client
    try:
        if active_tab:
            await conn.delete(_key(spreadsheet_id, active_tab))
            return
        async for key in conn.scan_iter(match=f"{CACHE_PREFIX}:{spreadsheet_id}:*"):
            await conn.delete(key)
    except Exception as e:
        logger.warning(f"Row cache invalidation failed for {spreadsheet_id}/{active_tab}: {e}")
