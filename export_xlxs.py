"""
Exports the GMGN-based pipeline's per-wallet aggregates (from
scoring/wallet_aggregator.py) to an Excel workbook.

One tab, "Wallet Summary", one row per wallet, with the numbers the pipeline
already computed from GMGN — total buy/sell (USD), net, trade counts, profit on
this token, and GMGN's wallet-level winrate / 7d profit / pnl ratio. Values are
written directly (not via SUMIFS) because each wallet is already a single
aggregated row here, not a stream of individual trade events.

This replaces the old Trade-based exporter: the previous "GMGN Cross-Check" tab
existed to check a wallet's reputation on some OTHER chain GMGN indexed, back
when RH Chain wasn't indexed. GMGN now indexes RH Chain directly, so those stats
are already folded into each aggregate — no separate cross-check sheet needed.
"""

from datetime import datetime, timezone

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from scoring.wallet_aggregator import WalletAggregate, insider_signals

HEADER_FONT = Font(name="Arial", bold=True, color="FFFFFF")
HEADER_FILL = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
BODY_FONT = Font(name="Arial")


def _style_header_row(ws, row: int, n_cols: int):
    for col in range(1, n_cols + 1):
        cell = ws.cell(row=row, column=col)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL


def _ts_to_dt(ts: int | None):
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def _num(value):
    """Parse GMGN's string/number fields to float, or None."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _wallet_pnl_rows(events: list[dict]) -> list[dict]:
    """
    Combine each wallet's buys and sells into one record, then derive cost basis
    and realized PnL by the average-cost method (from the COMBINED buy/sell, per
    the request), independent of GMGN's per-sell cost basis:

        avg_buy_price      = total_buy_usd / total_buy_tokens
        cost_basis_of_sold = avg_buy_price * total_sell_tokens
        realized_pnl       = total_sell_usd - cost_basis_of_sold
        realized_pnl_pct   = realized_pnl / cost_basis_of_sold

    Values that can't be computed (e.g. a wallet that sold without any buy in the
    data, so no average buy price) are left as None rather than a misleading 0.
    """
    acc: dict[str, dict] = {}
    for e in events:
        w = (e.get("wallet") or "").lower()
        if not w:
            continue
        side = (e.get("event_type") or e.get("type") or "").lower()
        cost = _num(e.get("cost_usd")) or 0.0
        amount = _num(e.get("token_amount")) or 0.0
        a = acc.setdefault(w, {
            "wallet": w, "symbol": (e.get("token") or {}).get("symbol"),
            "buy_count": 0, "buy_usd": 0.0, "buy_tokens": 0.0,
            "sell_count": 0, "sell_usd": 0.0, "sell_tokens": 0.0,
        })
        if side == "buy":
            a["buy_count"] += 1
            a["buy_usd"] += cost
            a["buy_tokens"] += amount
        elif side == "sell":
            a["sell_count"] += 1
            a["sell_usd"] += cost
            a["sell_tokens"] += amount

    rows = []
    for a in acc.values():
        avg_buy_price = a["buy_usd"] / a["buy_tokens"] if a["buy_tokens"] > 0 else None
        cost_basis_sold = avg_buy_price * a["sell_tokens"] if avg_buy_price is not None else None
        realized = a["sell_usd"] - cost_basis_sold if cost_basis_sold is not None else None
        realized_pct = (realized / cost_basis_sold * 100) if (cost_basis_sold and cost_basis_sold > 0) else None
        rows.append({
            **a,
            "avg_buy_price": avg_buy_price,
            "cost_basis_sold": cost_basis_sold,
            "realized_pnl": realized,
            "realized_pnl_pct": realized_pct,
        })
    rows.sort(key=lambda r: (r["realized_pnl"] is not None, r["realized_pnl"] or 0), reverse=True)
    return rows


def export_transactions(events: list[dict], output_path: str):
    """
    Two-tab workbook from raw GMGN wallet_activity events:
      - "Transactions": one row per buy/sell (time, wallet, side, symbol, amount,
        USD value, gas, tx hash), newest first.
      - "Wallet PnL": one row per wallet combining all its buys and sells, with
        cost basis and realized PnL derived from those combined totals.
    """
    if not events:
        raise ValueError("No transactions to export — nothing was ingested.")

    rows = sorted(events, key=lambda e: int(e.get("timestamp") or 0), reverse=True)

    wb = Workbook()

    # ---------- Transactions tab ----------
    ws = wb.active
    ws.title = "Transactions"
    headers = [
        "Time (UTC)", "Wallet", "Side", "Symbol",
        "Token Amount", "Value (USD)", "Gas (USD)", "Tx Hash",
    ]
    ws.append(headers)
    _style_header_row(ws, 1, len(headers))

    for e in rows:
        side = (e.get("event_type") or e.get("type") or "").lower()
        token = e.get("token") or {}
        ts = e.get("timestamp")
        ws.append([
            _ts_to_dt(int(ts)) if ts else None,
            e.get("wallet"),
            side,
            token.get("symbol"),
            _num(e.get("token_amount")),
            _num(e.get("cost_usd")),
            _num(e.get("gas_usd")),
            e.get("tx_hash"),
        ])
        r = ws.max_row
        for c in range(1, len(headers) + 1):
            ws.cell(row=r, column=c).font = BODY_FONT
        ws.cell(row=r, column=1).number_format = "yyyy-mm-dd hh:mm:ss"
        ws.cell(row=r, column=5).number_format = "#,##0.########"
        ws.cell(row=r, column=6).number_format = "$#,##0.00"
        ws.cell(row=r, column=7).number_format = "$#,##0.00"

    for i, header in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(i)].width = max(14, len(header) + 2)
    ws.column_dimensions["B"].width = 44  # wallet
    ws.column_dimensions["H"].width = 68  # tx hash

    # ---------- Wallet PnL tab ----------
    ws2 = wb.create_sheet("Wallet PnL")
    headers2 = [
        "Wallet", "Symbol",
        "Buy Count", "Total Buy (USD)", "Total Buy (tokens)",
        "Sell Count", "Total Sell (USD)", "Total Sell (tokens)",
        "Avg Buy Price (USD)", "Cost Basis of Sold (USD)",
        "Realized PnL (USD)", "Realized PnL %",
    ]
    ws2.append(headers2)
    _style_header_row(ws2, 1, len(headers2))

    pnl_rows = _wallet_pnl_rows(events)
    for w in pnl_rows:
        ws2.append([
            w["wallet"], w["symbol"],
            w["buy_count"], w["buy_usd"], w["buy_tokens"],
            w["sell_count"], w["sell_usd"], w["sell_tokens"],
            w["avg_buy_price"], w["cost_basis_sold"],
            w["realized_pnl"], w["realized_pnl_pct"],
        ])
        r = ws2.max_row
        for c in range(1, len(headers2) + 1):
            ws2.cell(row=r, column=c).font = BODY_FONT
        for c in (5, 8):                        # token amounts
            ws2.cell(row=r, column=c).number_format = "#,##0.########"
        for c in (4, 7, 10, 11):                # USD columns
            ws2.cell(row=r, column=c).number_format = "$#,##0.00"
        ws2.cell(row=r, column=9).number_format = "$#,##0.00000000"  # avg buy price
        ws2.cell(row=r, column=12).number_format = "0.0"             # pnl %

    for i, header in enumerate(headers2, start=1):
        ws2.column_dimensions[get_column_letter(i)].width = max(14, len(header) + 2)
    ws2.column_dimensions["A"].width = 44  # wallet

    note_row = len(pnl_rows) + 3
    ws2.cell(
        row=note_row, column=1,
        value=(
            "Note: cost basis and realized PnL use the average-cost method on each "
            "wallet's COMBINED buys/sells: avg_buy_price = Total Buy USD / Total Buy "
            "tokens; Cost Basis of Sold = avg_buy_price × Total Sell tokens; Realized "
            "PnL = Total Sell USD − Cost Basis of Sold. Blank = not computable (e.g. a "
            "wallet that sold with no buy in the pulled data)."
        ),
    ).font = Font(name="Arial", italic=True, size=9, color="808080")

    wb.save(output_path)


def export_wallet_report(
    aggregates: list[WalletAggregate],
    output_path: str,
    token_label: str = "TOKEN",
):
    """
    Write per-wallet aggregates to `output_path`. Ranks by per-token profit
    (falling back to net USD when GMGN didn't report a profit for the token).
    Missing values are left blank rather than written as a misleading 0.
    """
    if not aggregates:
        raise ValueError("No wallet aggregates to export — nothing to write.")

    rows = sorted(
        aggregates,
        key=lambda a: (a.realized_profit_usd if a.realized_profit_usd is not None else a.net_usd),
        reverse=True,
    )

    wb = Workbook()
    ws = wb.active
    ws.title = "Wallet Summary"

    headers = [
        "Wallet", "Insider Signals", "Tags", "Token",
        "Total Buy (USD)", "Total Sell (USD)", "Net (USD)",
        "Buy Count", "Sell Count",
        "Profit (USD)", "Profit Change %",
        "Winrate %", "Wallet Profit (period)", "Wallet PnL Ratio",
        "First Trade (UTC)", "Last Trade (UTC)",
    ]
    ws.append(headers)
    _style_header_row(ws, 1, len(headers))

    for a in rows:
        profit_change_pct = a.profit_change * 100 if a.profit_change is not None else None
        winrate_pct = a.winrate * 100 if a.winrate is not None else None
        ws.append([
            a.wallet,
            ", ".join(insider_signals(a)),
            ",".join(a.tags),
            a.token_address or token_label,
            a.total_buy_usd,
            a.total_sell_usd,
            a.net_usd,
            a.buy_count,
            a.sell_count,
            a.realized_profit_usd,
            profit_change_pct,
            winrate_pct,
            a.wallet_realized_profit,
            a.wallet_pnl_ratio,
            _ts_to_dt(a.first_ts),
            _ts_to_dt(a.last_ts),
        ])
        r = ws.max_row
        for c in range(1, len(headers) + 1):
            ws.cell(row=r, column=c).font = BODY_FONT
        for c in (5, 6, 7, 10, 13):       # USD columns
            ws.cell(row=r, column=c).number_format = "$#,##0"
        for c in (11, 12):                # percentage columns
            ws.cell(row=r, column=c).number_format = "0.0"
        ws.cell(row=r, column=14).number_format = "0.000"        # pnl ratio
        for c in (15, 16):                # timestamps
            ws.cell(row=r, column=c).number_format = "yyyy-mm-dd hh:mm:ss"

    for i, header in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(i)].width = max(14, len(header) + 2)
    ws.column_dimensions["A"].width = 44  # wallet address
    ws.column_dimensions["B"].width = 34  # insider signals
    ws.column_dimensions["D"].width = 44  # token address

    note_row = len(rows) + 3
    ws.cell(
        row=note_row, column=1,
        value=(
            "Note: 'Profit', 'Total Buy/Sell' and Buy/Sell counts are for THIS token. In "
            "windowed mode (--from/--to) they are computed from wallet activity within the "
            "date range, and Profit = sum over in-window sells of (proceeds - cost basis); "
            "otherwise they are GMGN's all-time token_top_traders figures. 'Winrate', "
            "'Wallet 7d Profit' and 'Wallet PnL Ratio' are GMGN's cross-token 7d stats for "
            "the wallet. 'Insider Signals' are rule-based flags (GMGN rat_trader/sniper/"
            "smart_money tags, high winrate, profitable). Blank cells mean GMGN did not "
            "report that value (not zero)."
        ),
    ).font = Font(name="Arial", italic=True, size=9, color="808080")

    wb.save(output_path)
