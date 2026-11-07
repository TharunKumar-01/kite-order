# Trading Research Assistant

A local macOS tool that screens NSE stocks before the market opens and sends up to 5 picks to Telegram, where you approve or reject each one. It paper-trades the approved picks with realistic Zerodha delivery costs and scores itself against Nifty every evening.

**It never places orders.** You place every real trade yourself in Kite. This is a learning experiment for a small personal account, **not financial advice**.

## How a day works (all times IST, regardless of the Mac's timezone)

| When | What |
|---|---|
| 08:15–09:10 | Pre-market screen → Telegram: a header with global cues, then each pick with Approve/Reject buttons. "No picks today" if nothing qualifies. If the Mac wakes after 09:10, you get a "missed" notice instead of stale picks. |
| any time | Tap Approve/Reject (each pick records one decision; later taps are ignored). Reply `bought HAL 1 4210.5` to log a real fill. |
| 11:00 | Picks with no decision are marked *expired*. |
| ≥16:30 | Evening run: open, 09:45 price and close for today's picks; gap vs post-open move; paper portfolio net of costs; hit rate vs Nifty at 1/3/5 days. If the Mac was asleep, it catches up before the next pre-market run. |
| Saturday | Weekly report broken down by sector, by rule, and approved vs rejected picks. |

## Setup

```bash
cd trading-assistant
/opt/homebrew/bin/python3.13 -m venv .venv          # any Python 3.11+
.venv/bin/pip install -e ".[dev]"                    # add ,llm for the optional LLM summarizer
cp .env.example .env                                 # then fill in (below)
.venv/bin/python -m assistant.cli init-db
.venv/bin/python -m assistant.cli selftest
.venv/bin/python -m assistant.cli premarket --dry-run   # prints picks, sends/stores nothing
.venv/bin/pytest -q
```

### Telegram bot

1. In Telegram, message **@BotFather** → `/newbot` → pick a name. Copy the token into `TELEGRAM_BOT_TOKEN` in `.env`.
2. Open a chat with your new bot and send it any message (e.g. `hi`).
3. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and find `"chat":{"id":123456789,...}`. Put that number in `TELEGRAM_CHAT_ID`.
4. `selftest` should now show `Telegram OK`. The bot ignores every chat except this one.

### Run it automatically (launchd)

```bash
launchd/install.sh      # installs both agents; safe to re-run
launchd/uninstall.sh    # removes them (keeps data/ and logs/)
```

- `com.user.trading-assistant.tick` runs `cli tick` every 5 minutes and at login. Each tick converts "now" to IST and runs whatever is due and not yet done (state lives in the `job_runs` table). Calendar times are never hard-coded into launchd, so travelling or changing the Mac's timezone doesn't move the IST windows.
- `com.user.trading-assistant.bot` keeps the Telegram listener alive (`KeepAlive`). Telegram holds updates for about 24 hours, so taps you make while the Mac sleeps are processed when it wakes.

**Wake the Mac before the pre-market window (optional):**

```bash
sudo pmset repeat wakeorpoweron MTWRF 08:05:00
```

`pmset` uses the Mac's **local** time. If you change timezone, re-run it with the local time that equals 08:05 IST. With the lid closed on battery, the Mac may not wake reliably. It works best plugged in. Check the schedule with `pmset -g sched`.

## Commands

`python -m assistant.cli <cmd>`

| cmd | |
|---|---|
| `premarket [--dry-run] [--force]` | Screen and send picks. `--dry-run` prints only. `--force` ignores the holiday and cutoff checks. |
| `evening [--date YYYY-MM-DD] [--dry-run]` | Score picks, rebuild the paper portfolio, send the summary. |
| `tick` | What launchd calls every 5 minutes. |
| `bot` | Telegram listener. |
| `backtest --years 5 [--no-refresh]` | Replays the price rules. Writes `reports/backtest_<date>.md`. |
| `report [--weekly]` | Prints paper P&L and status to the console. |
| `init-db`, `selftest` | |

Telegram commands: `/status`, `/pnl`, `/pause`, `/resume`, `/help`.

## Selection logic (deterministic, rules only)

Price rules use daily bars up to the prior close: trend (close > SMA50 > SMA200), 20-day closing breakout, volume > 1.5× the 20-day average, RSI(14) < 75, and 3-month return vs Nifty. Context adds a keyword news score from Google News RSS (clipped to ±3; ≤ −2 excludes the stock), the sector's last move (sector index, or peer average if there's no index), relevant overnight cues (e.g. Nasdaq/USD-INR for IT, SOX for semis, crude for energy/paints/aviation), and sector-level news.

`total = Σ weight × component` (weights in `config.yaml`). A stock needs `trend` and `not_overextended`, a close ≤ `max_position_value` (₹2,500), a 20-day average traded value ≥ ₹2 crore, a latest bar that isn't stale, and a total ≥ `min_total_score`. The top 5 are sent.

No LLM chooses or ranks anything. If `llm.enabled: true` and `ANTHROPIC_API_KEY` is set, Claude only writes the 2–3 sentence "Why" text. It gets the headlines (wrapped as untrusted data) plus the already-computed scores. On any error or refusal it falls back to the keyword summarizer, and the summarizer used is logged per pick. The default model is `claude-opus-5-5`. For this simple task a cheaper model such as `claude-haiku-5-5` works too; set it in `config.yaml`. Requests opt into Anthropic's server-side refusal fallback (`fallbacks: "default"`).

## Paper trading

- **All-picks log (signal quality):** every pick, approved or not. Entry is at that session's open. It records the 09:45 price, the gap (prev close → open) and post-open move (open → close), and returns at the close of trading days 1, 3 and 5 (entry day = day 1), each vs Nifty and vs the sector index.
- **Paper portfolio:** approved picks only. At most 2 open positions of up to ₹2,500 each, whole shares only. It buys at the open + 0.1% slippage and exits at the close after 5 trading days, or at the first close ≤ entry −6%. Full Zerodha delivery costs apply on both legs, including the flat DP charge on sells. It's compared with the same ₹5,000 put into Nifty 50 on the first paper day.
- The paper portfolio is **recomputed from scratch every evening**, so it's idempotent and a missed evening fills itself in. Paper entry uses the open even if you approved a few minutes after 09:15.

### Costs: verify them

The defaults in `config.yaml → costs` follow Zerodha's published delivery charges as of this build. **Check a sample trade against https://zerodha.com/brokerage-calculator** and adjust. On a ₹2,500 position, a round trip costs about ₹21 (≈0.85%), and most of that is the flat ~₹15 DP charge on the sell. On small positions, costs dominate.

## Backtest caveats

- Only price rules are replayed. There's no news and no sector or global cues, because historical news isn't available.
- Signals use bars up to the prior close, and entry is at the next open (there's a test for look-ahead).
- **Survivorship bias:** the universe is *today's* list.
- Affordability uses the actual (unadjusted) close as of each date.

## Data notes

- `data/universe.csv`: Nifty 100 names (as best known at build time; membership changes, so edit it) plus a `watchlist` group for semiconductor, EV, defence and infra names outside the Nifty 100. Columns: `symbol,name,sector,themes,group,aliases`. Aliases help match headlines (e.g. "Kotak" → KOTAKBANK).
- `LTIM.NS` currently returns no data from Yahoo (likely renamed). It's skipped automatically; fix the symbol when you know it.
- `data/holidays.csv`: NSE weekday holidays. **Verify it against NSE's official circular every year.** If a weekday has no market data by 21:00 IST, it's inferred as a holiday and you're told.
- Yahoo sometimes returns the latest daily bar without a close. Those bars are rebuilt from hourly bars, and any stock whose latest bar is still older than the others is skipped (and the header says so).

## Safety

- Secrets live only in `.env`, which is git-ignored. Log lines are redacted, and httpx URL logging (which contains the bot token) is silenced.
- Only `TELEGRAM_CHAT_ID` is served. Messages from any other chat are ignored.
- No Kite order API anywhere. `tests/test_news_and_safety.py::test_no_order_placement_code` enforces it, and `selftest` checks it too.
- Every external call has timeouts and retries. If a source fails, the message says what's missing instead of crashing.
- If a job fails, you get a Telegram alert with a one-line traceback summary, and retries are capped at 3. Logs rotate in `logs/`.
