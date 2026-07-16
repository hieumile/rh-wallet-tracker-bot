"""
Thin client around the Blockscout Pro API (v2) scoped to Robinhood Chain.

Covers exactly what the pipeline needs right now:
  - resolve a unix timestamp to the nearest block number (binary search)
  - pull paginated token transfers for a given token contract
  - fetch full transfer/log detail for a single transaction (for swap
    classification / price reconstruction downstream)

Nothing here executes trades or writes anywhere — read-only HTTP GETs.
"""

import time
import logging
import requests

import config

logger = logging.getLogger(__name__)

_session = requests.Session()
_session.headers.update({"Accept": "application/json"})
if config.BLOCKSCOUT_API_KEY:
    _session.headers.update({"Authorization": f"Bearer {config.BLOCKSCOUT_API_KEY}"})


class BlockscoutError(Exception):
    """Raised when the Blockscout API returns an unrecoverable error."""


def _get(path: str, params: dict | None = None) -> dict:
    """
    GET against the Blockscout Pro API with rate-limit and transient error backoff.
    Retries on 429 and server-side errors (500, 502, 503, 504).
    """
    url = f"{config.BLOCKSCOUT_BASE_URL}{path}"
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            resp = _session.get(url, params=params, timeout=config.REQUEST_TIMEOUT_SECONDS)
        except Exception as e:
            if attempt < max_attempts - 1:
                logger.warning("Request failed for %s (attempt %d/%d): %s. Retrying in 2.0s...", path, attempt + 1, max_attempts, e)
                time.sleep(2.0)
                continue
            raise

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", 2.0))
            logger.warning("Rate limited on %s, sleeping %.1fs", path, retry_after)
            time.sleep(retry_after)
            continue

        if resp.status_code in (500, 502, 503, 504) and attempt < max_attempts - 1:
            logger.warning("Transient server error %d on %s (attempt %d/%d). Retrying in 2.0s...", resp.status_code, path, attempt + 1, max_attempts)
            time.sleep(2.0)
            continue

        if resp.status_code >= 400:
            raise BlockscoutError(
                f"Blockscout GET {path} failed: {resp.status_code} {resp.text[:300]}"
            )
        time.sleep(config.BLOCKSCOUT_REQUEST_DELAY)  # stay under 5 RPS
        return resp.json()
    raise BlockscoutError(f"Blockscout GET {path} failed after maximum retry attempts")


def get_latest_block_number() -> int:
    """Current chain height, used as the upper bound for binary search."""
    data = _get("/main-page/blocks")
    if not data:
        raise BlockscoutError("Could not determine latest block number")
    # main-page/blocks returns a list of recent blocks, newest first
    return int(data[0]["height"])


def get_block(block_number: int) -> dict:
    """Fetch a single block's detail, including its unix timestamp."""
    return _get(f"/blocks/{block_number}")


def block_timestamp(block_number: int) -> int:
    """Return the unix timestamp (seconds) of a given block."""
    block = get_block(block_number)
    # Blockscout returns ISO8601; normalize to unix seconds.
    from datetime import datetime

    ts_str = block["timestamp"]
    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    return int(dt.timestamp())


def get_block_by_timestamp(target_ts: int, latest_block: int | None = None) -> int:
    """
    Binary search for the block number whose timestamp is closest to
    (and not after) target_ts. Robinhood Chain's ~0.1s block time means
    this converges in relatively few requests even over long ranges,
    but each step is a network call — cache results if calling repeatedly
    for the same time range.
    """
    hi = latest_block or get_latest_block_number()
    lo = 0
    result = 0

    while lo <= hi:
        mid = (lo + hi) // 2
        ts = block_timestamp(mid)
        if ts <= target_ts:
            result = mid
            lo = mid + 1
        else:
            hi = mid - 1

    return result


def get_token_transfers(token_address: str, start_block: int, end_block: int) -> list[dict]:
    """
    Pull all token-transfer events for a given ERC-20 token contract
    between start_block and end_block (inclusive) using direct JSON-RPC eth_getLogs
    from the Robinhood Chain RPC.
    """
    # 1. Fetch token details once from Blockscout to get decimals
    decimals = 18
    try:
        token_info = _get(f"/tokens/{token_address}")
        if token_info and "decimals" in token_info:
            decimals = int(token_info["decimals"])
    except Exception as e:
        logger.warning("Could not fetch token decimals for %s: %s. Defaulting to 18.", token_address, e)

    rpc_url = "https://rpc.mainnet.chain.robinhood.com"
    transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    
    # Chunk size: 5,000 blocks to stay comfortably under standard RPC size/weight limits
    chunk_size = 5000
    transfers: list[dict] = []
    
    current_start = start_block
    while current_start <= end_block:
        current_end = min(current_start + chunk_size - 1, end_block)
        
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getLogs",
            "params": [{
                "fromBlock": hex(current_start),
                "toBlock": hex(current_end),
                "address": token_address,
                "topics": [transfer_topic]
            }],
            "id": 1
        }
        
        success = False
        for attempt in range(3):
            try:
                # Override auth headers from session to make a clean RPC call
                r = requests.post(rpc_url, json=payload, headers={"Content-Type": "application/json"}, timeout=config.REQUEST_TIMEOUT_SECONDS)
                if r.status_code == 200:
                    data = r.json()
                    if "error" in data:
                        raise BlockscoutError(f"RPC Error: {data['error']}")
                    
                    results = data.get("result", [])
                    for log in results:
                        topics = log.get("topics") or []
                        if len(topics) < 3:
                            continue
                        
                        block_num = int(log["blockNumber"], 16)
                        tx_hash = log["transactionHash"]
                        from_addr = "0x" + topics[1][-40:]
                        to_addr = "0x" + topics[2][-40:]
                        
                        val_hex = log.get("data") or "0x0"
                        try:
                            value = int(val_hex, 16)
                        except ValueError:
                            value = 0
                            
                        transfers.append({
                            "block_number": block_num,
                            "transaction_hash": tx_hash,
                            "from": {"hash": from_addr},
                            "to": {"hash": to_addr},
                            "total": {"value": str(value)},
                            "token": {"decimals": decimals, "address": token_address}
                        })
                    
                    success = True
                    break
                else:
                    logger.warning("RPC returned status %d (attempt %d/3), retrying...", r.status_code, attempt + 1)
                    time.sleep(1.0)
            except Exception as e:
                logger.warning("RPC call failed (attempt %d/3): %s. Retrying...", attempt + 1, e)
                time.sleep(1.0)
                
        if not success:
            raise BlockscoutError(f"Failed to fetch RPC logs for block range {current_start}-{current_end}")
            
        current_start = current_end + 1
        
    # Sort transfers descending (newest block number first) to match Blockscout API sorting
    transfers.sort(key=lambda x: x["block_number"], reverse=True)
    return transfers


def get_address_token_transfers(
    address_hash: str,
    token_type: str = "ERC-20",
    max_pages: int = 5,
) -> list[dict]:
    """
    Pull all token-transfer events for a given address (wallet/contract),
    across up to max_pages. Stops early if no more pages are found.
    """
    transfers: list[dict] = []
    params: dict = {"type": token_type}
    path = f"/addresses/{address_hash}/token-transfers"

    for _ in range(max_pages):
        data = _get(path, params=params)
        items = data.get("items", [])
        if not items:
            break
        transfers.extend(items)
        next_params = data.get("next_page_params")
        if not next_params:
            break
        params = next_params

    return transfers


def get_transaction_token_transfers(tx_hash: str) -> list[dict]:
    """
    Full set of token transfers that occurred inside a single transaction,
    retrieved using direct RPC eth_getTransactionReceipt to avoid Blockscout rate/server limits.
    """
    rpc_url = "https://rpc.mainnet.chain.robinhood.com"
    transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getTransactionReceipt",
        "params": [tx_hash],
        "id": 1
    }
    
    legs: list[dict] = []
    
    for attempt in range(3):
        try:
            r = requests.post(rpc_url, json=payload, headers={"Content-Type": "application/json"}, timeout=config.REQUEST_TIMEOUT_SECONDS)
            if r.status_code == 200:
                data = r.json()
                if "error" in data:
                    raise BlockscoutError(f"RPC getTransactionReceipt Error: {data['error']}")
                
                receipt = data.get("result") or {}
                logs = receipt.get("logs") or []
                for log in logs:
                    topics = log.get("topics") or []
                    if len(topics) < 3:
                        continue
                    if topics[0].lower() != transfer_topic:
                        continue
                    
                    addr = log.get("address", "").lower()
                    from_addr = "0x" + topics[1][-40:]
                    to_addr = "0x" + topics[2][-40:]
                    
                    val_hex = log.get("data") or "0x0"
                    try:
                        value = int(val_hex, 16)
                    except ValueError:
                        value = 0
                        
                    # Decimals configuration for common tokens
                    decimals = 18
                    symbol = "TOKEN"
                    if addr == "0x0bd7d308f8e1639fab988df18a8011f41eacad73": # WETH
                        symbol = "WETH"
                        decimals = 18
                    elif "usd" in addr: # Simple wildcard check for USDC/USDT etc
                        symbol = "USD"
                        decimals = 6
                        
                    legs.append({
                        "token": {
                            "address_hash": addr,
                            "decimals": decimals,
                            "symbol": symbol
                        },
                        "total": {
                            "value": str(value)
                        },
                        "from": {
                            "hash": from_addr
                        },
                        "to": {
                            "hash": to_addr
                        }
                    })
                return legs
            else:
                logger.warning("RPC getTransactionReceipt returned status %d, retrying...", r.status_code)
                time.sleep(1.0)
        except Exception as e:
            logger.warning("RPC getTransactionReceipt failed: %s, retrying...", e)
            time.sleep(1.0)
            
    logger.error("Failed to fetch RPC receipt for transaction %s after 3 attempts.", tx_hash)
    return []


def get_transaction_logs(tx_hash: str) -> list[dict]:
    """
    Raw event logs for a transaction (e.g. Uniswap-v2-style Sync/Swap
    events), used to recover pool reserves at the moment of a trade.
    """
    data = _get(f"/transactions/{tx_hash}/logs")
    return data.get("items", [])


def get_oldest_funding_transaction(wallet_address: str, max_pages: int = 50) -> dict | None:
    """
    Paginate to the very first transaction of a wallet and return the first
    incoming coin transfer representing gas funding.
    """
    path = f"/addresses/{wallet_address}/transactions"
    params = {}

    for _ in range(max_pages):
        data = _get(path, params=params)
        items = data.get("items", [])
        if not items:
            return None

        next_params = data.get("next_page_params")
        if not next_params:
            # We are on the oldest page. Search in reverse chronological order
            # (oldest is at the end of the list) for the first incoming coin transfer.
            for tx in reversed(items):
                val_str = tx.get("value") or "0"
                try:
                    val = float(val_str)
                except (ValueError, TypeError):
                    val = 0.0
                to_hash = (tx.get("to") or {}).get("hash", "").lower()
                # Check if it was an incoming transfer of coin (native gas) to this wallet
                if to_hash == wallet_address.lower() and val > 0:
                    return tx
            return None
        params = next_params

    return None


def get_address_transactions(address_hash: str, max_pages: int = 2) -> list[dict]:
    """
    Pull general transactions for a given address.
    """
    txs: list[dict] = []
    params: dict = {}
    path = f"/addresses/{address_hash}/transactions"

    for _ in range(max_pages):
        data = _get(path, params=params)
        items = data.get("items", [])
        if not items:
            break
        txs.extend(items)
        next_params = data.get("next_page_params")
        if not next_params:
            break
        params = next_params

    return txs