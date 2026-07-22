import asyncio
from concurrent.futures import ThreadPoolExecutor
from googleapiclient.errors import HttpError

_executor = ThreadPoolExecutor(max_workers=4)

async def _with_retry(fn, max_attempts: int = 4, base_delay: float = 1.0):
    """
    Execute fn() with exponential backoff on transient Google Sheets API
    errors (HTTP 429 Too Many Requests, 500, 503 Service Unavailable).
    Runs synchronous API call fn() in thread executor to prevent blocking the event loop.
    """
    _TRANSIENT_CODES = {429, 500, 503}
    delay = base_delay
    last_exc = None
    loop = asyncio.get_running_loop()

    for attempt in range(max_attempts):
        try:
            return await loop.run_in_executor(_executor, fn)
        except HttpError as exc:
            if exc.status_code not in _TRANSIENT_CODES:
                raise
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(delay)
                delay *= 2
        except Exception:
            raise

    if last_exc:
        raise last_exc
