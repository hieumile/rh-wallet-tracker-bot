"""
GMGN-based pipeline: given a token on Robinhood chain, find the wallets
trading it (optionally filtered to insider-like tags), and aggregate each
wallet's total buy, total sell and profit.

This replaces the old Blockscout + DEX Screener + FIFO price-reconstruction
approach. GMGN returns buy/sell volume, USD prices and realized profit
directly, so there is no pool discovery or price reconstruction here.

Pipeline:
  1. token top traders (GMGN)          -> candidate wallets + tags
  2. drop the pool/router contract     (via DEX Screener pair address + heuristics)
  3. keep insider-tagged wallets       (rat_trader / smart_degen / sniper), or all
  4. per-wallet activity in the window -> total buy/sell + realized profit for the
                                          date range (from GMGN's per-sell cost basis)
  5. per-wallet stats                  -> GMGN winrate / 7d profit / tags
  6. flag insider signals, rank, print, optionally export .xlsx

Time windows: token_top_traders is all-time, so buy/sell/profit for a specific
window are computed from wallet_activity events (each carries a unix timestamp).
Windows support hour precision and can be given multiple times; an event counts
if it falls in ANY window. Realized profit in-window = sum over in-window sells
of (cost_usd - buy_cost_usd), i.e. proceeds minus GMGN's own cost basis.

Usage:
    # all-time aggregates
    python main.py <token> --tag smart_degen --limit 50 --export out.xlsx
    # single window, day granularity
    python main.py <token> --from 2026-07-08 --to 2026-07-12 --export out.xlsx
    # multiple hour-level windows
    python main.py <token> \
        --window "13/7/2026 12:00" "13/7/2026 15:00" \
        --window "13/7/2026 17:00" "13/7/2026 18:00" --txns txns.xlsx

Requires GMGN_API_KEY (env or .env). The core path needs only the API key —
no Ed25519 private key (that's only for holdings/swaps).
"""

import sys
import argparse
import logging
from datetime import datetime, timezone

import config
from ingestion import gmgn_client as gmgn
from ingestion import dexscreener_client as dex
from ingestion import blockscout_client as bs
from scoring import wallet_aggregator as agg
from scoring import wallet_scorer as scorer
from scoring import watchlist as wl

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _pool_address(token_address: str) -> str | None:
    """Best-effort: the token's primary pool address, to exclude it from traders."""
    try:
        pair = dex.get_primary_pair(token_address)
    except Exception as e:  # DEX Screener is a nice-to-have here, not required
        logger.warning("Could not fetch pair from DEX Screener: %s", e)
        return None
    return pair.get("pairAddress", "").lower() if pair else None


def is_pool_like(row: dict, pool_address: str | None, token_address: str) -> bool:
    """
    A token_top_traders row can be the pool/router contract rather than a real
    trader. Filter those: known pool address, the token contract itself, rows
    tagged as an exchange venue, or rows with only transfers and no buy/sell.
    """
    addr = (row.get("address") or "").lower()
    if not addr:
        return True
    if pool_address and addr == pool_address:
        return True
    if addr == token_address.lower():
        return True
    if row.get("exchange"):  # e.g. "uniswap_v3" — this row IS the venue
        return True
    return False


def build_aggregates(token_address: str, tag: str | None, limit: int) -> list[agg.WalletAggregate]:
    traders = gmgn.get_token_top_traders(
        token_address, tag=tag, order_by="profit", limit=limit
    )
    logger.info("GMGN returned %d trader rows%s", len(traders),
                f" (tag={tag})" if tag else "")

    pool = _pool_address(token_address)
    if pool:
        logger.info("Excluding pool contract %s", pool)

    aggregates: list[agg.WalletAggregate] = []
    for row in traders:
        if is_pool_like(row, pool, token_address):
            continue
        aggregates.append(agg.from_trader_row(row, token_address))
    logger.info("%d real wallets after filtering pool/contract rows", len(aggregates))
    return aggregates


def enrich_with_stats(aggregates: list[agg.WalletAggregate], period: str) -> None:
    """
    Overlay GMGN wallet_stats (winrate / pnl / tags / distribution) onto each
    aggregate over `period` — the cross-token track record the scorer needs.

    Note: despite the docs claiming batch support, wallet_stats on Robinhood
    chain returns only the FIRST wallet when passed multiple, so we query one
    wallet per call. Each call is weight 3 (~6/s ceiling); the client throttles.
    """
    by_wallet = {a.wallet: a for a in aggregates}
    for wallet in list(by_wallet):
        try:
            stats_list = gmgn.get_wallet_stats([wallet], period=period)
        except gmgn.GmgnError as e:
            logger.warning("wallet_stats failed for %s: %s", wallet, e)
            continue
        for stats in stats_list:
            w = (stats.get("wallet_address") or stats.get("address") or "").lower()
            if w in by_wallet:
                agg.apply_stats(by_wallet[w], stats)
                by_wallet[w].stats_period = period
                
                # Fetch wallet activity to calculate max drawdown, profit factor, and Sharpe ratio
                try:
                    from scoring import wallet_scorer
                    # Fetch 1 page of recent transactions (50 events)
                    activities = gmgn.get_wallet_activity(w, max_pages=1)
                    dd_ratio, pf, sharpe = wallet_scorer.calculate_advanced_metrics(activities)
                    by_wallet[w].wallet_max_drawdown_ratio = dd_ratio
                    by_wallet[w].wallet_profit_factor = pf
                    by_wallet[w].wallet_sharpe_ratio = sharpe
                except Exception as ae:
                    logger.warning("Failed to calculate advanced metrics for %s: %s", w, ae)


def enrich_with_activity(
    aggregates: list[agg.WalletAggregate],
    token_address: str,
    windows: list[tuple[int | None, int | None]] | None = None,
    drop_empty: bool = False,
) -> list[agg.WalletAggregate]:
    """
    Re-aggregate each wallet's totals + realized profit from raw activity events,
    restricted to the given time `windows` when set (an event counts if it falls
    in ANY window). One paginated call per wallet. When drop_empty is set,
    wallets with no buy/sell in the windows are removed. Returns the survivors.
    """
    survivors: list[agg.WalletAggregate] = []
    for a in aggregates:
        try:
            events = gmgn.get_wallet_activity(
                a.wallet, token_address=token_address, types=["buy", "sell"]
            )
        except gmgn.GmgnError as e:
            logger.warning("activity failed for %s: %s", a.wallet, e)
            survivors.append(a)  # keep discovery data rather than silently drop
            continue

        recomputed = agg.aggregate_activity(
            a.wallet, events, token_address=token_address, windows=windows,
        )
        if drop_empty and recomputed.buy_count == 0 and recomputed.sell_count == 0:
            continue

        a.total_buy_usd = recomputed.total_buy_usd
        a.total_sell_usd = recomputed.total_sell_usd
        a.total_buy_tokens = recomputed.total_buy_tokens
        a.total_sell_tokens = recomputed.total_sell_tokens
        a.buy_count = recomputed.buy_count
        a.sell_count = recomputed.sell_count
        a.realized_profit_usd = recomputed.realized_profit_usd  # window realized profit
        a.first_ts = recomputed.first_ts
        a.last_ts = recomputed.last_ts
        survivors.append(a)
    return survivors


def collect_transactions(
    aggregates: list[agg.WalletAggregate],
    token_address: str,
    windows: list[tuple[int | None, int | None]] | None = None,
) -> list[dict]:
    """
    Pull raw buy/sell activity events for every discovered wallet on this token,
    keeping events that fall in ANY of the given time `windows` (all, if none).
    Returns the raw GMGN event dicts — the raw ingested transactions, for
    inspection/verification.
    """
    events: list[dict] = []
    for a in aggregates:
        try:
            wallet_events = gmgn.get_wallet_activity(
                a.wallet, token_address=token_address, types=["buy", "sell"]
            )
        except gmgn.GmgnError as e:
            logger.warning("activity failed for %s: %s", a.wallet, e)
            continue
        for ev in wallet_events:
            ts = ev.get("timestamp")
            ts = int(ts) if ts is not None else None
            if agg.in_windows(ts, windows):
                events.append(ev)
    return events



def print_table(aggregates: list[agg.WalletAggregate]) -> None:
    aggregates.sort(key=lambda a: (a.realized_profit_usd or a.net_usd or 0), reverse=True)
    print(
        f"\n{'wallet':<44} {'buy$':>13} {'sell$':>13} "
        f"{'profit$':>13} {'win%':>6}  {'insider signals':<40}"
    )
    for a in aggregates:
        profit = a.realized_profit_usd if a.realized_profit_usd is not None else a.net_usd
        win = f"{a.winrate*100:5.1f}" if a.winrate is not None else "   - "
        signals = ", ".join(agg.insider_signals(a))[:40]
        print(
            f"{a.wallet:<44} {a.total_buy_usd:>13,.0f} {a.total_sell_usd:>13,.0f} "
            f"{profit:>13,.0f} {win:>6}  {signals:<40}"
        )
    if not aggregates:
        print("No wallets found for this token/tag/window — try --all or a wider range.")


def print_scores(scores: list[scorer.WalletScore]) -> None:
    print(
        f"\n=== WALLETS WORTH FOLLOWING (ranked) ===\n"
        f"{'wallet':<44} {'score':>6} {'win%':>6} {'profit$':>10} "
        f"{'vol$':>10} {'pf':>5} {'sr':>5} {'txs':>5} {'dd%':>5}  {'signals':<32}"
    )
    for s in scores:
        win = f"{s.winrate*100:5.1f}" if s.winrate is not None else "   - "
        profit = s.realized_profit_usd if s.realized_profit_usd is not None else 0.0
        vol = s.volume_usd if s.volume_usd is not None else 0.0
        pf_str = f"{s.profit_factor:4.2f}" if s.profit_factor is not None else "  - "
        sr_str = f"{s.sharpe_ratio:4.2f}" if s.sharpe_ratio is not None else "  - "
        txs_str = f"{s.tx_count:4d}" if s.tx_count is not None else "   -"
        dd_str = f"{s.max_drawdown_ratio*100:4.1f}" if s.max_drawdown_ratio is not None else "  - "
        sig = ", ".join(s.insider_signals)[:32]
        print(
            f"{s.wallet:<44} {s.score:>6.1f} {win:>6} {profit:>10,.0f} "
            f"{vol:>10,.0f} {pf_str:>5} {sr_str:>5} {txs_str:>5} {dd_str:>5}  {sig:<32}"
        )
    if not scores:
        print("No wallets cleared the follow-worthiness filters "
              f"(min {config.MIN_TOKEN_NUM} tokens, profit >= ${config.MIN_REALIZED_PROFIT_USD:,.0f}).")


# Accepted date/time inputs (all interpreted as UTC). Slash form is day-first
# (13/7/2026), matching the user's convention; dash form is ISO (2026-07-13).
_DT_FORMATS = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
]


def _parse_dt(text: str, end: bool = False) -> int:
    """
    Parse a flexible UTC date/time into a unix timestamp. Supports date-only
    ('2026-07-13', '13/7/2026') and date+time ('13/7/2026 12:00'). For a
    date-only value used as a window END, the bound extends to 23:59:59.
    """
    text = text.strip()
    for fmt in _DT_FORMATS:
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if end and "%H" not in fmt:  # date-only end bound -> end of that day
            dt = dt.replace(hour=23, minute=59, second=59)
        return int(dt.timestamp())
    raise SystemExit(
        f"Could not parse date/time {text!r}. Use e.g. '2026-07-13', "
        "'13/7/2026 12:00', or '2026-07-13 15:00' (UTC)."
    )


def _ts_label(ts: int | None) -> str:
    if ts is None:
        return "…"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _build_windows(args) -> list[tuple[int | None, int | None]]:
    """Collect all requested time windows from --window pairs and --from/--to."""
    windows: list[tuple[int | None, int | None]] = []
    for pair in (args.window or []):
        start_s, end_s = pair
        windows.append((_parse_dt(start_s), _parse_dt(end_s, end=True)))
    if args.date_from or args.date_to:
        windows.append((
            _parse_dt(args.date_from) if args.date_from else None,
            _parse_dt(args.date_to, end=True) if args.date_to else None,
        ))
    return windows
def build_onchain_aggregates(token_address: str, windows: list[tuple[int | None, int | None]], limit: int = 50) -> tuple[list[agg.WalletAggregate], list[dict]]:
    """
    Fetch raw transfers from Blockscout, classify swaps against the pool,
    reconstruct prices, calculate local wallet PnL, and return (aggregates, raw_trades).
    """
    now_ts = int(datetime.now(timezone.utc).timestamp())
    # 1. Resolve block ranges
    try:
        latest_block = bs.get_latest_block_number()
    except Exception:
        latest_block = 9000000
        
    block_ranges = []
    if not windows:
        # Default: last 10,000 blocks
        block_ranges.append((latest_block - 10000, latest_block))
    else:
        for s_ts, e_ts in windows:
            s_blk = bs.get_block_by_timestamp(s_ts, latest_block) if s_ts else (latest_block - 20000)
            if s_ts:
                s_blk = max(0, s_blk - 15000)
            e_blk = bs.get_block_by_timestamp(e_ts, latest_block) if e_ts else latest_block
            if e_ts:
                e_blk = e_blk + 15000
            block_ranges.append((s_blk, e_blk))
            
    # 2. Get Uniswap Pair address
    pair_address = dex.get_primary_pair(token_address)
    pool_addr = pair_address.get("pairAddress", "").lower() if pair_address else ""
    if not pool_addr:
        logger.warning("Could not find DEX pair for token %s. On-chain classification might fail.", token_address)
        
    # Get current WETH price to help with USD value estimation
    weth_price = 3000.0  # fallback
    try:
        import requests
        r = requests.get("https://api.dexscreener.com/latest/dex/pairs/ethereum/0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640", timeout=5).json()
        pair_data = r.get("pair") or {}
        if pair_data.get("priceUsd"):
            weth_price = float(pair_data.get("priceUsd"))
    except Exception as e:
        logger.warning("Could not fetch global WETH price: %s", e)

    # 3. Pull all transfers in block ranges
    all_transfers = []
    for s_blk, e_blk in block_ranges:
        logger.info("Fetching on-chain transfers for token from block %d to %d...", s_blk, e_blk)
        try:
            transfers = bs.get_token_transfers(token_address, s_blk, e_blk)
            all_transfers.extend(transfers)
        except Exception as e:
            logger.error("Failed to fetch transfers: %s", e)
            
    logger.info("Found %d raw transfers on-chain.", len(all_transfers))
    if not all_transfers:
        return [], [], []
        
    # 4. Group transfers by transaction hash to classify trades
    tx_groups = {}
    for t in all_transfers:
        tx_hash = t.get("transaction_hash")
        tx_groups.setdefault(tx_hash, []).append(t)
        
    logger.info("Processing %d unique transactions...", len(tx_groups))
    
    trades = []
    raw_trades = []
    processed_count = 0
    
    # Cap processing to avoid rate limiting
    tx_hashes = list(tx_groups.keys())[:1000]
    if len(tx_groups) > 1000:
        logger.warning("Capping scan at 1000 transactions to avoid rate limit throttling.")
        
    capped_set = set(tx_hashes)
        
    for tx_hash, group in tx_groups.items():
        is_capped = tx_hash not in capped_set
        
        tx_legs = []
        if not is_capped:
            try:
                tx_legs = bs.get_transaction_token_transfers(tx_hash)
            except Exception:
                tx_legs = []
            
        for t in group:
            from_hash = (t.get("from") or {}).get("hash", "").lower()
            to_hash = (t.get("to") or {}).get("hash", "").lower()
            val_str = t.get("total", {}).get("value") or "0"
            decimals = int(t.get("token", {}).get("decimals") or 18)
            amount = float(val_str) / (10 ** decimals)
            block_num = int(t.get("block_number") or 0)
            approx_ts = now_ts - (latest_block - block_num) * 2
            
            side = None
            wallet = None
            if from_hash == pool_addr:
                side = "buy"
                wallet = to_hash
            elif to_hash == pool_addr:
                side = "sell"
                wallet = from_hash
                
            # Classify status
            if is_capped:
                status = "CAPPED_LIMIT_EXCEEDED"
            elif amount <= 0.0:
                status = "ZERO_VALUE_SPAM"
            elif not side or wallet == pool_addr or not wallet:
                status = "NON_TRADE_TRANSFER"
            else:
                status = "PENDING"
                
            usd_value = 0.0
            if status == "PENDING":
                # Reconstruct price using the other legs
                for leg in tx_legs:
                    leg_token = (leg.get("token") or {}).get("address_hash", "").lower()
                    leg_val = float(leg.get("total", {}).get("value") or 0.0)
                    leg_dec = int((leg.get("token") or {}).get("decimals") or 18)
                    
                    # WETH
                    if leg_token == "0x0bd7d308f8e1639fab988df18a8011f41eacad73":
                        usd_value = (leg_val / (10 ** leg_dec)) * weth_price
                        break
                    # USDC / USDT
                    elif "usd" in (leg.get("token") or {}).get("symbol", "").lower():
                        usd_value = leg_val / (10 ** leg_dec)
                        break
                        
                # Fallback
                if usd_value == 0.0 and pair_address:
                    usd_value = amount * float(pair_address.get("priceUsd") or 0.0)
                    
            price_usd = usd_value / amount if amount > 0 else 0.0
            
            raw_trades.append({
                "transaction_hash": tx_hash,
                "timestamp": approx_ts,
                "wallet": wallet or from_hash,
                "side": side or "TRANSFER",
                "token_amount": amount,
                "usd_value": usd_value,
                "status": status,
                "token": {"address": token_address, "symbol": "TOKEN"}
            })
            
            if status == "PENDING":
                trades.append({
                    "transaction_hash": tx_hash,
                    "timestamp": approx_ts,
                    "wallet": wallet,
                    "side": side,
                    "token_amount": amount,
                    "usd_value": usd_value,
                    "price": price_usd,
                })
            
        if not is_capped:
            processed_count += 1
            if processed_count % 100 == 0:
                logger.info("Processed %d/%d transactions...", processed_count, len(tx_hashes))
            
    # 5. Group trades by wallet to calculate PnL
    wallet_trades = {}
    for tr in trades:
        wallet_trades.setdefault(tr["wallet"], []).append(tr)
        
    aggregates = []
    for wallet, w_trades in wallet_trades.items():
        w_trades.sort(key=lambda x: x.get("timestamp") or 0)
        
        buys = [t for t in w_trades if t["side"] == "buy"]
        sells = [t for t in w_trades if t["side"] == "sell"]
        
        total_buy_usd = sum(t["usd_value"] for t in buys)
        total_sell_usd = sum(t["usd_value"] for t in sells)
        total_buy_tokens = sum(t["token_amount"] for t in buys)
        total_sell_tokens = sum(t["token_amount"] for t in sells)
        
        realized_profit = 0.0
        pnl_ratio = 0.0
        if total_buy_tokens > 0:
            avg_buy_price = total_buy_usd / total_buy_tokens
            matched_tokens = min(total_buy_tokens, total_sell_tokens)
            realized_cost = matched_tokens * avg_buy_price
            realized_revenue = 0.0
            if total_sell_tokens > 0:
                avg_sell_price = total_sell_usd / total_sell_tokens
                realized_revenue = matched_tokens * avg_sell_price
                
            realized_profit = realized_revenue - realized_cost
            if realized_cost > 0:
                pnl_ratio = realized_profit / realized_cost
                
        a = agg.WalletAggregate(wallet=wallet, token_address=token_address)
        a.total_buy_usd = total_buy_usd
        a.total_sell_usd = total_sell_usd
        a.realized_profit_usd = realized_profit
        a.tags = ["onchain_trader"]
        
        # Mark as sniper if they bought in first 5% of block range
        if w_trades and block_ranges:
            first_block = min(r[0] for r in block_ranges)
            last_block = max(r[1] for r in block_ranges)
            range_len = last_block - first_block
            if range_len > 0:
                earliest_trade_block = min(t.get("timestamp") or last_block for t in w_trades)
                if earliest_trade_block - first_block < range_len * 0.05:
                    a.tags.append("sniper")
                    
        aggregates.append(a)
        
    aggregates.sort(key=lambda x: x.realized_profit_usd or 0.0, reverse=True)
    return aggregates[:limit], trades, raw_trades


def main():
    parser = argparse.ArgumentParser(description="Aggregate RH-chain wallet buy/sell/profit for a token via GMGN.")
    parser.add_argument("token_address", help="Token contract address (0x...)")
    parser.add_argument("--tag", help="GMGN wallet tag filter (rat_trader / smart_degen / sniper). Default: iterate INSIDER_TAGS.")
    parser.add_argument("--all", action="store_true", help="Consider all traders, ignore tag filters.")
    parser.add_argument("--limit", type=int, default=50, help="Max traders to pull per tag (<=100).")
    parser.add_argument("--from", dest="date_from", metavar="DATE[ TIME]", help="Single-window start (UTC). e.g. '2026-07-13' or '13/7/2026 12:00'.")
    parser.add_argument("--to", dest="date_to", metavar="DATE[ TIME]", help="Single-window end (UTC, inclusive).")
    parser.add_argument("--window", nargs=2, action="append", metavar=("START", "END"),
                        help="A time window START END (UTC), repeatable for multiple windows. "
                             "e.g. --window '13/7/2026 12:00' '13/7/2026 15:00' --window '13/7/2026 17:00' '13/7/2026 18:00'")
    parser.add_argument("--activity", action="store_true", help="Force activity re-aggregation even without a date window.")
    parser.add_argument("--no-stats", action="store_true", help="Skip GMGN wallet_stats enrichment (also disables scoring).")
    parser.add_argument("--stats-period", default=config.STATS_PERIOD, choices=["7d", "30d"], help="wallet_stats window for the track record.")
    parser.add_argument("--export", metavar="PATH", help="Also write the wallet summary to an .xlsx file.")
    parser.add_argument("--txns", metavar="PATH", help="Dump raw ingested transactions (one row per buy/sell) to an .xlsx file — for testing ingestion.")
    parser.add_argument("--watchlist", metavar="PATH", default=config.WATCHLIST_PATH, help="Merge scored wallets into this JSON watchlist (Subsystem 3 input).")
    parser.add_argument("--no-watchlist", action="store_true", help="Score and print but do not write the watchlist file.")
    parser.add_argument("--onchain", action="store_true", help="Ingest all transfers on-chain via Blockscout directly instead of GMGN.")
    args = parser.parse_args()

    if not config.GMGN_API_KEY:
        print("GMGN_API_KEY is not set. Add it to .env or the environment.")
        sys.exit(1)

    windows = _build_windows(args)
    windowed = bool(windows)
    if windowed:
        desc = "; ".join(f"{_ts_label(s)} -> {_ts_label(e)}" for s, e in windows)
        logger.info("Windowed mode (%d window(s)): %s", len(windows), desc)
        logger.info("Aggregates & transactions restricted to activity in these window(s).")

    token = args.token_address
    raw_trades = []
    if args.onchain:
        aggregates, trades, raw_trades = build_onchain_aggregates(token, windows, limit=args.limit)
        tags = ["onchain_trader"]
    else:
        # Discovery: which tag(s) to pull.
        if args.all:
            tags = [None]
        elif args.tag:
            tags = [args.tag]
        else:
            tags = list(config.INSIDER_TAGS)

        merged: dict[str, agg.WalletAggregate] = {}
        for tag in tags:
            for a in build_aggregates(token, tag, args.limit):
                # A wallet can match multiple tags; keep the richer row, merge tags.
                if a.wallet in merged:
                    for t in a.tags:
                        if t not in merged[a.wallet].tags:
                            merged[a.wallet].tags.append(t)
                else:
                    merged[a.wallet] = a

        aggregates = list(merged.values())
        logger.info("%d unique wallets across tag(s) %s", len(aggregates), tags)

    # Pre-filter: enrich with stats and drop MEV/non-qualifying wallets early.
    # This prevents downloading transactions or writing reports for sandwich/MEV bots.
    if not args.no_stats:
        enrich_with_stats(aggregates, period=args.stats_period)
        initial_len = len(aggregates)
        aggregates = [a for a in aggregates if scorer.passes_filters(a)]
        logger.info(
            "Filtered out %d MEV/non-qualifying wallets. %d wallets remaining.",
            initial_len - len(aggregates), len(aggregates),
        )

    # In windowed mode (GMGN path), per-token buy/sell/profit MUST come from activity within
    # the window(s) (the trader summary is all-time). Drop wallets inactive then.
    if not args.onchain and (windowed or args.activity):
        aggregates = enrich_with_activity(
            aggregates, token, windows=windows, drop_empty=windowed
        )
        if windowed:
            logger.info("%d wallets active in the window(s)", len(aggregates))

    # Raw ingestion dump: one row per transaction (independent of scoring).
    if args.txns:
        from export_xlxs import export_transactions
        raw_events = None
        if args.onchain:
            # Fetch actual token symbol from DEX Screener
            token_symbol = "TOKEN"
            try:
                from ingestion import dexscreener_client as dex
                pair_address = dex.get_primary_pair(token)
                if pair_address:
                    token_symbol = pair_address.get("baseToken", {}).get("symbol") or "TOKEN"
            except Exception:
                pass

            events = []
            raw_events = []
            valid_wallets = {a.wallet for a in aggregates}
            for t in trades:
                if t["wallet"] in valid_wallets:
                    events.append({
                        "wallet": t["wallet"],
                        "timestamp": t["timestamp"],
                        "event_type": t["side"],
                        "token": {"address": token, "symbol": token_symbol},
                        "token_amount": t["token_amount"],
                        "cost_usd": t["usd_value"],
                        "gas_usd": 0.0,
                        "tx_hash": t["transaction_hash"]
                    })
                    
            for t in raw_trades:
                status = t["status"]
                if status == "PENDING":
                    if t["wallet"] in valid_wallets:
                        status = "CLEAN_TRADE"
                    else:
                        status = "MEV_OR_NON_QUALIFYING"
                raw_events.append({
                    "wallet": t["wallet"],
                    "timestamp": t["timestamp"],
                    "event_type": t["side"],
                    "token": {"address": token, "symbol": token_symbol},
                    "token_amount": t["token_amount"],
                    "cost_usd": t["usd_value"],
                    "tx_hash": t["transaction_hash"],
                    "status": status
                })
        else:
            events = collect_transactions(aggregates, token, windows=windows)
            
        logger.info("Ingested %d raw transactions across %d wallets", len(events), len(aggregates))
        try:
            export_transactions(events, args.txns, raw_events=raw_events)
            logger.info("Wrote transactions to %s", args.txns)
        except ValueError as e:
            logger.warning("Transactions export skipped: %s", e)

    print_table(aggregates)

    if args.export:
        from export_xlxs import export_wallet_report
        try:
            export_wallet_report(aggregates, args.export, token_label=token)
            logger.info("Wrote wallet summary to %s", args.export)
        except ValueError as e:
            logger.warning("Export skipped: %s", e)

    # --- Subsystem 2: score wallets and persist the watchlist ---
    if args.no_stats:
        logger.info("Scoring skipped (--no-stats): the scorer needs wallet_stats.")
        return

    scores = scorer.rank_wallets(aggregates)
    print_scores(scores)

    # Filter out wallets that do not meet the minimum quality score threshold for the watchlist
    watchlist_scores = [s for s in scores if s.score >= config.MIN_WATCHLIST_SCORE]

    if watchlist_scores and not args.no_watchlist:
        watchlist_map = wl.upsert(
            args.watchlist, watchlist_scores, seed_token=token, stats_period=args.stats_period
        )
        logger.info(
            "Watchlist updated: +%d scored (>= %s) from this token, %d wallets total -> %s",
            len(watchlist_scores), config.MIN_WATCHLIST_SCORE, len(watchlist_map), args.watchlist,
        )
        
        # Export watchlist to Excel as well
        try:
            from export_xlxs import export_watchlist
            watchlist_xlsx = args.watchlist.replace(".json", ".xlsx")
            export_watchlist(watchlist_map, watchlist_xlsx)
            logger.info("Watchlist Excel exported -> %s", watchlist_xlsx)
        except Exception as e:
            logger.warning("Watchlist Excel export failed: %s", e)

    # --- Auto-open all generated Excel reports ---
    import platform
    import subprocess
    import os
    
    def auto_open(path: str):
        if not path or not os.path.exists(path):
            return
        try:
            abs_path = os.path.abspath(path)
            if platform.system() == "Darwin":
                subprocess.run(["open", abs_path], check=True)
            elif platform.system() == "Windows":
                os.startfile(abs_path)
            else:
                subprocess.run(["xdg-open", abs_path], check=True)
            logger.info("Auto-opened report: %s", abs_path)
        except Exception as e:
            logger.warning("Could not auto-open %s: %s", abs_path, e)

    if args.txns:
        auto_open(args.txns)
    if args.export:
        auto_open(args.export)
    if not args.no_watchlist:
        watchlist_xlsx = args.watchlist.replace(".json", ".xlsx")
        auto_open(watchlist_xlsx)


if __name__ == "__main__":
    main()
