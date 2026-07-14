"""
Central config for the RH Chain wallet-tracking pipeline.

No secrets live here — only non-sensitive constants. API keys are read from
environment variables / a local .env (see .env.example).

Data-source strategy (verified 2026-07-13 against live endpoints):
  - GMGN OpenAPI (https://openapi.gmgn.ai) is the PRIMARY source. It supports
    Robinhood chain (chain slug "robinhood") for token traders/holders and
    per-wallet activity/stats, and returns buy/sell volumes, USD prices and
    realized profit DIRECTLY — so we no longer need to find trading pools or
    reconstruct prices from raw swap events.
  - DEX Screener (chainId "robinhood", confirmed) is kept only as a light
    token-discovery / metadata helper (liquidity, pair address to filter the
    pool contract out of trader lists). Not used for historical price anymore.
  - Blockscout (robinhoodchain.blockscout.com) is now OPTIONAL/legacy. Its
    public /api/v2 returns 503 to datacenter IPs and RH-chain DEXs are Uniswap
    v3 (concentrated liquidity), which the old v2 price-reconstruction in
    ingestion/classify.py cannot price correctly. Left in the tree for
    reference but off the main path.
"""

import os

try:  # optional: load a local .env if python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is a convenience, not required
    pass

# --- Chain identifiers ---
RH_CHAIN_ID = 4663              # Robinhood Chain mainnet
GMGN_CHAIN_SLUG = "robinhood"   # GMGN + DEX Screener both use this slug

# --- GMGN OpenAPI (primary source) ---
GMGN_BASE_URL = "https://openapi.gmgn.ai"
GMGN_API_KEY = os.environ.get("GMGN_API_KEY", "")
# Ed25519 private key (PEM). Only needed for "signed" routes (wallet holdings,
# swaps, follow-wallet). The core read pipeline uses "exist auth" routes
# (token traders, wallet activity/stats) which need the API key only.
GMGN_PRIVATE_KEY = os.environ.get("GMGN_PRIVATE_KEY", "")

# GMGN leaky-bucket limiter: rate=20, capacity=20, weighted per route.
# Sustainable rate for a route ≈ 20 / weight requests per second. We throttle
# client-side to stay comfortably under that and avoid RATE_LIMIT_BANNED (5m).
GMGN_BUCKET_RATE = 20
GMGN_ROUTE_WEIGHTS = {
    "/v1/market/token_top_traders": 5,
    "/v1/market/token_top_holders": 5,
    "/v1/user/wallet_activity": 3,
    "/v1/user/wallet_stats": 3,
    "/v1/user/wallet_token_balance": 1,
    "/v1/user/info": 1,
}
GMGN_MIN_REQUEST_DELAY = 0.30   # floor between any two GMGN calls (seconds)
GMGN_MAX_PAGES = 50             # cap on cursor pagination per wallet (safety)

# --- DEX Screener (token discovery / metadata only) ---
DEXSCREENER_BASE_URL = "https://api.dexscreener.com"
DEXSCREENER_CHAIN_SLUG = "robinhood"  # verified against live API 2026-07-13

# --- Blockscout (legacy / optional) ---
BLOCKSCOUT_BASE_URL = "https://robinhoodchain.blockscout.com/api/v2"
BLOCKSCOUT_API_KEY = os.environ.get("BLOCKSCOUT_API_KEY", "")
BLOCKSCOUT_REQUEST_DELAY = 0.25
MAX_PAGES_PER_TOKEN = 500

# --- Wallet selection / scoring thresholds ---
# Tags GMGN assigns that we treat as "insider-like" for discovery. See
# gmgn token --tag values: rat_trader (insider/sneak), smart_degen (smart
# money), sniper (bought at launch). Set to None to consider all traders.
INSIDER_TAGS = ["rat_trader", "smart_degen", "sniper"]

# Wallet scoring engine (Subsystem 2). The score is a wallet's CROSS-TOKEN
# track record (from GMGN wallet_stats over STATS_PERIOD), not its record on
# the single seed token — the seed token only supplies the candidate pool.
STATS_PERIOD = "30d"             # wallet_stats window: "7d" or "30d"
MIN_TOKEN_NUM = 3                # exclude one/two-hit wonders (too little history)
MIN_REALIZED_PROFIT_USD = 0.0    # must be net profitable over the period to follow
MIN_WINRATE = 0.0                # optional floor (0..1); 0 = no floor

# Composite score weights (points out of 100). Each component is normalized to
# 0..1 then multiplied by its weight; see scoring/wallet_scorer.py.
SCORE_WEIGHTS = {
    "winrate": 25,      # ratio of profitable trades
    "pnl_ratio": 20,    # realized_profit / total_cost
    "profit": 20,       # absolute realized profit (log-scaled)
    "moonshot": 15,     # share of trades that returned > 2x
    "experience": 10,   # number of distinct tokens traded (anti-luck)
    "tags": 10,         # GMGN smart_money / insider tags
}
PROFIT_FULL_SCORE_USD = 100_000  # realized profit that maxes the profit component
EXPERIENCE_FULL_TOKENS = 30      # token_num that maxes the experience component

# Persisted output that Subsystem 3 (signal generator) will consume.
WATCHLIST_PATH = "watchlist.json"

# --- HTTP ---
REQUEST_TIMEOUT_SECONDS = 20
