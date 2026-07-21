"""
Persisted wallet watchlist — the durable hand-off from Subsystem 2 (scoring)
to Subsystem 3 (signal generator).

A JSON file mapping wallet address -> its latest score and track-record stats,
plus the set of seed tokens it was discovered on and when it was last updated.
`upsert` MERGES: running the pipeline on a new seed token adds newly-found
wallets and unions seed_tokens onto ones already present, so you can build the
watchlist up across many seed tokens without losing prior discoveries.

Schema (per wallet):
    {
      "wallet": "0x..",
      "score": 71.2,
      "winrate": 0.55, "realized_profit_usd": 20575.3, "pnl_ratio": 0.84,
      "token_num": 61, "moonshot_count": 1,
      "tags": ["smart_degen", "gmgn"],
      "insider_signals": ["smart_money", "high_winrate(55%)", "profitable"],
      "seed_tokens": ["0xtoken1", "0xtoken2"],
      "stats_period": "30d",
      "first_added": "2026-07-13T...Z",
      "updated_at": "2026-07-13T...Z"
    }
"""

import json
import os
from datetime import datetime, timezone

from scoring.wallet_scorer import WalletScore
from ingestion import blockscout_client as bs


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load(path: str) -> dict[str, dict]:
    """Load the watchlist as {wallet: entry}. Empty dict if the file is absent."""
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        data = json.load(f)
    # Stored as a list for readability; index by wallet on load.
    if isinstance(data, list):
        return {e["wallet"]: e for e in data if e.get("wallet")}
    if isinstance(data, dict):
        return {w: e for w, e in data.items()}
    return {}


def upsert(
    path: str,
    scores: list[WalletScore],
    seed_token: str | None,
    stats_period: str | None = None,
) -> dict[str, dict]:
    """
    Merge `scores` into the watchlist at `path` and write it back (ranked by
    score, highest first). Returns the merged {wallet: entry} map.
    """
    existing = load(path)
    now = _now_iso()

    for s in scores:
        prev = existing.get(s.wallet)
        seeds = set(prev.get("seed_tokens", [])) if prev else set()
        if seed_token:
            seeds.add(seed_token)

        existing[s.wallet] = {
            "wallet": s.wallet,
            "score": s.score,
            "winrate": s.winrate,
            "realized_profit_usd": s.realized_profit_usd,
            "pnl_ratio": s.pnl_ratio,
            "token_num": s.token_num,
            "moonshot_count": s.moonshot_count,
            "max_drawdown_ratio": s.max_drawdown_ratio,
            "volume_usd": s.volume_usd,
            "profit_factor": s.profit_factor,
            "sharpe_ratio": s.sharpe_ratio,
            "tx_count": s.tx_count,
            "tags": s.tags,
            "insider_signals": s.insider_signals,
            "components": s.components,
            "seed_tokens": sorted(seeds),
            "stats_period": stats_period,
            "first_added": prev.get("first_added", now) if prev else now,
            "updated_at": now,
        }

        # Trace parent wallet if it is a fresh wallet
        tags_lower = {t.lower() for t in s.tags}
        if "fresh_wallet" in tags_lower:
            try:
                tx = bs.get_oldest_funding_transaction(s.wallet, max_pages=3)
                if tx:
                    parent_addr = (tx.get("from") or {}).get("hash", "").lower()
                    is_contract = (tx.get("from") or {}).get("is_contract", False)
                    # We check that it is a regular EOA (not a smart contract or pool)
                    if parent_addr and not is_contract:
                        prev_parent = existing.get(parent_addr)
                        parent_seeds = set(prev_parent.get("seed_tokens", [])) if prev_parent else set()
                        if seed_token:
                            parent_seeds.add(seed_token)
                        
                        funded_set = set(prev_parent.get("funded_wallets", [])) if prev_parent else set()
                        funded_set.add(s.wallet)
                        
                        existing[parent_addr] = {
                            "wallet": parent_addr,
                            "score": s.score,  # Inherit child's score
                            "winrate": None,
                            "realized_profit_usd": None,
                            "pnl_ratio": None,
                            "token_num": None,
                            "moonshot_count": None,
                            "tags": sorted(list(set(prev_parent.get("tags", []) if prev_parent else []).union({"insider_funder"}))),
                            "insider_signals": sorted(list(set(prev_parent.get("insider_signals", []) if prev_parent else []).union({"insider_funder", f"funded_{s.wallet[:8]}"}))),
                            "components": {},
                            "seed_tokens": sorted(parent_seeds),
                            "stats_period": stats_period,
                            "first_added": prev_parent.get("first_added", now) if prev_parent else now,
                            "updated_at": now,
                            "funded_wallets": sorted(list(funded_set)),
                        }
            except Exception as e:
                # Watchlist upsert should be robust against single Blockscout request failures
                pass

    ranked = sorted(existing.values(), key=lambda e: e.get("score", 0), reverse=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(ranked, f, indent=2)
    os.replace(tmp, path)  # atomic write so a crash can't corrupt the watchlist

    return {e["wallet"]: e for e in ranked}
