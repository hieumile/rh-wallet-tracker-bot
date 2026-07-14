# rh-wallet-tracker-bot

Finds the wallets that trade a Robinhood-chain token profitably — insiders,
snipers, and "smart money" — and ranks the ones worth following. You give it a
token you already care about; it tells you **who trades it well**, aggregates
each wallet's **buy / sell / profit**, and builds a scored **watchlist** you can
later use to spot the tokens those wallets are buying next.

All data comes from the GMGN API (buy/sell volumes, USD prices, realized profit,
and smart-money tags), so there is no pool discovery or price reconstruction.

---

## Using the bot (for traders)

### 1. One-time setup

```bash
pip install -r requirements.txt
```

Create a `.env` file in the project root with your GMGN API key:

```
GMGN_API_KEY=your_key_here
```

> You apply for a GMGN API key via GMGN's cooperation form; they email you the
> key. Only the API key is needed for everything in this README.

### 2. Run it

```bash
python3 main.py <TOKEN_ADDRESS> [options]
```

The bot prints two tables — a per-wallet buy/sell/profit summary and a ranked
"wallets worth following" list — and (unless told not to) merges the scored
wallets into `watchlist.json`.

### 3. Command reference

| Part | What it does |
|------|--------------|
| `python3 main.py` | Runs the bot. |
| `<TOKEN_ADDRESS>` | **Required.** The token contract you want to analyse, e.g. `0x020bfc65…18b4`. The bot finds the wallets that traded *this* token. |
| `--tag <tag>` | Only look at wallets GMGN labels with this tag: `rat_trader` (insider / sneak trading), `smart_degen` (smart money), or `sniper` (bought at launch). Omit to scan all three by default. |
| `--all` | Ignore tags — consider **every** top trader of the token, not just tagged ones. |
| `--limit <n>` | How many traders to pull (per tag), max 100. Default 50. Lower = faster and gentler on rate limits. |
| `--from <when>` / `--to <when>` | Restrict buy/sell/profit to a single time window (UTC). Accepts `2026-07-13`, `13/7/2026`, or with a time `13/7/2026 12:00`. A date with no time covers the whole day. |
| `--window <START> <END>` | A time window, **repeatable** — pass it several times to combine multiple windows (a trade counts if it lands in any of them). Supports hour precision. |
| `--stats-period <7d\|30d>` | Track-record window used for scoring winrate / profit. Default `30d`. |
| `--export <file.xlsx>` | Write the per-wallet **summary** (buy/sell/profit, winrate, insider signals) to Excel. |
| `--txns <file.xlsx>` | Write the **raw transactions** to Excel — a "Transactions" tab (one row per buy/sell) plus a "Wallet PnL" tab (combined buy/sell → cost basis + realized PnL per wallet). |
| `--activity` | Recompute totals from raw transactions even without a time window (auditable, slower). |
| `--no-stats` | Skip the GMGN track-record lookup. Faster, but disables scoring and the watchlist. |
| `--watchlist <file.json>` | Where to save/merge the scored wallets. Default `watchlist.json`. Re-running on new tokens *adds* to it. |
| `--no-watchlist` | Score and print, but don't write the watchlist file. |

### 4. Examples

**Example 1 — Who are the smart-money wallets on this token, and save them to my watchlist?**

```bash
python3 main.py 0x020bfc650a365f8bb26819deaabf3e21291018b4 \
  --tag smart_degen --limit 30 --export cashcat_wallets.xlsx
```

Pulls the top 30 smart-money traders of the token, ranks them by their 30-day
track record, writes a per-wallet summary to `cashcat_wallets.xlsx`, and merges
the followed wallets into `watchlist.json`.

**Example 2 — Pull the raw transactions during two specific time windows.**

```bash
python3 main.py 0x020bfc650a365f8bb26819deaabf3e21291018b4 --all --limit 50 \
  --window "11/7/2026 05:00" "11/7/2026 06:00" \
  --window "12/7/2026 20:00" "12/7/2026 22:00" \
  --txns transactions.xlsx --no-stats --no-watchlist
```

Looks at all top traders, keeps only trades inside 05:00–06:00 on 11 Jul **or**
20:00–22:00 on 12 Jul (UTC), and dumps them to `transactions.xlsx` with a
combined buy/sell PnL tab. `--no-stats --no-watchlist` keeps it to pure data
ingestion.

### 5. Good to know

- **Use higher-activity tokens.** GMGN only indexes wallet data for tokens with
  enough trading; small/new tokens may return no traders. The address in the
  examples (CASHCAT) works well for testing.
- **Rate limits.** Keep `--limit` modest (≤50) — the bot throttles itself, but
  large pulls plus per-wallet lookups take time.
- **All times are UTC**, and profit/prices are in USD.
