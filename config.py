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
MIN_REALIZED_PROFIT_USD = 100.0  # must make at least $100 net profit over the period
MIN_VOLUME_USD = 500.0           # must trade at least $500 total volume to ensure active trading
MIN_WINRATE = 0.40               # require at least 40% winrate to avoid lucky gamblers
MIN_HOLDING_TIME_SECS = 60       # discard high-frequency MEV/sandwich bots (under 1m hold)
EXCLUDE_TAGS = {"sandwich_bot", "uniswap_v3_multicall"}
MAX_DRAWDOWN_RATIO_LIMIT = 0.60  # exclude wallets with max drawdown > 60% of peak profit
MAX_TX_COUNT = 500               # exclude wallets with > 500 transactions in period (trading bots)
MIN_WATCHLIST_SCORE = 40.0       # minimum composite score required to enter the watchlist
PROFIT_FACTOR_CAP = 2.0          # profit factor that maxes the profit_factor component
SHARPE_CAP = 1.5                 # Sharpe ratio that maxes the sharpe component
VOLUME_FULL_SCORE_USD = 200_000  # trading volume (USD) that maxes the volume component

# Ingestion Settings
MAX_ONCHAIN_TRANSACTIONS = 2000  # default cap on unique transactions processed on-chain
MIN_WALLET_AGE_DAYS = 2.0        # only follow wallets that are at least 2 days old (filters fresh burners)


# Composite score weights (points out of 100). Each component is normalized to
# 0..1 then multiplied by its weight; see scoring/wallet_scorer.py.
SCORE_WEIGHTS = {
    "winrate": 10,      # ratio of profitable trades
    "pnl_ratio": 10,    # realized_profit / total_cost
    "profit": 15,       # absolute realized profit (log-scaled)
    "volume": 10,       # overall trading volume (log-scaled)
    "profit_factor": 20, # gross profit / gross loss (increased from 15)
    "sharpe": 15,       # risk-adjusted return ratio (increased from 10)
    "drawdown": 10,     # control of maximum asset drawdown from peak
    "moonshot": 10,     # share of trades that returned > 2x
}
PROFIT_FULL_SCORE_USD = 100_000  # realized profit that maxes the profit component

# Persisted output that Subsystem 3 (signal generator) will consume.
WATCHLIST_PATH = "output/watchlist.json"

# --- Subsystem 3 (Signal Generator) ---
# Common quote tokens on Robinhood Chain (in lowercase) to ignore as target assets
QUOTE_TOKENS = {
    "0x0bd7d308f8e1639fab988df18a8011f41eacad73": "WETH",
}
SIGNAL_LOOKBACK_HOURS = 24
SIGNAL_STATE_PATH = "output/signal_state.json"
MIN_SIGNAL_BUY_USD = 100.0  # Discard buys below this USD size (lottery tickets)
MIN_CO_BUYERS = 2           # Minimum unique buyers required to flag a Co-Investment
MIN_NET_ACCUMULATION_RATIO = 2.0  # Buying USD must be at least this multiple of selling USD

# --- HTTP ---
# Timeout for all outgoing HTTP requests
REQUEST_TIMEOUT_SECONDS = 20
