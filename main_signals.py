"""
Command-line interface for the Signal Generator (Subsystem 3).

Usage:
    python3 main_signals.py --min-score 30 --export signals.xlsx
"""

import sys
import argparse
import logging
from datetime import datetime, timezone

import config
from signals import signal_generator as sig_gen
from export_xlxs import export_signals_report

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _ts_label(ts: int | None) -> str:
    if ts is None:
        return "N/A"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def print_co_investments(co_investments: list[dict]):
    print("\n=== CO-INVESTMENTS DETECTED ===")
    print(f"{'Token':<12} {'Address':<44} {'Buyers':>6} {'Price':>10} {'Liquidity':>12}")
    for c in co_investments:
        price = f"${c['price_usd']:.4f}" if c['price_usd'] is not None else "N/A"
        liq = f"${c['liquidity_usd']:,.0f}" if c['liquidity_usd'] is not None else "N/A"
        print(f"{c['symbol']:<12} {c['token_address']:<44} {c['buyers_count']:>6} {price:>10} {liq:>12}")
        for b in c["buyers"]:
            time_str = _ts_label(b["timestamp"])
            print(f"  - Buyer: {b['wallet']} | Score: {b['score']:.1f} | Amount: {b['amount']:,.0f} | Time: {time_str}")
    if not co_investments:
        print("No co-investments detected.")


def print_signals_table(signals: list[dict], limit: int = 50):
    print(f"\n=== RECENT TRANSACTION SIGNALS (showing top {limit}) ===")
    print(
        f"{'Time (UTC)':<16} {'Wallet':<44} {'Score':>5} {'Side':<4} "
        f"{'Symbol':<12} {'Amount':>12} {'Est. Value':>12} {'Tx Hash':<10}..."
    )
    for s in signals[:limit]:
        time_str = _ts_label(s["timestamp"])
        val_str = f"${s['estimated_value_usd']:,.0f}" if s['estimated_value_usd'] is not None else "N/A"
        amt_str = f"{s['amount']:,.0f}"
        tx_short = s["tx_hash"][:10] if s["tx_hash"] else "N/A"
        print(
            f"{time_str:<16} {s['wallet']:<44} {s['score']:>5.1f} {s['side']:<4} "
            f"{s['symbol']:<12} {amt_str:>12} {val_str:>12} {tx_short:<10}..."
        )
    if not signals:
        print("No new transaction signals detected.")


def main():
    parser = argparse.ArgumentParser(description="Scan blockscout for recent scored wallet activity and flag tokens.")
    parser.add_argument("--min-score", type=float, default=0.0, help="Only scan wallets with score >= this threshold.")
    parser.add_argument("--watchlist", default=config.WATCHLIST_PATH, help="Path to watchlist JSON.")
    parser.add_argument("--state", default=config.SIGNAL_STATE_PATH, help="Path to transaction tracking state JSON.")
    parser.add_argument("--limit-pages", type=int, default=2, help="Max pages of transfers to check per wallet.")
    parser.add_argument("--force", action="store_true", help="Ignore saved transaction state and scan from scratch.")
    parser.add_argument("--no-save", action="store_true", help="Do not save the updated state file.")
    parser.add_argument("--export", metavar="PATH", help="Export signals to an Excel workbook.")
    parser.add_argument("--co-only", action="store_true", help="Only show co-investments.")
    args = parser.parse_args()

    if not config.BLOCKSCOUT_API_KEY:
        print("BLOCKSCOUT_API_KEY is not set. Add it to .env or the environment.")
        sys.exit(1)

    logger.info("Starting signal generator scan (force_scan=%s)...", args.force)

    # State path configuration
    state_file = args.state
    if args.no_save:
        state_file = "/dev/null"  # prevents saving state file

    result = sig_gen.generate_signals(
        watchlist_path=args.watchlist,
        state_path=state_file,
        min_score=args.min_score,
        force_scan=args.force,
        max_pages=args.limit_pages,
    )

    signals = result["signals"]
    co_investments = result["co_investments"]

    logger.info("Found %d new signals and %d co-investments.", len(signals), len(co_investments))

    # Display results
    print_co_investments(co_investments)
    
    if not args.co_only:
        print_signals_table(signals)

    # Export to Excel if requested
    if args.export:
        try:
            export_signals_report(signals, co_investments, args.export)
            logger.info("Successfully exported signals to %s", args.export)
        except Exception as e:
            logger.error("Failed to export signals to Excel: %s", e)


if __name__ == "__main__":
    main()
