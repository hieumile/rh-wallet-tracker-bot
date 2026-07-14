"""
Thin client around the Blockscout Pro API (v2) scoped to Robinhood Chain.

Covers exactly what the pipeline needs right now:
  - resolve a unix timestamp to the nearest block number (binary search)
  - pull paginated token transfers for a given token contract
  - fetch full transfer/log detail for a single transaction (for swap
    classification / price reconstruction downstream)

Nothing here executes trades or writes anywhere — read-only HTTP GETs.
"""

import time
import logging
import requests

import config

logger = logging.getLogger(__name__)

_session = requests.Session()
_session.headers.update({"Accept": "application/json"})
if config.BLOCKSCOUT_API_KEY:
    _session.headers.update({"Authorization": f"Bearer {config.BLOCKSCOUT_API_KEY}"})


class BlockscoutError(Exception):
    """Raised when the Blockscout API returns an unrecoverable error."""


def _get(path: str, params: dict | None = None) -> dict:
    """
    GET against the Blockscout Pro API with basic rate-limit backoff.
    Retries once on 429 after respecting Retry-After (or a flat delay).
    """
    url = f"{config.BLOCKSCOUT_BASE_URL}{path}"
    for attempt in range(2):
        resp = _session.get(url, params=params, timeout=config.REQUEST_TIMEOUT_SECONDS)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", 2.0))
            logger.warning("Rate limited on %s, sleeping %.1fs", path, retry_after)
            time.sleep(retry_after)
            continue
        if resp.status_code >= 400:
            raise BlockscoutError(
                f"Blockscout GET {path} failed: {resp.status_code} {resp.text[:300]}"
            )
        time.sleep(config.BLOCKSCOUT_REQUEST_DELAY)  # stay under 5 RPS
        return resp.json()
    raise BlockscoutError(f"Blockscout GET {path} failed after retry (still rate limited)")


def get_latest_block_number() -> int:
    """Current chain height, used as the upper bound for binary search."""
    data = _get("/main-page/blocks")
    if not data:
        raise BlockscoutError("Could not determine latest block number")
    # main-page/blocks returns a list of recent blocks, newest first
    return int(data[0]["height"])


def get_block(block_number: int) -> dict:
    """Fetch a single block's detail, including its unix timestamp."""
    return _get(f"/blocks/{block_number}")


def block_timestamp(block_number: int) -> int:
    """Return the unix timestamp (seconds) of a given block."""
    block = get_block(block_number)
    # Blockscout returns ISO8601; normalize to unix seconds.
    from datetime import datetime

    ts_str = block["timestamp"]
    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    return int(dt.timestamp())


def get_block_by_timestamp(target_ts: int, latest_block: int | None = None) -> int:
    """
    Binary search for the block number whose timestamp is closest to
    (and not after) target_ts. Robinhood Chain's ~0.1s block time means
    this converges in relatively few requests even over long ranges,
    but each step is a network call — cache results if calling repeatedly
    for the same time range.
    """
    hi = latest_block or get_latest_block_number()
    lo = 0
    result = 0

    while lo <= hi:
        mid = (lo + hi) // 2
        ts = block_timestamp(mid)
        if ts <= target_ts:
            result = mid
            lo = mid + 1
        else:
            hi = mid - 1

    return result


def get_token_transfers(token_address: str, start_block: int, end_block: int) -> list[dict]:
    """
    Pull all token-transfer events for a given ERC-20 token contract
    between start_block and end_block (inclusive), across as many pages
    as needed. Stops early once transfers pass end_block (Blockscout
    returns newest-first, so we page until we're past the window).

    Returns raw transfer dicts as given by the API — classification into
    buy/sell happens in classify.py, not here.
    """
    transfers: list[dict] = []
    params: dict = {}
    path = f"/tokens/{token_address}/transfers"

    for _ in range(config.MAX_PAGES_PER_TOKEN):
        data = _get(path, params=params or None)
        items = data.get("items", [])
        if not items:
            break

        stop = False
        for item in items:
            block_num = int(item["block_number"])
            if block_num < start_block:
                stop = True
                break
            if block_num <= end_block:
                transfers.append(item)
            # block_num > end_block: too recent, skip but keep paging back in time

        if stop:
            break

        next_params = data.get("next_page_params")
        if not next_params:
            break
        params = next_params

    return transfers


def get_transaction_token_transfers(tx_hash: str) -> list[dict]:
    """
    Full set of token transfers that occurred inside a single transaction.
    A swap typically shows two transfers moving in opposite directions
    (token_in from wallet to pool, token_out from pool to wallet) —
    this is the raw material for price reconstruction in classify.py.
    """
    data = _get(f"/transactions/{tx_hash}/token-transfers")
    return data.get("items", [])


def get_transaction_logs(tx_hash: str) -> list[dict]:
    """
    Raw event logs for a transaction (e.g. Uniswap-v2-style Sync/Swap
    events), used to recover pool reserves at the moment of a trade.
    """
    data = _get(f"/transactions/{tx_hash}/logs")
    return data.get("items", [])