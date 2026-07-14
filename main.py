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
        f"{'wallet':<44} {'score':>6} {'win%':>6} {'profit$':>13} "
        f"{'pnl':>5} {'toks':>5}  {'signals':<32}"
    )
    for s in scores:
        win = f"{s.winrate*100:5.1f}" if s.winrate is not None else "   - "
        profit = s.realized_profit_usd if s.realized_profit_usd is not None else 0.0
        pnl = f"{s.pnl_ratio:4.2f}" if s.pnl_ratio is not None else "  - "
        sig = ", ".join(s.insider_signals)[:32]
        print(
            f"{s.wallet:<44} {s.score:>6.1f} {win:>6} {profit:>13,.0f} "
            f"{pnl:>5} {s.token_num or 0:>5}  {sig:<32}"
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

    # Discovery: which tag(s) to pull.
    if args.all:
        tags: list[str | None] = [None]
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

    # In windowed mode, per-token buy/sell/profit MUST come from activity within
    # the window(s) (the trader summary is all-time). Drop wallets inactive then.
    if windowed or args.activity:
        aggregates = enrich_with_activity(
            aggregates, token, windows=windows, drop_empty=windowed
        )
        if windowed:
            logger.info("%d wallets active in the window(s)", len(aggregates))

    # Raw ingestion dump: one row per transaction (independent of scoring).
    if args.txns:
        from export_xlxs import export_transactions
        events = collect_transactions(aggregates, token, windows=windows)
        logger.info("Ingested %d raw transactions across %d wallets", len(events), len(aggregates))
        try:
            export_transactions(events, args.txns)
            logger.info("Wrote transactions to %s", args.txns)
        except ValueError as e:
            logger.warning("Transactions export skipped: %s", e)

    if not args.no_stats:
        enrich_with_stats(aggregates, period=args.stats_period)

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

    if scores and not args.no_watchlist:
        watchlist_map = wl.upsert(
            args.watchlist, scores, seed_token=token, stats_period=args.stats_period
        )
        logger.info(
            "Watchlist updated: +%d scored from this token, %d wallets total -> %s",
            len(scores), len(watchlist_map), args.watchlist,
        )


if __name__ == "__main__":
    main()
