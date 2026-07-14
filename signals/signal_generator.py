"""
Signal Generator Engine (Subsystem 3).

Monitors scored wallets from the watchlist, pulls their recent on-chain transfers
from Blockscout, classifies swaps (buys/sells), and groups them to find co-investments.
"""

import logging
import time
from datetime import datetime, timezone
import os
import json

import config
from ingestion import blockscout_client as bs
from ingestion import dexscreener_client as dex
from scoring import watchlist as wl

logger = logging.getLogger(__name__)


def load_state(path: str) -> dict:
    """Load transaction tracking state: {wallet_address: last_seen_tx_hash}"""
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Could not load signal state from %s: %s", path, e)
    return {}


def save_state(path: str, state: dict):
    """Save transaction tracking state"""
    try:
        tmp = f"{path}.tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("Could not save signal state to %s: %s", path, e)


def classify_wallet_transfer(transfer: dict, wallet: str) -> dict | None:
    """
    Classify a single token transfer for a wallet into BUY, SELL, or TRANSFER.
    Returns a dict with classification details, or None if it should be skipped.
    """
    token_info = transfer.get("token") or {}
    token_addr = (token_info.get("address_hash") or "").lower()
    
    # Skip quote tokens (e.g. WETH) as targets
    if token_addr in config.QUOTE_TOKENS:
        return None

    symbol = token_info.get("symbol") or "TOKEN"
    name = token_info.get("name") or "Token"
    decimals = int(token_info.get("decimals") or 18)
    
    total_info = transfer.get("total") or {}
    try:
        raw_val = total_info.get("value") or "0"
        amount = float(raw_val) / (10 ** decimals)
    except (ValueError, TypeError):
        amount = 0.0

    tx_hash = transfer.get("transaction_hash")
    ts_str = transfer.get("timestamp")
    
    # Convert ISO timestamp to unix timestamp
    ts = None
    if ts_str:
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            ts = int(dt.timestamp())
        except ValueError:
            pass

    from_hash = (transfer.get("from") or {}).get("hash", "").lower()
    to_hash = (transfer.get("to") or {}).get("hash", "").lower()
    wallet_lower = wallet.lower()

    # Determine trade side
    if to_hash == wallet_lower:
        # Received token
        is_from_contract = (transfer.get("from") or {}).get("is_contract", False)
        side = "BUY" if is_from_contract else "TRANSFER_IN"
    elif from_hash == wallet_lower:
        # Sent token
        is_to_contract = (transfer.get("to") or {}).get("is_contract", False)
        side = "SELL" if is_to_contract else "TRANSFER_OUT"
    else:
        # Wallet not directly sender or receiver of this leg
        return None

    return {
        "tx_hash": tx_hash,
        "timestamp": ts,
        "side": side,
        "token_address": token_addr,
        "symbol": symbol,
        "name": name,
        "amount": amount,
    }


def scan_wallet_activity(
    wallet: str,
    last_tx: str | None,
    max_pages: int = 2,
    is_funder: bool = False,
) -> tuple[list[dict], str | None]:
    """
    Fetch and classify recent activity for a wallet from Blockscout.
    If is_funder is True, scans native transaction history for outgoing coin transfers.
    Otherwise, scans ERC-20 transfers for standard token buys/sells.
    Returns: (list of classified trades/transfers, newest_tx_hash)
    """
    if is_funder:
        try:
            txs = bs.get_address_transactions(wallet, max_pages=max_pages)
        except Exception as e:
            logger.error("Failed to fetch Blockscout transactions for funder %s: %s", wallet, e)
            return [], last_tx

        if not txs:
            return [], last_tx

        newest_tx = txs[0].get("hash")
        classified = []

        for tx in txs:
            tx_hash = tx.get("hash")
            if last_tx and tx_hash == last_tx:
                break

            from_hash = (tx.get("from") or {}).get("hash", "").lower()
            if from_hash == wallet.lower():
                to_info = tx.get("to") or {}
                to_hash = to_info.get("hash", "").lower()
                is_contract = to_info.get("is_contract", False)
                val_str = tx.get("value") or "0"
                try:
                    val = float(val_str) / 1e18
                except (ValueError, TypeError):
                    val = 0.0

                # Outgoing coin transfer to non-contract EOA representing funding
                if to_hash and not is_contract and val > 0:
                    ts_str = tx.get("timestamp")
                    ts = None
                    if ts_str:
                        try:
                            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                            ts = int(dt.timestamp())
                        except ValueError:
                            pass

                    classified.append({
                        "tx_hash": tx_hash,
                        "timestamp": ts,
                        "side": "FUNDING",
                        "token_address": to_hash,  # funded wallet address
                        "symbol": "ETH",
                        "name": "Ethereum",
                        "amount": val,
                    })
        return classified, newest_tx
    else:
        try:
            transfers = bs.get_address_token_transfers(wallet, max_pages=max_pages)
        except Exception as e:
            logger.error("Failed to fetch Blockscout transfers for %s: %s", wallet, e)
            return [], last_tx

        if not transfers:
            return [], last_tx

        newest_tx = transfers[0].get("transaction_hash")
        classified = []

        for t in transfers:
            tx_hash = t.get("transaction_hash")
            if last_tx and tx_hash == last_tx:
                break
            
            c = classify_wallet_transfer(t, wallet)
            if c:
                classified.append(c)

        return classified, newest_tx


def enrich_token_metadata(token_address: str) -> dict:
    """
    Fetch market metadata for a token from DEX Screener.
    """
    meta = {
        "symbol": "TOKEN",
        "name": "Token",
        "price_usd": None,
        "liquidity_usd": None,
        "volume_24h": None,
        "pair_url": None,
    }
    try:
        pair = dex.get_primary_pair(token_address)
        if pair:
            meta["symbol"] = pair.get("baseToken", {}).get("symbol") or meta["symbol"]
            meta["name"] = pair.get("baseToken", {}).get("name") or meta["name"]
            meta["price_usd"] = float(pair.get("priceUsd") or 0.0) if pair.get("priceUsd") else None
            meta["liquidity_usd"] = float(pair.get("liquidity", {}).get("usd") or 0.0) if pair.get("liquidity") else None
            meta["volume_24h"] = float(pair.get("volume", {}).get("h24") or 0.0) if pair.get("volume") else None
            meta["pair_url"] = pair.get("url")
    except Exception as e:
        logger.warning("Failed to enrich token metadata for %s: %s", token_address, e)
    return meta


def generate_signals(
    watchlist_path: str,
    state_path: str,
    min_score: float = 0.0,
    force_scan: bool = False,
    max_pages: int = 2,
) -> dict:
    """
    Main signal generation pipeline.
    Scans wallets, groups signals, enrich token details, and updates state.
    """
    # 1. Load watchlist
    watchlist = wl.load(watchlist_path)
    if not watchlist:
        logger.warning("Watchlist is empty or not found.")
        return {"signals": [], "co_investments": []}

    # Filter wallets by score threshold
    active_wallets = {
        addr: entry for addr, entry in watchlist.items()
        if entry.get("score", 0.0) >= min_score
    }
    logger.info("Scanning %d wallets with score >= %.1f", len(active_wallets), min_score)

    # 2. Load state
    state = {} if force_scan else load_state(state_path)
    new_state = {**state}

    raw_signals = []
    
    # 3. Scan each wallet
    for addr, entry in active_wallets.items():
        last_tx = state.get(addr)
        tags = entry.get("tags") or []
        is_funder = "insider_funder" in {t.lower() for t in tags}
        logger.info("Scanning wallet %s (last tx: %s, is_funder=%s)", addr, last_tx, is_funder)
        
        trades, newest_tx = scan_wallet_activity(addr, last_tx, max_pages=max_pages, is_funder=is_funder)
        if newest_tx:
            new_state[addr] = newest_tx
        
        for t in trades:
            raw_signals.append({
                "wallet": addr,
                "score": entry.get("score", 0.0),
                "winrate": entry.get("winrate"),
                "tags": entry.get("tags", []),
                **t
            })
        
        # Respect rate limits between wallet calls
        time.sleep(config.BLOCKSCOUT_REQUEST_DELAY)

    # 4. Group by token address to identify co-investments
    by_token = {}
    for sig in raw_signals:
        t_addr = sig["token_address"]
        by_token.setdefault(t_addr, []).append(sig)

    # Build structured signals list
    processed_signals = []
    co_investments = []

    # Map to cache token metadata so we don't query DEX Screener repeatedly for the same token
    meta_cache = {}

    for t_addr, sigs in by_token.items():
        is_funding = sigs[0].get("side") == "FUNDING"
        if is_funding:
            meta = {
                "symbol": "ETH",
                "name": "Ethereum",
                "price_usd": None,
                "liquidity_usd": None,
                "volume_24h": None,
                "pair_url": None,
            }
        else:
            if t_addr not in meta_cache:
                meta_cache[t_addr] = enrich_token_metadata(t_addr)
            meta = meta_cache[t_addr]

        # Determine co-investment buys
        buys = [s for s in sigs if s["side"] == "BUY"]
        if len(buys) >= 2:
            co_investments.append({
                "token_address": t_addr,
                "symbol": meta["symbol"] if meta["symbol"] != "TOKEN" else sigs[0]["symbol"],
                "name": meta["name"] if meta["name"] != "Token" else sigs[0]["name"],
                "price_usd": meta["price_usd"],
                "liquidity_usd": meta["liquidity_usd"],
                "pair_url": meta["pair_url"],
                "buyers_count": len(buys),
                "buyers": [
                    {
                        "wallet": b["wallet"],
                        "score": b["score"],
                        "amount": b["amount"],
                        "timestamp": b["timestamp"],
                        "tx_hash": b["tx_hash"]
                    }
                    for b in sorted(buys, key=lambda x: x["score"], reverse=True)
                ]
            })

        for s in sigs:
            processed_signals.append({
                "timestamp": s["timestamp"],
                "wallet": s["wallet"],
                "score": s["score"],
                "winrate": s["winrate"],
                "tags": s["tags"],
                "side": s["side"],
                "token_address": t_addr,
                "symbol": meta["symbol"] if meta["symbol"] != "TOKEN" else s["symbol"],
                "name": meta["name"] if meta["name"] != "Token" else s["name"],
                "amount": s["amount"],
                "estimated_value_usd": (s["amount"] * meta["price_usd"]) if (s["amount"] and meta["price_usd"]) else None,
                "price_usd": meta["price_usd"],
                "liquidity_usd": meta["liquidity_usd"],
                "tx_hash": s["tx_hash"],
            })

    # Sort signals by timestamp (newest first)
    processed_signals.sort(key=lambda s: s["timestamp"] or 0, reverse=True)
    # Sort co-investments by buyers count
    co_investments.sort(key=lambda c: c["buyers_count"], reverse=True)

    # 5. Save state
    if not force_scan:
        save_state(state_path, new_state)

    return {
        "signals": processed_signals,
        "co_investments": co_investments
    }
