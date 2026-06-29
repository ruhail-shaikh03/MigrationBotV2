import time
from googleapiclient.errors import HttpError

def _with_retry(fn, max_attempts: int = 4, base_delay: float = 1.0):
    """
    Execute fn() with exponential backoff on transient Google Sheets API
    errors (HTTP 429 Too Many Requests, 500, 503 Service Unavailable).
    """
    _TRANSIENT_CODES = {429, 500, 503}
    delay = base_delay
    last_exc = None

    for attempt in range(max_attempts):
        try:
            return fn()
        except HttpError as exc:
            if exc.status_code not in _TRANSIENT_CODES:
                raise
            last_exc = exc
            if attempt < max_attempts - 1:
                time.sleep(delay)
                delay *= 2
        except Exception:
            raise

    if last_exc:
        raise last_exc
