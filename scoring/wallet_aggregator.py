"""
Aggregates GMGN data into one row per wallet: total buy, total sell, and
profit — plus GMGN's own winrate / pnl when available.

Two inputs, combined:
  - token_top_traders rows (per-token): GMGN already sums buy/sell volume and
    realized profit for each wallet on the specific token. Cheap and immediate.
  - wallet_activity rows (per-event, optional): individual buy/sell events with
    cost_usd / token_amount / timestamp. We re-aggregate these ourselves so the
    totals are auditable from raw events (which/amount/time), not just trusted
    from the summary endpoint. This is what replaces the old pool-based
    classify + FIFO + price-reconstruction path entirely.

All USD figures come straight from GMGN — no pool discovery, no price
reconstruction. Fields that GMGN omits come back as None rather than 0, so a
missing value is never silently treated as "zero profit".
"""

from dataclasses import dataclass, field


def _f(value) -> float | None:
    """Parse GMGN's mixed str/number fields into a float, or None if absent."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class WalletAggregate:
    wallet: str
    token_address: str | None
    tags: list[str] = field(default_factory=list)

    # totals (USD unless noted)
    total_buy_usd: float = 0.0
    total_sell_usd: float = 0.0
    total_buy_tokens: float = 0.0
    total_sell_tokens: float = 0.0
    buy_count: int = 0
    sell_count: int = 0

    # per-token profit for THIS token (from token_top_traders)
    realized_profit_usd: float | None = None
    unrealized_profit_usd: float | None = None
    profit_change: float | None = None   # ratio, e.g. 1.5 = +150%

    # wallet-level, cross-token verdict (from wallet_stats over stats_period) —
    # kept separate so a wallet's overall record is never confused with its
    # profit on THIS token. This is the basis for the scoring engine.
    winrate: float | None = None                  # 0..1
    wallet_realized_profit: float | None = None   # over stats_period
    wallet_pnl_ratio: float | None = None         # realized_profit / total_cost
    wallet_token_num: int | None = None           # distinct tokens traded (anti-luck)
    wallet_moonshot_count: int | None = None      # trades that returned > 2x
    wallet_avg_holding_secs: float | None = None
    wallet_max_drawdown_ratio: float | None = None # calculated from activity
    wallet_volume_usd: float | None = None        # total traded volume (USD)
    wallet_tx_count: int | None = None            # total transactions (buys + sells)
    wallet_profit_factor: float | None = None     # gross profits / gross losses
    wallet_sharpe_ratio: float | None = None      # risk-adjusted return ratio
    stats_period: str | None = None

    first_ts: int | None = None
    last_ts: int | None = None

    @property
    def net_usd(self) -> float:
        return self.total_sell_usd - self.total_buy_usd

    @property
    def total_profit_usd(self) -> float | None:
        if self.realized_profit_usd is None and self.unrealized_profit_usd is None:
            return None
        return (self.realized_profit_usd or 0.0) + (self.unrealized_profit_usd or 0.0)


def from_trader_row(row: dict, token_address: str) -> WalletAggregate:
    """
    Build a per-token aggregate directly from a token_top_traders row. GMGN has
    already summed buy/sell volume, cost and profit for this wallet on this token.
    """
    tag = row.get("wallet_tag_v2") or row.get("tag")
    return WalletAggregate(
        wallet=(row.get("address") or "").lower(),
        token_address=token_address.lower(),
        tags=[tag] if tag else [],
        total_buy_usd=_f(row.get("buy_volume_cur")) or 0.0,
        total_sell_usd=_f(row.get("sell_volume_cur")) or 0.0,
        total_buy_tokens=_f(row.get("buy_amount_cur")) or 0.0,
        total_sell_tokens=_f(row.get("sell_amount_cur")) or 0.0,
        buy_count=int(_f(row.get("buy_tx_count_cur")) or 0),
        sell_count=int(_f(row.get("sell_tx_count_cur")) or 0),
        realized_profit_usd=_f(row.get("realized_profit")) or _f(row.get("profit")),
        unrealized_profit_usd=_f(row.get("unrealized_profit")),
        profit_change=_f(row.get("profit_change")),
    )


Window = tuple[int | None, int | None]  # (start_ts, end_ts), None = open-ended


def in_windows(ts: int | None, windows: list[Window] | None) -> bool:
    """
    True if `ts` falls inside ANY of the given [start, end] windows (inclusive;
    None bound = open on that side). No windows -> everything matches. A None ts
    with windows set can't be placed, so it's excluded.
    """
    if not windows:
        return True
    if ts is None:
        return False
    for start, end in windows:
        if (start is None or ts >= start) and (end is None or ts <= end):
            return True
    return False


def aggregate_activity(
    wallet: str,
    activities: list[dict],
    token_address: str | None = None,
    windows: list[Window] | None = None,
) -> WalletAggregate:
    """
    Re-aggregate a wallet's raw activity events into totals we can defend from
    first principles, optionally restricted to one or more time `windows`
    (an event counts if it falls in ANY window).

    Buy/sell only (transfers ignored). Realized profit is computed from GMGN's
    own per-sell cost basis:
        sell event: cost_usd = USD proceeds, buy_cost_usd = cost basis of the
        tokens sold  ->  realized profit for that sell = cost_usd - buy_cost_usd
        buy event:  cost_usd = USD spent, buy_cost_usd = null
    Summing (cost_usd - buy_cost_usd) over sells in the window(s) gives realized
    profit FOR THOSE WINDOWS — unlike the all-time figure from token_top_traders.
    Left as None (blank) if no in-window sell carried a cost basis.
    """
    agg = WalletAggregate(
        wallet=wallet.lower(),
        token_address=token_address.lower() if token_address else None,
    )
    realized: float | None = None

    for a in activities:
        ts = a.get("timestamp")
        ts = int(ts) if ts is not None else None
        if windows and not in_windows(ts, windows):
            continue

        # GMGN labels the direction as `event_type` (buy/sell/add/remove/...).
        atype = (a.get("event_type") or a.get("type") or "").lower()
        cost = _f(a.get("cost_usd")) or 0.0
        amount = _f(a.get("token_amount")) or 0.0

        if ts is not None:
            agg.first_ts = ts if agg.first_ts is None else min(agg.first_ts, ts)
            agg.last_ts = ts if agg.last_ts is None else max(agg.last_ts, ts)

        if atype == "buy":
            agg.buy_count += 1
            agg.total_buy_usd += cost
            agg.total_buy_tokens += amount
        elif atype == "sell":
            agg.sell_count += 1
            agg.total_sell_usd += cost
            agg.total_sell_tokens += amount
            basis = _f(a.get("buy_cost_usd"))
            if basis is not None:
                realized = (realized or 0.0) + (cost - basis)

    agg.realized_profit_usd = realized
    return agg


INSIDER_TAG_LABELS = {
    "rat_trader": "insider(rat_trader)",
    "sniper": "sniper",
    "smart_degen": "smart_money",
}


def insider_signals(agg: WalletAggregate, min_winrate: float = 0.5) -> list[str]:
    """
    Transparent, rule-based flags for "is this an insider / informed wallet".
    Not a black-box score — each signal says why it fired, so the ranking is
    auditable. The strongest single signal is GMGN's own `rat_trader` tag
    (its insider / sneak-trading label).
    """
    signals: list[str] = []
    tagset = {t.lower() for t in agg.tags}
    for raw, label in INSIDER_TAG_LABELS.items():
        if raw in tagset:
            signals.append(label)
    if agg.winrate is not None and agg.winrate >= min_winrate:
        signals.append(f"high_winrate({agg.winrate*100:.0f}%)")
    if agg.realized_profit_usd is not None and agg.realized_profit_usd > 0:
        signals.append("profitable")
    return signals


def apply_stats(agg: WalletAggregate, stats: dict) -> WalletAggregate:
    """
    Overlay GMGN wallet_stats onto an aggregate as WALLET-LEVEL fields. Does NOT
    touch the per-token profit from token_top_traders — the two are different
    questions (profit on this token vs. the wallet's overall record).

    Field locations verified against the live response 2026-07-13: winrate lives
    under `pnl_stat.winrate` (not top-level, despite the docs), the pnl ratio is
    `realized_profit_pnl`, and tags are under `common.tags`.
    """
    pnl_stat = stats.get("pnl_stat") or {}
    if pnl_stat.get("winrate") is not None:
        agg.winrate = _f(pnl_stat.get("winrate"))
    if stats.get("realized_profit") is not None:
        agg.wallet_realized_profit = _f(stats.get("realized_profit"))
    if stats.get("realized_profit_pnl") is not None:
        agg.wallet_pnl_ratio = _f(stats.get("realized_profit_pnl"))
    if pnl_stat.get("token_num") is not None:
        agg.wallet_token_num = int(_f(pnl_stat.get("token_num")) or 0)
    
    # Calculate trading volume
    b_cost = _f(stats.get("bought_cost"))
    s_income = _f(stats.get("sold_income"))
    if b_cost is not None or s_income is not None:
        agg.wallet_volume_usd = (b_cost or 0.0) + (s_income or 0.0)
        
    # Calculate transaction count
    buy_cnt = _f(stats.get("buy"))
    sell_cnt = _f(stats.get("sell"))
    if buy_cnt is not None or sell_cnt is not None:
        agg.wallet_tx_count = int((buy_cnt or 0) + (sell_cnt or 0))

    # moonshots = trades that returned more than 2x (2x-5x plus >5x buckets)
    moon = _f(pnl_stat.get("pnl_2x_5x_num"))
    moon5 = _f(pnl_stat.get("pnl_gt_5x_num"))
    if moon is not None or moon5 is not None:
        agg.wallet_moonshot_count = int((moon or 0) + (moon5 or 0))
    if pnl_stat.get("avg_holding_period") is not None:
        agg.wallet_avg_holding_secs = _f(pnl_stat.get("avg_holding_period"))

    common = stats.get("common") or {}
    tags = common.get("tags")
    if isinstance(tags, list):
        for t in tags:
            if t and t not in agg.tags:
                agg.tags.append(t)
    return agg
