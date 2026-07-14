"""
Turns raw Blockscout transfer records into buy/sell-labeled trades with a
reconstructed price, and matches buys to sells per wallet (FIFO) to produce
completed round-trips for scoring.

Key assumption flagged up front: this module assumes a standard two-leg
AMM swap (token A transferred into the pool, token B transferred out, in
the same transaction) — true for Uniswap-v2-style pools, which is the most
common AMM design and a reasonable starting assumption for RH Chain DEXs.
It will NOT correctly price:
  - swaps routed through a native-ETH leg with no ERC-20 Transfer event
    for the ETH side (needs the tx's native `value` field instead)
  - multi-hop swaps (A -> B -> C in one tx) without extra logic to walk
    the full transfer sequence
  - concentrated-liquidity (Uniswap-v3-style) pools, where "reserves" isn't
    a meaningful concept the way it is for v2 pairs
Verify which AMM design RH Chain's DEXs actually use before trusting this
on real numbers — this covers the common case, not every case.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Trade:
    wallet: str
    token_address: str
    side: str  # "buy" or "sell"
    tx_hash: str
    amount: float
    price: float | None       # in terms of the paired asset (e.g. per-token price)
    pool_liquidity_usd: float | None
    timestamp: int


@dataclass
class RoundTrip:
    wallet: str
    token_address: str
    buy: Trade
    sell: Trade

    @property
    def pnl_pct(self) -> float | None:
        if not self.buy.price or self.buy.price == 0 or self.sell.price is None:
            return None
        return (self.sell.price / self.buy.price) - 1

    @property
    def hold_seconds(self) -> int:
        return self.sell.timestamp - self.buy.timestamp

    @property
    def size_ratio(self) -> float | None:
        """Buy size relative to pool depth at time of buy — proxy for conviction."""
        if not self.buy.pool_liquidity_usd:
            return None
        return (self.buy.amount * (self.buy.price or 0)) / self.buy.pool_liquidity_usd


def classify_transfer(transfer: dict, pair_address: str) -> str:
    """
    A raw ERC-20 transfer only says "from X to Y" — direction relative to
    the pool address is what tells you whether it's a buy or a sell.
      - tokens leaving the pool (from == pair)  -> the counterparty bought
      - tokens entering the pool (to == pair)   -> the counterparty sold
      - neither side is the pool                -> wallet-to-wallet transfer,
        not a trade (exclude from trade analysis, but don't just drop it
        silently — see note below)
    """
    from_addr = transfer["from"]["hash"].lower()
    to_addr = transfer["to"]["hash"].lower()
    pair = pair_address.lower()

    if from_addr == pair:
        return "buy"
    if to_addr == pair:
        return "sell"
    return "other"


def counterparty_wallet(transfer: dict, pair_address: str, side: str) -> str:
    """The non-pool address in a classified buy/sell transfer."""
    pair = pair_address.lower()
    from_addr = transfer["from"]["hash"].lower()
    to_addr = transfer["to"]["hash"].lower()
    return to_addr if side == "buy" else from_addr


def reconstruct_trade_price(
    tx_hash: str,
    token_address: str,
    all_token_transfers_in_tx: list[dict],
) -> float | None:
    """
    Derive a price for `token_address` in this specific transaction by
    finding the other token leg transferred in the same tx and computing
    a ratio. Returns None if the tx doesn't have a clean two-leg structure
    this function understands (see module docstring for known gaps) —
    callers should treat None as "price unknown for this trade" rather
    than guessing, so bad data doesn't silently corrupt scoring downstream.
    """
    legs = {}
    for t in all_token_transfers_in_tx:
        addr = t["token"]["address"].lower()
        amount = float(t["total"]["value"]) / (10 ** int(t["token"]["decimals"]))
        legs.setdefault(addr, 0.0)
        legs[addr] += amount

    target = token_address.lower()
    if target not in legs or len(legs) != 2:
        return None  # not a clean two-leg swap we can price confidently

    other_addr = next(a for a in legs if a != target)
    target_amount = legs[target]
    other_amount = legs[other_addr]

    if target_amount == 0:
        return None

    # price of target token, denominated in the other leg's units
    return other_amount / target_amount


def match_round_trips(trades: list[Trade]) -> list[RoundTrip]:
    """
    FIFO-match buys to sells per (wallet, token). This deliberately ignores
    partial-fill complexity (splitting a buy across multiple sells) for a
    first pass — every buy is matched to the next unmatched sell in time
    order. Good enough to validate the approach; revisit if wallets
    frequently do partial exits and the simplification is losing signal.
    """
    by_key: dict[tuple[str, str], list[Trade]] = {}
    for t in trades:
        by_key.setdefault((t.wallet, t.token_address), []).append(t)

    round_trips: list[RoundTrip] = []
    for (wallet, token), wallet_trades in by_key.items():
        wallet_trades.sort(key=lambda t: t.timestamp)
        buys = [t for t in wallet_trades if t.side == "buy"]
        sells = [t for t in wallet_trades if t.side == "sell"]

        bi, si = 0, 0
        while bi < len(buys) and si < len(sells):
            buy, sell = buys[bi], sells[si]
            if sell.timestamp <= buy.timestamp:
                si += 1  # a sell before this buy can't close it; skip (likely closes an earlier buy)
                continue
            round_trips.append(RoundTrip(wallet=wallet, token_address=token, buy=buy, sell=sell))
            bi += 1
            si += 1

    return round_trips


def now_utc_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())