"""
Thin client around DEX Screener's public API. No API key required, but it's
an unofficial/rate-limited surface for heavy use — treat this as
supplementary (pair discovery, current liquidity), not the source of truth
for historical price. Historical price/reserves at a specific past trade
should be reconstructed from the swap event itself via Blockscout
(see classify.py), not pulled from here.
"""

import time
import logging
import requests

import config

logger = logging.getLogger(__name__)

_session = requests.Session()
_session.headers.update({"Accept": "application/json"})


class DexScreenerError(Exception):
    pass


def _get(path: str, params: dict | None = None) -> dict:
    url = f"{config.DEXSCREENER_BASE_URL}{path}"
    resp = _session.get(url, params=params, timeout=config.REQUEST_TIMEOUT_SECONDS)
    if resp.status_code == 429:
        logger.warning("DEX Screener rate limited on %s, backing off", path)
        time.sleep(2.0)
        resp = _session.get(url, params=params, timeout=config.REQUEST_TIMEOUT_SECONDS)
    if resp.status_code >= 400:
        raise DexScreenerError(f"DEX Screener GET {path} failed: {resp.status_code} {resp.text[:300]}")
    return resp.json()


def get_pairs_for_token(token_address: str) -> list[dict]:
    """
    Return all known trading pairs for a token address, across whatever
    chains DEX Screener has indexed it on. Filter by chainId == "robinhood"
    (VERIFY this slug against DEX Screener's own listing before trusting it —
    chain slugs are assigned by DEX Screener and this is unconfirmed for RH
    Chain specifically) to keep only Robinhood-Chain-native pairs.
    """
    data = _get(f"/latest/dex/tokens/{token_address}")
    pairs = data.get("pairs") or []
    return [p for p in pairs if p.get("chainId") == config.DEXSCREENER_CHAIN_SLUG]


def get_primary_pair(token_address: str) -> dict | None:
    """
    Pick the pair with the highest current liquidity for a token — used as
    "the" pool address/liquidity reference when a token trades across
    multiple pairs. Returns None if the token has no indexed RH-chain pair
    yet (e.g. brand new token, or DEX Screener hasn't indexed it yet).
    """
    pairs = get_pairs_for_token(token_address)
    if not pairs:
        return None
    return max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))


def search_token_address_by_symbol(symbol: str) -> str | None:
    """
    Search DEX Screener for pairs matching the symbol, filter for chainId == "robinhood",
    and return the address of the token if found.
    """
    try:
        url = f"/latest/dex/search"
        data = _get(url, params={"q": symbol})
        pairs = data.get("pairs") or []
        rh_pairs = [p for p in pairs if p.get("chainId") == config.DEXSCREENER_CHAIN_SLUG]
        if not rh_pairs:
            return None
        rh_pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)
        for p in rh_pairs:
            base = p.get("baseToken", {})
            if base.get("symbol", "").lower() == symbol.lower():
                return base.get("address")
    except Exception as e:
        logger.warning("DEX Screener search failed for %s: %s", symbol, e)
    return None