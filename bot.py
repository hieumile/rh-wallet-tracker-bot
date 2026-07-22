#!/usr/bin/env python3
"""
Unified CLI Entrypoint for the Robinhood Chain Wallet Tracker Bot.

Simplifies execution by supporting:
- Token symbols (e.g. arrow, cashcat) instead of raw addresses
- Shorthand time windows (e.g. 24h, 7d, '07/07 19:00')
- Smart default file exports
- Executable shell shortcut commands (scrap and watch)
"""

import sys
import os
import argparse
from datetime import datetime, timedelta, timezone

# Add current folder to path to make imports work from anywhere
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from ingestion import dexscreener_client as dex

COMMON_ALIASES = {
    "arrow": "0xf2915d1e3c1b0c769d0c756ec43f1c1f6c99cd03",
    "cashcat": "0x020bfc650a365f8bb26819deaabf3e21291018b4",
    "pons": "0x39dbed3a2bd333467115de45665cc57f813c4571",
}


def _resolve_token(token_input: str) -> str:
    """Resolve token symbol or alias to contract address."""
    token_lower = token_input.lower()
    
    # 1. Check hardcoded alias dictionary
    if token_lower in COMMON_ALIASES:
        resolved = COMMON_ALIASES[token_lower]
        print(f"Resolved token alias '{token_input}' to address: {resolved}")
        return resolved
        
    # 2. Check if it looks like a valid contract address
    if token_input.startswith("0x") and len(token_input) == 42:
        return token_input
        
    # 3. Dynamic lookup via DEX Screener search
    print(f"Searching DEX Screener for token symbol '{token_input}'...")
    resolved = dex.search_token_address_by_symbol(token_input)
    if resolved:
        print(f"Found address for '{token_input}': {resolved}")
        return resolved
        
    # Fallback: return raw string but validate it is a valid EVM address format
    if not (token_input.startswith("0x") and len(token_input) == 42):
        print(
            f"\nERROR: '{token_input}' is not a valid EVM address format.\n"
            f"Robinhood Chain requires a 42-character hex address starting with '0x' (e.g., 0x39dbed3a...).\n"
            f"It looks like you passed a Solana address (pump.fun) or an invalid contract."
        )
        sys.exit(1)
        
    return token_input


def _parse_shorthand_date(date_str: str, default_time: str) -> str:
    """Helper to parse raw date string into standard dd/mm/yyyy HH:MM."""
    parts = date_str.strip().split()
    date_part = parts[0]
    time_part = parts[1] if len(parts) > 1 else default_time
    
    # If date is dd/mm, append current year
    if date_part.count("/") == 1:
        current_year = datetime.now(timezone.utc).year
        date_part = f"{date_part}/{current_year}"
        
    return f"{date_part} {time_part}"


def _format_windows(window_args: list) -> list[str]:
    """Convert shorthand window arguments into list of raw main.py parameters."""
    formatted_args = []
    
    # window_args is a list of lists: e.g. [['24h'], ['07/07', '08/07 13:00']]
    for w in window_args:
        # Relative time window (e.g. 24h, 7d)
        if len(w) == 1 and (w[0].endswith("h") or w[0].endswith("d")):
            val = w[0]
            now = datetime.now(timezone.utc)
            try:
                if val.endswith("h"):
                    start = now - timedelta(hours=int(val[:-1]))
                else:
                    start = now - timedelta(days=int(val[:-1]))
                
                # Format to dd/mm/yyyy HH:MM
                start_str = start.strftime("%d/%m/%Y %H:%M")
                end_str = now.strftime("%d/%m/%Y %H:%M")
                formatted_args.extend(["--window", start_str, end_str])
            except ValueError:
                # If invalid, pass through
                formatted_args.extend(["--window", val])
        # Specific date window (START, END)
        elif len(w) == 2:
            start_str = _parse_shorthand_date(w[0], "00:00")
            end_str = _parse_shorthand_date(w[1], "23:59")
            formatted_args.extend(["--window", start_str, end_str])
        else:
            # Fallback
            for item in w:
                formatted_args.extend(["--window", item])
                
    return formatted_args


def _get_token_identifier(token_input: str, resolved_address: str) -> str:
    """Return a clean, safe filename identifier for the token (e.g. 'pons' or '0x39dbed3a')."""
    token_lower = token_input.lower().strip()
    
    # 1. If input was a known alias, return it
    if token_lower in COMMON_ALIASES:
        return token_lower
        
    # 2. Check reverse mapping in COMMON_ALIASES
    for alias, addr in COMMON_ALIASES.items():
        if addr.lower() == resolved_address.lower():
            return alias
            
    # 3. Try to get symbol from DEX Screener
    try:
        pair = dex.get_primary_pair(resolved_address)
        if pair and pair.get("baseToken"):
            symbol = pair["baseToken"].get("symbol", "").lower().strip()
            if symbol and symbol.isalnum():
                return symbol
    except Exception:
        pass
        
    # Fallback to short address
    return resolved_address[:10].lower()


def run_scan(args, extra_args):
    """Normalize scanner arguments and execute main.py."""
    token = _resolve_token(args.token)
    token_id = _get_token_identifier(args.token, token)
    
    # Construct base sys.argv for main.py
    sys_args = ["main.py", token]
    
    # Handle windows
    if args.window:
        sys_args.extend(_format_windows(args.window))
        
    if args.onchain:
        sys_args.append("--onchain")
    if args.gmgn:
        sys_args.append("--gmgn")
    if args.all:
        sys_args.append("--all")
    if args.tag:
        sys_args.extend(["--tag", args.tag])
    if args.limit:
        sys_args.extend(["--limit", str(args.limit)])
        
    # Append smart defaults for token-specific export paths
    txns_path = args.txns if args.txns != "transactions.xlsx" else f"transactions_{token_id}.xlsx"
    export_path = args.export if args.export != "wallet_summary.xlsx" else f"wallet_summary_{token_id}.xlsx"
    
    sys_args.extend(["--txns", txns_path])
    sys_args.extend(["--export", export_path])
    
    # Append any unparsed CLI arguments directly
    sys_args.extend(extra_args)
    
    # Import and execute main
    print(f"Running scan with args: {' '.join(sys_args[1:])}")
    sys.argv = sys_args
    
    import main
    main.main()


def run_watch(args, extra_args):
    """Normalize signals arguments and execute main_signals.py."""
    sys_args = ["main_signals.py"]
    
    if args.min_score:
        sys_args.extend(["--min-score", str(args.min_score)])
    if args.co_only:
        sys_args.append("--co-only")
    if args.limit_pages:
        sys_args.extend(["--limit-pages", str(args.limit_pages)])
    if args.force:
        sys_args.append("--force")
        
    # Append smart default export
    sys_args.extend(["--export", args.export])
    
    # Append any unparsed CLI arguments directly
    sys_args.extend(extra_args)
    
    # Import and execute main_signals
    print(f"Running watch with args: {' '.join(sys_args[1:])}")
    sys.argv = sys_args
    
    import main_signals
    main_signals.main()


def run_remove(args):
    """Remove one or more wallets from the watchlist and sync with Excel."""
    import json
    from export_xlxs import export_watchlist
    
    watchlist_path = args.watchlist
    if not os.path.exists(watchlist_path):
        print(f"Error: Watchlist file not found at '{watchlist_path}'")
        sys.exit(1)
        
    with open(watchlist_path, "r") as f:
        try:
            data = json.load(f)
        except Exception as e:
            print(f"Error reading watchlist: {e}")
            sys.exit(1)
            
    initial_count = len(data)
    remove_targets = {w.lower().strip() for w in args.wallets}
    
    # Filter out matching wallets
    cleaned = [e for e in data if e.get("wallet", "").lower().strip() not in remove_targets]
    removed_count = initial_count - len(cleaned)
    
    if removed_count == 0:
        print("No matching wallets found in the watchlist. No changes made.")
        return
        
    # Save watchlist.json
    with open(watchlist_path, "w") as f:
        json.dump(cleaned, f, indent=2)
        
    # Sync with watchlist.xlsx
    excel_path = watchlist_path.replace(".json", ".xlsx")
    try:
        watchlist_map = {e["wallet"]: e for e in cleaned}
        export_watchlist(watchlist_map, excel_path)
        excel_msg = f" and synchronized {excel_path}"
        
        # Auto-open/reload the updated Excel file
        import subprocess
        abs_path = os.path.abspath(excel_path)
        if sys.platform == "darwin":
            subprocess.run(["open", abs_path], check=True)
        elif sys.platform.startswith("win"):
            os.startfile(abs_path)
        else:
            subprocess.run(["xdg-open", abs_path], check=True)
    except Exception as e:
        excel_msg = f" (failed to sync/open Excel: {e})"
        
    print(f"Successfully removed {removed_count} wallet(s) from watchlist{excel_msg}.")
    print(f"Remaining wallets: {len(cleaned)}.")


def main():
    parser = argparse.ArgumentParser(
        description="Unified CLI interface for Robinhood Chain Wallet Tracker Bot."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # Subcommand: scan
    scan_parser = subparsers.add_parser("scan", help="Scan a token's trading activity and score wallets.")
    scan_parser.add_argument("token", help="Token symbol (e.g. arrow) or contract address (0x...)")
    scan_parser.add_argument(
        "--window", nargs="+", action="append", metavar="WINDOW",
        help="Shorthand time window. Relative (e.g. 24h, 7d) or exact (e.g. '07/07 19:00' '08/07 13:00')"
    )
    scan_parser.add_argument("--onchain", action="store_true", help="Legacy flag (on-chain is default).")
    scan_parser.add_argument("--gmgn", action="store_true", help="Use GMGN top traders database instead of direct on-chain scan.")
    scan_parser.add_argument("--all", action="store_true", help="Scan all traders, bypass tag filters.")
    scan_parser.add_argument("--tag", help="GMGN tag filter.")
    scan_parser.add_argument("--limit", type=int, default=50, help="Max candidates to pull per tag.")
    scan_parser.add_argument("--txns", default="transactions.xlsx", help="Export path for raw transactions database.")
    scan_parser.add_argument("--export", default="wallet_summary.xlsx", help="Export path for wallet rankings.")
    
    # Subcommand: watch
    watch_parser = subparsers.add_parser("watch", help="Watch live transaction signals from watchlist.")
    watch_parser.add_argument("--min-score", type=float, default=30.0, help="Minimum wallet score to watch.")
    watch_parser.add_argument("--export", default="signals.xlsx", help="Export path for signal logs.")
    watch_parser.add_argument("--co-only", action="store_true", help="Only show co-investment alerts.")
    watch_parser.add_argument("--limit-pages", type=int, default=2, help="Max history pages to scan per wallet.")
    watch_parser.add_argument("--force", action="store_true", help="Ignore saved cursor state and scan fresh.")
    
    # Subcommand: remove
    remove_parser = subparsers.add_parser("remove", help="Remove one or more wallets from the watchlist.")
    remove_parser.add_argument("wallets", nargs="+", help="Wallet addresses (0x...) to remove.")
    remove_parser.add_argument("--watchlist", default="watchlist.json", help="Path to watchlist file.")
    
    # Parse defined arguments, leaving extra unparsed args to be forwarded
    args, extra_args = parser.parse_known_args()
    
    if args.command == "scan":
        run_scan(args, extra_args)
    elif args.command == "watch":
        run_watch(args, extra_args)
    elif args.command == "remove":
        run_remove(args)


if __name__ == "__main__":
    main()
