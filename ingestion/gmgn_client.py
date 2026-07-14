"""
Thin client around the GMGN OpenAPI (https://openapi.gmgn.ai), scoped to the
"exist auth" read endpoints the pipeline needs on Robinhood chain:

  - token top traders / holders  (who trades a token, with per-wallet buy/sell
    volume, USD cost and realized profit already computed by GMGN)
  - per-wallet activity          (individual buy/sell events: amount, USD price,
    timestamp — the raw material we aggregate per wallet)
  - per-wallet stats             (winrate, realized/unrealized PnL, pnl ratio,
    tags — GMGN's own cross-token verdict on a wallet)

Auth (verified 2026-07-13 against the live API and the gmgn-cli source):
  "exist auth" routes need ONLY the API key. Each request carries:
    header  X-APIKEY: <GMGN_API_KEY>
    query   timestamp=<unix seconds>  (server allows ±5s skew)
    query   client_id=<uuid>          (replays rejected within ~7s)
  No request signature is required for these routes, so no Ed25519 private key
  is needed for the core pipeline. Signed routes (wallet_holdings, swaps) would
  additionally need X-Signature over the private key — not implemented here.

Rate limits: GMGN uses a leaky bucket (rate=20, capacity=20) weighted per
route (traders/holders=5, activity/stats=3). We throttle client-side and, on
429, respect reset_at / X-RateLimit-Reset and retry once. Spamming during a
cooldown extends a RATE_LIMIT_BANNED window, so we back off rather than hammer.

Everything here is read-only HTTP GET. Nothing signs or sends transactions.
"""

import time
import uuid
import logging

import requests

import config

logger = logging.getLogger(__name__)

_session = requests.Session()
_session.headers.update(
    {
        "Accept": "application/json",
        "User-Agent": "rh-wallet-tracker-bot/0.1",
    }
)

_last_request_ts = 0.0


class GmgnError(Exception):
    """Raised when the GMGN API returns an unrecoverable error."""


class GmgnRateLimited(GmgnError):
    """Raised when rate-limited and unable to recover within one retry."""

    def __init__(self, message: str, reset_at: int | None = None):
        super().__init__(message)
        self.reset_at = reset_at


def _throttle(sub_path: str) -> None:
    """Client-side spacing so we stay under the per-route leaky-bucket rate."""
    global _last_request_ts
    weight = config.GMGN_ROUTE_WEIGHTS.get(sub_path, 3)
    min_gap = max(config.GMGN_MIN_REQUEST_DELAY, weight / config.GMGN_BUCKET_RATE)
    elapsed = time.time() - _last_request_ts
    if elapsed < min_gap:
        time.sleep(min_gap - elapsed)
    _last_request_ts = time.time()


def _auth_params() -> dict:
    return {"timestamp": int(time.time()), "client_id": str(uuid.uuid4())}


def _get(sub_path: str, params: dict | None = None) -> dict:
    """
    GET an exist-auth GMGN route and return the parsed `data` payload.

    GMGN wraps responses as {"code": 0, "data": {...}} on success; a non-zero
    code (or HTTP >=400) is an API-level error we surface rather than guess past.
    """
    if not config.GMGN_API_KEY:
        raise GmgnError(
            "GMGN_API_KEY is not set. Add it to your environment or .env "
            "(apply for a key via GMGN's cooperation form)."
        )

    url = f"{config.GMGN_BASE_URL}{sub_path}"
    headers = {"X-APIKEY": config.GMGN_API_KEY}

    for attempt in range(2):
        _throttle(sub_path)
        query = {**(params or {}), **_auth_params()}
        resp = _session.get(
            url, params=query, headers=headers, timeout=config.REQUEST_TIMEOUT_SECONDS
        )

        if resp.status_code == 429:
            reset_at = _extract_reset_at(resp)
            if attempt == 0 and reset_at:
                wait = max(0, reset_at - int(time.time())) + 1
                if wait <= 30:  # short cooldown: wait it out once, then retry
                    logger.warning("GMGN rate limited on %s, waiting %ds", sub_path, wait)
                    time.sleep(wait)
                    continue
            raise GmgnRateLimited(
                f"GMGN rate limited on {sub_path}"
                + (f"; retry after {reset_at} (unix)" if reset_at else ""),
                reset_at=reset_at,
            )

        if resp.status_code >= 400:
            raise GmgnError(
                f"GMGN GET {sub_path} failed: {resp.status_code} {resp.text[:300]}"
            )

        body = resp.json()
        code = body.get("code")
        if code not in (0, "0", None):
            raise GmgnError(
                f"GMGN GET {sub_path} returned code={code}: "
                f"{body.get('message') or body.get('error')}"
            )
        return body.get("data", {}) or {}

    raise GmgnError(f"GMGN GET {sub_path} failed after retry")


def _extract_reset_at(resp: requests.Response) -> int | None:
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("reset_at"):
            return int(body["reset_at"])
    except Exception:
        pass
    hdr = resp.headers.get("X-RateLimit-Reset")
    return int(hdr) if hdr and hdr.isdigit() else None


# --------------------------------------------------------------------------- #
# Token-level: who trades this token
# --------------------------------------------------------------------------- #
def get_token_top_traders(
    token_address: str,
    tag: str | None = None,
    order_by: str = "profit",
    direction: str = "desc",
    limit: int = 100,
) -> list[dict]:
    """
    Top traders for a token on Robinhood chain, WITH GMGN's per-wallet buy/sell
    volume and realized profit already aggregated. This is the discovery step:
    it yields the candidate wallet list plus a first cut of total buy/sell/profit
    per wallet — no pool lookup or price reconstruction needed.

    tag: optional GMGN wallet-label filter — e.g. "rat_trader" (insider/sneak),
    "smart_degen" (smart money), "sniper". None returns all traders.

    Note: the list often includes the pool/router contract itself (addr_type 2,
    exchange like "uniswap_v3"); callers should filter those out — see
    classify.is_pool_like / the pair address from DEX Screener.
    """
    params: dict = {
        "chain": config.GMGN_CHAIN_SLUG,
        "address": token_address,
        "order_by": order_by,
        "direction": direction,
        "limit": min(limit, 100),
    }
    if tag:
        params["tag"] = tag
    data = _get("/v1/market/token_top_traders", params)
    return data.get("list", []) if isinstance(data, dict) else (data or [])


def get_token_top_holders(
    token_address: str,
    tag: str | None = None,
    order_by: str = "amount_percentage",
    limit: int = 100,
) -> list[dict]:
    """Current top holders of a token (subset of traders that still hold)."""
    params: dict = {
        "chain": config.GMGN_CHAIN_SLUG,
        "address": token_address,
        "order_by": order_by,
        "direction": "desc",
        "limit": min(limit, 100),
    }
    if tag:
        params["tag"] = tag
    data = _get("/v1/market/token_top_holders", params)
    return data.get("list", []) if isinstance(data, dict) else (data or [])


# --------------------------------------------------------------------------- #
# Wallet-level: what a wallet did
# --------------------------------------------------------------------------- #
def get_wallet_activity(
    wallet_address: str,
    token_address: str | None = None,
    types: list[str] | None = None,
    max_pages: int | None = None,
) -> list[dict]:
    """
    All buy/sell (and optionally transfer) events for a wallet, following the
    `next` cursor across pages. Each event carries token_amount, cost_usd,
    price_usd and timestamp — the raw rows we aggregate into totals per wallet.

    types: filter, e.g. ["buy", "sell"]. token_address: restrict to one token.
    """
    max_pages = max_pages or config.GMGN_MAX_PAGES
    activities: list[dict] = []
    cursor: str | None = None

    for _ in range(max_pages):
        params: dict = {"chain": config.GMGN_CHAIN_SLUG, "wallet_address": wallet_address}
        if token_address:
            params["token_address"] = token_address
        if types:
            params["type"] = types
        if cursor:
            params["cursor"] = cursor

        data = _get("/v1/user/wallet_activity", params)
        page = data.get("activities", []) if isinstance(data, dict) else []
        activities.extend(page)

        cursor = data.get("next") if isinstance(data, dict) else None
        if not cursor or not page:
            break

    return activities


def get_wallet_stats(wallet_addresses: list[str], period: str = "7d") -> list[dict]:
    """
    GMGN's own trading stats for one or more wallets (batch): winrate,
    realized/unrealized profit, pnl ratio, buy/sell counts, and `common.tags`
    (e.g. ["smart_money"]). This is GMGN's cross-token verdict on the wallet,
    complementing the per-token aggregates from get_token_top_traders.

    Returns a list of stat objects (one per wallet); shape is normalized so a
    single-wallet response and a batch response both come back as a list.
    """
    params: dict = {
        "chain": config.GMGN_CHAIN_SLUG,
        "wallet_address": wallet_addresses,
        "period": period,
    }
    data = _get("/v1/user/wallet_stats", params)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Some responses nest the batch under a key; otherwise it's one object.
        for key in ("list", "stats", "wallets"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return []
