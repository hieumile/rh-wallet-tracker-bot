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
    # Divide into funding and trading
    funding_signals = [s for s in signals if s.get("side") == "FUNDING"]
    trading_signals = [s for s in signals if s.get("side") in ("BUY", "SELL")]

    # 1. Print Funding Alerts
    if funding_signals:
        print("\n=== INSIDER FUNDING ALERTS ===")
        for s in funding_signals[:20]: # Cap at 20 alerts
            time_str = _ts_label(s["timestamp"])
            parent = s["wallet"]
            child = s["token_address"]
            val = s["amount"]
            tx_short = s["tx_hash"][:10] if s["tx_hash"] else "N/A"
            print(f"[{time_str}] Parent {parent[:12]}... (Score: {s['score']:.1f}) funded new wallet {child[:12]}... with {val:.2f} ETH (Tx: {tx_short})")
        print()

    # 2. Group Trading Activity by Token
    by_token = {}
    for s in trading_signals:
        t_addr = s["token_address"]
        by_token.setdefault(t_addr, []).append(s)

    if trading_signals:
        print("=== ACTIVE TOKEN TRADING ACTIVITY ===")
        # Sort tokens by the time of their latest transaction (newest first)
        sorted_tokens = sorted(
            by_token.items(),
            key=lambda item: max((t.get("timestamp") or 0 for t in item[1])),
            reverse=True
        )

        for t_addr, sigs in sorted_tokens[:limit]:
            # Get token metadata from first transaction
            symbol = sigs[0]["symbol"]
            name = sigs[0]["name"]
            price_usd = sigs[0].get("price_usd")
            price_str = f"${price_usd:.6f}" if price_usd is not None else "N/A"
            
            print(f"\nToken: {symbol} ({name}) | Address: {t_addr}")
            print(f"Price: {price_str}")

            # Group by wallet for this token
            by_wallet = {}
            for s in sigs:
                by_wallet.setdefault(s["wallet"], []).append(s)

            for wallet, w_sigs in by_wallet.items():
                score = w_sigs[0]["score"]
                buys = [s for s in w_sigs if s["side"] == "BUY"]
                sells = [s for s in w_sigs if s["side"] == "SELL"]

                buy_val = sum(s["estimated_value_usd"] or 0.0 for s in buys)
                sell_val = sum(s["estimated_value_usd"] or 0.0 for s in sells)
                
                # Determine status
                if buys and not sells:
                    status = f"HOLDING (Bought ${buy_val:,.0f})"
                elif sells and not buys:
                    status = f"SELLING OLD POSITION (Sold ${sell_val:,.0f})"
                elif buy_val > 0:
                    if sell_val >= buy_val * 0.95:
                        status = f"FULLY EXITED (Bought ${buy_val:,.0f}, Sold ${sell_val:,.0f})"
                    else:
                        status = f"PARTIALLY SOLD (Bought ${buy_val:,.0f}, Sold ${sell_val:,.0f})"
                else:
                    status = f"TRADING (Buys: ${buy_val:,.0f}, Sells: ${sell_val:,.0f})"

                # Get latest action
                w_sigs_sorted = sorted(w_sigs, key=lambda x: x.get("timestamp") or 0, reverse=True)
                latest_action = w_sigs_sorted[0]["side"]
                latest_time = _ts_label(w_sigs_sorted[0]["timestamp"])

                print(f"  - Wallet: {wallet[:12]}... (Score: {score:.1f}) | Status: {status} | Last Action: {latest_action} ({latest_time})")
                for s in reversed(w_sigs_sorted): # print chronologically
                    t_time = datetime.fromtimestamp(s["timestamp"], tz=timezone.utc).strftime("%H:%M") if s["timestamp"] else "N/A"
                    val_str = f"${s['estimated_value_usd']:,.0f}" if s['estimated_value_usd'] is not None else "N/A"
                    print(f"    * [{t_time}] {s['side']} {s['amount']:,.0f} {symbol} ({val_str})")
        print()
    
    if not signals:
        print("No new signals detected.")


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
