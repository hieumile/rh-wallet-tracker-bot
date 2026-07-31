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


def is_contract_address(address: str) -> bool:
    """Check if an address is a smart contract (contains bytecode) on Robinhood Chain."""
    rpc_url = "https://rpc.mainnet.chain.robinhood.com"
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getCode",
        "params": [address, "latest"],
        "id": 1
    }
    for attempt in range(3):
        try:
            r = requests.post(rpc_url, json=payload, headers={"Content-Type": "application/json"}, timeout=5)
            if r.status_code == 200:
                result = r.json().get("result") or "0x"
                return result != "0x" and result != ""
            elif r.status_code == 429:
                time.sleep(1.0)
        except Exception:
            pass
    return False


def get_latest_block_number() -> int:
    """Current chain height, fetched directly from JSON-RPC node for 100% stability."""
    rpc_url = "https://rpc.mainnet.chain.robinhood.com"
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_blockNumber",
        "params": [],
        "id": 1
    }
    try:
        r = requests.post(rpc_url, json=payload, headers={"Content-Type": "application/json"}, timeout=config.REQUEST_TIMEOUT_SECONDS)
        if r.status_code == 200:
            data = r.json()
            if "result" in data:
                return int(data["result"], 16)
    except Exception as e:
        logger.warning("RPC eth_blockNumber failed: %s. Falling back to Blockscout.", e)
        
    data = _get("/main-page/blocks")
    if not data:
        raise BlockscoutError("Could not determine latest block number")
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


_block_time_cache = {}


def get_block_timestamp(block_num: int) -> int:
    """Fetch the timestamp of a block using JSON-RPC, with caching."""
    if block_num in _block_time_cache:
        return _block_time_cache[block_num]
        
    rpc_url = "https://rpc.mainnet.chain.robinhood.com"
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getBlockByNumber",
        "params": [hex(block_num), False],
        "id": 1
    }
    import time
    for attempt in range(5):
        try:
            r = requests.post(rpc_url, json=payload, headers={"Content-Type": "application/json"}, timeout=5)
            if r.status_code == 200:
                res = r.json().get("result") or {}
                ts_hex = res.get("timestamp")
                if ts_hex:
                    ts = int(ts_hex, 16)
                    _block_time_cache[block_num] = ts
                    return ts
            elif r.status_code == 429:
                time.sleep(2.0 ** attempt)
            else:
                time.sleep(1.0)
        except Exception:
            time.sleep(1.0)
    return 0


def get_block_by_timestamp(target_ts: int, latest_block: int | None = None) -> int:
    """
    Find the block closest to target_ts using a standard binary search over the entire block range,
    using an in-memory cache to minimize RPC overhead.
    """
    import time
    now_ts = int(time.time())
    hi = latest_block or get_latest_block_number()
    
    if target_ts >= now_ts:
        return hi
        
    low = 0
    high = hi
    
    best_block = hi
    best_diff = float("inf")
    
    while low <= high:
        mid = (low + high) // 2
        ts = get_block_timestamp(mid)
        if ts == 0:
            # Fallback to linear estimation if RPC fails
            ts = now_ts - int((hi - mid) * 0.1)
            
        diff = ts - target_ts
        if abs(diff) < best_diff:
            best_diff = abs(diff)
            best_block = mid
            
        if ts < target_ts:
            low = mid + 1
        elif ts > target_ts:
            high = mid - 1
        else:
            return mid
            
    return best_block


def _fetch_chunk_with_split(token_address: str, current_start: int, current_end: int, decimals: int, transfer_topic: str, rpc_url: str) -> list[dict]:
    import time
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
    
    for attempt in range(10):
        try:
            r = requests.post(rpc_url, json=payload, headers={"Content-Type": "application/json"}, timeout=15)
            if r.status_code == 200:
                data = r.json()
                if "error" in data:
                    err = data["error"] or {}
                    err_msg = str(err.get("message", "")).lower()
                    if "exceeds limit" in err_msg or "limit of 10000" in err_msg or "too many" in err_msg:
                        if current_start >= current_end:
                            return []
                        mid = (current_start + current_end) // 2
                        left = _fetch_chunk_with_split(token_address, current_start, mid, decimals, transfer_topic, rpc_url)
                        right = _fetch_chunk_with_split(token_address, mid + 1, current_end, decimals, transfer_topic, rpc_url)
                        return left + right
                    else:
                        raise BlockscoutError(f"RPC Error: {err}")
                
                results = data.get("result", [])
                chunk_transfers = []
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
                        
                    chunk_transfers.append({
                        "block_number": block_num,
                        "transaction_hash": tx_hash,
                        "from": {"hash": from_addr},
                        "to": {"hash": to_addr},
                        "total": {"value": str(value)},
                        "token": {"decimals": decimals, "address": token_address}
                    })
                return chunk_transfers
            else:
                backoff = min((2.0 ** attempt) * 3.0, 30.0) if r.status_code == 429 else 2.0
                logger.warning("RPC returned status %d (attempt %d/10) for range %d-%d, retrying after %.1fs...", r.status_code, attempt + 1, current_start, current_end, backoff)
                time.sleep(backoff)
        except Exception as e:
            err_msg = str(e).lower()
            if "exceeds limit" in err_msg or "limit of 10000" in err_msg or "too many" in err_msg:
                if current_start >= current_end:
                    return []
                mid = (current_start + current_end) // 2
                left = _fetch_chunk_with_split(token_address, current_start, mid, decimals, transfer_topic, rpc_url)
                right = _fetch_chunk_with_split(token_address, mid + 1, current_end, decimals, transfer_topic, rpc_url)
                return left + right
            backoff = min((2.0 ** attempt) * 2.0, 20.0)
            logger.warning("RPC call failed (attempt %d/10) for range %d-%d: %s. Retrying after %.1fs...", attempt + 1, current_start, current_end, e, backoff)
            time.sleep(backoff)
            
    raise BlockscoutError(f"Failed to fetch logs for range {current_start}-{current_end}")


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
    
    # Chunk size: 100,000 blocks to stay within RPC limits while minimizing request count
    chunk_size = 100000
    
    # Generate chunks
    chunks = []
    current_start = start_block
    while current_start <= end_block:
        current_end = min(current_start + chunk_size - 1, end_block)
        chunks.append((current_start, current_end))
        current_start += chunk_size
        
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    transfers = []
    # Use 4 threads to fetch chunks concurrently to stay within RPC limits
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_fetch_chunk_with_split, token_address, s, e, decimals, transfer_topic, rpc_url): (s, e) for s, e in chunks}
        
        processed_chunks = 0
        for future in as_completed(futures):
            s, e = futures[future]
            try:
                chunk_res = future.result()
                transfers.extend(chunk_res)
                processed_chunks += 1
                logger.info("Ingested transfers chunk %d/%d (blocks %d-%d), found %d transfers in chunk.", processed_chunks, len(chunks), s, e, len(chunk_res))
            except Exception as ex:
                logger.error("Failed to fetch transfers for chunk %d-%d: %s", s, e, ex)
                raise ex
                
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
                    elif addr == "0x5fc5360d0400a0fd4f2af552add042d716f1d168": # USDG
                        symbol = "USDG"
                        decimals = 6
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


def get_transaction_token_transfers_batch(tx_hashes: list[str]) -> dict[str, list[dict]]:
    """
    Fetch token transfers for multiple transaction hashes in batches using direct JSON-RPC batch requests.
    Returns a dict mapping tx_hash to its list of transfer legs.
    """
    if not tx_hashes:
        return {}
        
    rpc_url = "https://rpc.mainnet.chain.robinhood.com"
    transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    
    results = {}
    # Reduced chunk size to 15 to stay within strict RPC rate limit caps
    chunk_size = 15
    chunks = [tx_hashes[i:i + chunk_size] for i in range(0, len(tx_hashes), chunk_size)]
    
    for idx_chunk, chunk in enumerate(chunks):
        # Build batch request payload
        payload = []
        for idx, h in enumerate(chunk):
            payload.append({
                "jsonrpc": "2.0",
                "method": "eth_getTransactionReceipt",
                "params": [h],
                "id": idx
            })
            
        # Initialize results for this chunk to empty lists
        for h in chunk:
            results[h] = []
            
        success = False
        # Increase attempts to 4
        for attempt in range(4):
            try:
                r = requests.post(
                    rpc_url, 
                    json=payload, 
                    headers={"Content-Type": "application/json"}, 
                    timeout=config.REQUEST_TIMEOUT_SECONDS
                )
                if r.status_code == 200:
                    batch_resp = r.json()
                    # Batch response can be a list or a single dict (if chunk was 1)
                    if isinstance(batch_resp, dict):
                        batch_resp = [batch_resp]
                        
                    # Build index-to-txhash mapping for this chunk
                    id_to_hash = {idx: h for idx, h in enumerate(chunk)}
                    
                    for item in batch_resp:
                        if not isinstance(item, dict):
                            continue
                        req_id = item.get("id")
                        tx_hash = id_to_hash.get(req_id)
                        if not tx_hash:
                            continue
                            
                        if "error" in item:
                            logger.warning("Batch RPC error for tx %s: %s", tx_hash, item["error"])
                            continue
                            
                        receipt = item.get("result") or {}
                        logs = receipt.get("logs") or []
                        legs = []
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
                                
                            decimals = 18
                            symbol = "TOKEN"
                            if addr == "0x0bd7d308f8e1639fab988df18a8011f41eacad73": # WETH
                                symbol = "WETH"
                                decimals = 18
                            elif addr == "0x5fc5360d0400a0fd4f2af552add042d716f1d168": # USDG
                                symbol = "USDG"
                                decimals = 6
                            elif "usd" in addr:
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
                        results[tx_hash] = legs
                    success = True
                    break
                else:
                    # Exponential or longer backoff for 429 rate limit
                    backoff = 3.0 if r.status_code == 429 else 1.5
                    logger.warning("RPC Batch getTransactionReceipt status %d, retrying after %.1fs...", r.status_code, backoff)
                    time.sleep(backoff)
            except Exception as e:
                logger.warning("RPC Batch getTransactionReceipt failed: %s, retrying after 2.0s...", e)
                time.sleep(2.0)
                
        if not success:
            logger.error("Failed to fetch RPC receipt batch for %d hashes after 4 attempts.", len(chunk))
            
        # Log progress
        fetched_count = min((idx_chunk + 1) * chunk_size, len(tx_hashes))
        logger.info("Fetched receipts for %d/%d transactions...", fetched_count, len(tx_hashes))
            
        # Add a minor delay between chunks to be gentler on the public RPC node
        if idx_chunk < len(chunks) - 1:
            time.sleep(0.1)
            
    return results


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