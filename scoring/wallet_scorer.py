"""
Wallet scoring engine (Subsystem 2).

Turns a wallet's CROSS-TOKEN track record (GMGN wallet_stats, carried on a
WalletAggregate) into a single 0 to 100 score, so a seed token's traders can be
ranked into a list of wallets worth following. The seed token only supplies the
candidate pool; the score is about the wallet's overall record, not its result
on that one token — that's what makes a wallet worth tracking onto OTHER tokens
(the job Subsystem 3 will do).

The score is a transparent weighted blend of normalized components, each in
0..1, so every ranking is explainable (see WalletScore.components):

  winrate     ratio of profitable trades              (raw 0..1)
  pnl_ratio   realized_profit / total_cost            (0..3x -> 0..1)
  profit      absolute realized profit                (log-scaled to a cap)
  moonshot    share of trades that returned > 2x      (0..1)
  experience  distinct tokens traded                  (0..cap -> 0..1)
  tags        GMGN smart_money / insider labels       (0/0.5/1.0)

Hard filters (config) drop wallets we can't or shouldn't follow: too little
history (MIN_TOKEN_NUM), not net profitable (MIN_REALIZED_PROFIT_USD), or below
MIN_WINRATE. Wallets with no stats at all can't be assessed and are excluded.
"""

import math
from dataclasses import dataclass, field

import config
from scoring.wallet_aggregator import WalletAggregate, insider_signals

# GMGN tags that earn the "tags" component, and their credit (0..1).
_TAG_CREDIT = {
    "rat_trader": 1.0,   # insider / sneak trading
    "smart_degen": 1.0,  # smart money
    "sniper": 0.7,
    "kol": 0.5,
    "bluechip_owner": 0.5,
}


@dataclass
class WalletScore:
    wallet: str
    score: float
    seed_token: str | None
    # echoed track-record stats (for display / persistence)
    winrate: float | None
    realized_profit_usd: float | None
    pnl_ratio: float | None
    token_num: int | None
    moonshot_count: int | None
    tags: list[str] = field(default_factory=list)
    insider_signals: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _score_components(agg: WalletAggregate) -> dict[str, float]:
    """Each component normalized to 0..1 before weighting."""
    winrate = agg.winrate or 0.0

    pnl_ratio = agg.wallet_pnl_ratio or 0.0
    pnl_component = _clamp01(pnl_ratio / 3.0)  # 3x realized return -> full marks

    profit = agg.wallet_realized_profit or 0.0
    if profit <= 0:
        profit_component = 0.0
    else:
        # log-scaled: PROFIT_FULL_SCORE_USD -> 1.0, one decade below -> ~0.8, etc.
        cap = math.log10(max(10.0, config.PROFIT_FULL_SCORE_USD))
        profit_component = _clamp01(math.log10(profit) / cap)

    token_num = agg.wallet_token_num or 0
    if token_num > 0 and agg.wallet_moonshot_count is not None:
        moonshot_component = _clamp01(agg.wallet_moonshot_count / token_num)
    else:
        moonshot_component = 0.0

    tagset = {t.lower() for t in agg.tags}
    is_fresh = "fresh_wallet" in tagset
    experience_component = 1.0 if is_fresh else _clamp01(token_num / config.EXPERIENCE_FULL_TOKENS)
    tag_component = max((_TAG_CREDIT.get(t, 0.0) for t in tagset), default=0.0)

    return {
        "winrate": _clamp01(winrate),
        "pnl_ratio": pnl_component,
        "profit": profit_component,
        "moonshot": moonshot_component,
        "experience": experience_component,
        "tags": tag_component,
    }


def _passes_filters(agg: WalletAggregate) -> bool:
    # No stats at all -> can't assess a track record -> not followable.
    if agg.winrate is None and agg.wallet_realized_profit is None:
        return False
        
    tagset = {t.lower() for t in agg.tags}

    # 1. MEV Exclusions
    if tagset.intersection(config.EXCLUDE_TAGS):
        return False
    if agg.wallet_avg_holding_secs is not None and agg.wallet_avg_holding_secs < config.MIN_HOLDING_TIME_SECS:
        return False

    # 2. History & Profit Filters
    is_fresh = "fresh_wallet" in tagset
    if not is_fresh and (agg.wallet_token_num or 0) < config.MIN_TOKEN_NUM:
        return False
        
    if (agg.wallet_realized_profit or 0.0) < config.MIN_REALIZED_PROFIT_USD:
        return False
    if (agg.winrate or 0.0) < config.MIN_WINRATE:
        return False
    return True


def score_wallet(agg: WalletAggregate) -> WalletScore | None:
    """Score one wallet, or None if it fails the follow-worthiness filters."""
    if not _passes_filters(agg):
        return None

    components = _score_components(agg)
    weights = config.SCORE_WEIGHTS
    score = sum(components[k] * weights.get(k, 0) for k in components)

    return WalletScore(
        wallet=agg.wallet,
        score=round(score, 2),
        seed_token=agg.token_address,
        winrate=agg.winrate,
        realized_profit_usd=agg.wallet_realized_profit,
        pnl_ratio=agg.wallet_pnl_ratio,
        token_num=agg.wallet_token_num,
        moonshot_count=agg.wallet_moonshot_count,
        tags=list(agg.tags),
        insider_signals=insider_signals(agg),
        components={k: round(v, 3) for k, v in components.items()},
    )


def rank_wallets(aggregates: list[WalletAggregate]) -> list[WalletScore]:
    """Score every aggregate and return the survivors ranked high to low."""
    scored = [score_wallet(a) for a in aggregates]
    scored = [s for s in scored if s is not None]
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored
