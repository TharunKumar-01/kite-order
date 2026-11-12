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


## Paper-only mode and the pick tracker

Real Kite orders are **off** (`kite.real_orders: false` in `config.yaml`). Nothing is placed from Telegram; `/orderson`
can't override it, buy buttons are hidden, and typed `buy …` messages are refused. GTTs already in Kite (stop-losses,
target sells) are still tracked and reported.

Every evening the **pick tracker** auto-buys every morning pick on paper at that day's open (₹`tracker.amount_per_pick`, default ₹1,00,000
each, no Approve tap and no ₹2,500 cap), applies the paper stop-loss and hold period, and reports:
- a scorecard: picks up, picks beating Nifty, average return vs Nifty, net ₹ after charges, and approved vs rejected;
- each recent pick's entry → latest price, return, Nifty and sector over the same days, best/worst point;
- for picks that are losing or trailing Nifty, likely reasons read from the data: whole market fell, sector fell,
  stock-specific weakness, a gap-up open that faded, an overstretched entry (high RSI), gains given back, or
  negative headlines since (shown escaped, as untrusted text). These are after-the-fact readings, not proven causes.

Set `kite.real_orders: true` to allow real GTT orders again (each still needs a Confirm tap).

**Screen price limit:** while real orders are off, the morning screen allows stocks up to the paper amount
(₹1,00,000) instead of ₹2,500 per share, so expensive stocks are judged too.

## Telegram channel as a news source

`channels.names` in `config.yaml` (now `daytradertelugu`) lists public Telegram channels. They're read from the
public web page `t.me/s/<name>`, with no Telegram login:
- The 5-minute tick caches new posts every 30 minutes (07:00–22:00), because t.me often drops connections.
  The morning screen refreshes once more before it runs.
- Most posts are news-card images. They're upscaled with macOS `sips` and read with `tesseract` (free, on this
  Mac, nothing sent anywhere). Each image is read once and kept 14 days in `channel_posts`.
- A post is linked to a stock when it names it. The headline is the line naming the stock plus the bullets under
  it. Price tables are skipped, and group companies aren't confused ("Reliance Power" isn't Reliance Industries,
  "SBI Life" isn't SBI).
- Channel headlines feed the same keyword news score as Google News. The morning header lists every stock the
  channel mentioned (🟢/🔴 by keywords), and the pick tracker marks mentioned picks with 📣 and compares their
  returns with the rest.
- Post text is untrusted: sanitized, length-capped, only matched against names and keywords, and escaped when
  shown. It's never treated as an instruction or a reason to buy.

## Setup

```bash
cd ~/trading-assistant   # keep it outside Documents (see the system service section)
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

### Run it without being logged in (system service, recommended)

`launchd/install.sh` only runs while you're logged in to this macOS profile. To run the bot and the tick
from boot, even while you're using another profile, install them as LaunchDaemons from an **admin**
account:

```bash
sudo /Users/Tharun-Personal/trading-assistant/launchd/install-daemon.sh      # safe to re-run
sudo /Users/Tharun-Personal/trading-assistant/launchd/uninstall-daemon.sh    # remove (keeps data/ and logs/)
launchd/install-daemon.sh --dry-run    # only generate and check the files, no sudo needed
```

- The services run as the project's owner (`UserName` in the plist), so files stay owned by that user.
- The installer also does three other things:
  - disables any old per-user copies of the bot (in any profile; the plist is renamed `*.disabled`), so only
    one bot reads Telegram;
  - stops any running copies;
  - sets `pmset -c sleep 0`, so the Mac doesn't sleep on the charger.
- The project must live outside `Documents`, `Desktop` and `Downloads`. macOS privacy controls block
  background services there, so it lives in `~/trading-assistant`.
- FileVault is on. After a restart, nothing runs until some user logs in once (any profile), which unlocks
  the disk.
- Check status with `sudo launchctl print system/com.user.trading-assistant.bot | grep state`. Logs are in `logs/`.

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
| `scan [--print]` | Run the screen now, plus a holdings check. Sends to Telegram, or prints with `--print`. |
| `tick` | What launchd calls every 5 minutes. |
| `bot` | Telegram listener. |
| `backtest --years 5 [--no-refresh]` | Replays the price rules. Writes `reports/backtest_<date>.md`. |
| `report [--weekly]` | Prints paper P&L and status to the console. |
| `init-db`, `selftest` | |

Telegram commands: `/scan`, `/status`, `/pnl`, `/pause`, `/resume`, `/help` (plus the Kite ones below).

### On-demand scan (`/scan`)

`/scan` runs the same screen as the morning job at any time: prices, rules, sector and global cues, news.
It takes about 15–60 seconds.

- **Which prices it uses:** before 15:30 it uses only completed daily bars, so today's half-formed bar
  is ignored. After the close it includes today's bar and prepares for the next session.
- **Up to 5 buy ideas,** each with a 🛒 button (₹2,500 @now with the default stop-loss, Confirm still
  required). They're **not saved as picks**, so the paper portfolio and hit rates only track the morning run.
- **Holdings check** (needs that day's Kite login). Each holding gets one of these flags:
  - 📤 **book profit:** gain ≥ `review.take_profit_pct` (10%), and the stock closed below its 20-day average,
    or has RSI ≥ 75, or has negative news.
  - 🔒 **protect:** gain ≥ 10% and the trend is intact. It suggests a stop-loss near the 20-day average.
  - ⚠️ **weak:** a downtrend (below the 50-day average, which is below the 200-day), a 5% fall in 5 days while
    below the 50-day average, or negative news.
  - ✅ **no flag:** none of the above.
- **Rotation idea:** if anything is flagged 📤 or ⚠️, it shows how much exiting those would free and
  which buy ideas that would fund.
- **📉 Weakest stocks:** stocks in the universe that aren't in an uptrend, ranked by worst 3-month strength
  vs Nifty, listed as "avoid buying". Delivery (CNC) can't be sold short, so these are only sell trades
  if you own them.

## Selection logic (deterministic, rules only)

Price rules use daily bars up to the prior close: trend (close > SMA50 > SMA200), 20-day closing breakout, volume > 1.5× the 20-day average, RSI(14) < 75, and 3-month return vs Nifty. Context adds a keyword news score from Google News RSS (clipped to ±3; ≤ −2 excludes the stock), the sector's last move (sector index, or peer average if there's no index), relevant overnight cues (e.g. Nasdaq/USD-INR for IT, SOX for semis, crude for energy/paints/aviation), and sector-level news.

`total = Σ weight × component` (weights in `config.yaml`). A stock needs `trend` and `not_overextended`, a close ≤ `max_position_value` (₹2,500), a 20-day average traded value ≥ ₹2 crore, a latest bar that isn't stale, and a total ≥ `min_total_score`. The top 5 are sent.

No LLM chooses or ranks anything. If `llm.enabled: true` and `ANTHROPIC_API_KEY` is set, Claude only writes the 2–3 sentence "Why" text. It gets the headlines (wrapped as untrusted data) plus the already-computed scores. On any error or refusal it falls back to the keyword summarizer, and the summarizer used is logged per pick. The default model is `claude-opus-5-5`. For this simple task a cheaper model such as `claude-haiku-5-5` works too; set it in `config.yaml`. Requests opt into Anthropic's server-side refusal fallback (`fallbacks: "default"`).

## Paper trading

- **All-picks log (signal quality):** every pick, approved or not. Entry is at that session's open. It records the 09:45 price, the gap (prev close → open) and post-open move (open → close), and returns at the close of trading days 1, 3 and 5 (entry day = day 1), each vs Nifty and vs the sector index.
- **Paper portfolio:** approved picks only. At most 2 open positions of up to ₹2,500 each, whole shares only. It buys at the open + 0.1% slippage and exits at the close after `paper.hold_days` trading days (40, changed from 5 on 11 Nov 2026, see below), or at the first close ≤ entry −6%. Full Zerodha delivery costs apply on both legs, including the flat DP charge on sells. It's compared with the same ₹5,000 put into Nifty 50 on the first paper day.
- The paper portfolio is **recomputed from scratch every evening**, so it's idempotent and a missed evening fills itself in. Paper entry uses the open even if you approved a few minutes after 09:15.

### Costs: verify them

The defaults in `config.yaml → costs` follow Zerodha's published delivery charges as of this build. **Check a sample trade against https://zerodha.com/brokerage-calculator** and adjust. On a ₹2,500 position, a round trip costs about ₹21 (≈0.85%), and most of that is the flat ~₹15 DP charge on the sell. On small positions, costs dominate.

## Exit-rule study (11 Nov 2026)

This replayed the screen's 5-year signals (Nov 2021 – Nov 2026): ₹2,500 per trade, entry at the open,
Zerodha costs, and GTT-style intraday stops. "Excess" is the stock's gain minus Nifty's gain over the
same days.

| Exit rule (8% stop-loss) | Trades | Avg days | Stock | Nifty, same days | Excess | Net per trade |
|---|---|---|---|---|---|---|
| fixed 5 days | 2,822 | 5 | −0.01% | +0.08% | −0.10% | −₹19 |
| fixed 10 days | 2,062 | 9 | +0.34% | +0.10% | +0.24% | −₹11 |
| fixed 20 days | 1,505 | 17 | +1.02% | +0.22% | +0.81% | +₹4 |
| fixed 40 days | 1,107 | 28 | +2.43% | +0.42% | +2.01% | +₹36 |
| fixed 60 days | 901 | 38 | +4.04% | +0.64% | +3.39% | +₹75 |
| 40 days + 25% target (OCO default) | 1,175 | – | – | – | – | +₹16 |
| 40 days + 15% target | 1,343 | – | – | – | – | +₹4 |
| trailing 15% stop (max 120 days) | 777 | 50 | +4.56% | +0.70% | +3.86% | +₹87 |
| sell on close below 50-day avg (max 120 days) | 998 | 28 | +3.29% | +0.52% | +2.77% | +₹56 |

- Fixed profit targets (5/8/10/15%) made every combination worse. They cut the few big winners that
  carry the result.
- Buying on a dip (limit 0.3–2% below the open, expiring the same day) wasn't better than the
  "@now" uptick entry.
- Fewer than half the trades beat Nifty even when the average does. The edge comes from a minority of
  large winners.
- Caveats: survivorship bias (today's universe), price rules only, and a rising market (Nifty +40%).
  Treat these numbers as an upper bound.

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

## Ordering in Kite from Telegram (GTT triggers)

Since 1 Apr 2026 Zerodha only accepts regular API orders from a registered static IP. GTT triggers
were tested from this Mac on 7 Nov 2026 and are accepted without one, including a stop-loss sell
trigger placed before you hold the shares. So every order the bot sends is a GTT trigger: delivery
(CNC), NSE, limit price.

**One-time setup:** create a Kite Connect app (Personal plan) at developers.kite.trade with the
redirect URL `http://127.0.0.1:8765/kite`, then put `KITE_API_KEY` and `KITE_API_SECRET` in `.env`.

**Every morning:** Kite tokens expire daily. Log in again with `/kitelogin` in Telegram (copy the
127.0.0.1 address you land on and send it to the bot) or with `python -m assistant.cli kite-login`.

**How to order** (every order shows a summary and needs your ✅ Confirm tap within 3 minutes):

| Message | Meaning |
|---|---|
| `buy HAL 2 @4210 sl 3950` | 2 shares, limit 4210, stop-loss trigger 3950 |
| `buy PNB ₹2500 @now sl 6%` | as many shares as ₹2,500 buys, "now", stop-loss 6% below, per-stock target |
| `buy PNB ₹2500 @now sl 6% tp 20%` | same, with a 20% target (`tp 160` = a price, `tp none` = no target) |
| `buy ₹2500` | today's last approved pick, now, default 6% stop-loss |
| `... sl none` | no stop-loss |
| `/oco 5` (per-stock target) or `/oco 5 10%` | add a target to filled order 5: its stop-loss GTT is replaced by an OCO (preview, then Confirm) |
| 🛒 button after Approve | ₹2,500 now with the default stop-loss |
| `sell JPL all @1455` | target sell of all your JPL at ₹1,455 (trigger just above, so it fills) |
| `sell JPL 12 profit 4000` | price that keeps ₹4,000 after sell charges, from Kite's average buy price |
| `sell JPL all target 5%` | 5% above your average buy price |

- **"Now" is not a market order.** A GTT can't be one. It's a trigger 0.3% above the last price
  with a limit 0.5% above that, so it fires on a small uptick. If the price only falls, it never fires.
- **A price below the market** waits as a trigger until the price comes down.
- **Buy triggers last one trading day.** If a buy hasn't fired by 15:30 IST (`kite.buy_valid_until`), the bot
  deletes it and its stop-loss and tells you. An order confirmed after 15:30 is for the next trading day.
  Set `""` to keep buy triggers for Kite's full year. Target sells and stop-losses on filled buys never expire.
  Once a trigger fires, the limit order lasts for that day only.
- **Stop-loss + target = one OCO GTT.** Kite's two-leg GTT sells at whichever price comes first and
  cancels the other leg, so there's never a second sell waiting for shares you've already sold.
  - The stop-loss leg's limit is 1% below its trigger, so it fills on a fast fall.
  - The target leg's trigger sits just above its limit, so it fills once it fires.
  - **The default target is per stock** (`kite.target_mode: auto`): the rise that stock reached within the
    hold period (40 trading days) in half of all windows over the last 5 years, clamped to 4–25%. A quiet
    large-cap like GAIL gets ~8–9%; a volatile stock gets more. The Confirm message shows how an OCO with that
    target and stop would have played out from every past day (target first / stop first / neither).
    It's history, not a forecast. `target_mode: fixed` uses `default_target_pct` (10%); `none` = stop-loss only.
    A typed `tp 20%`, `tp 160` or `tp none` always wins. Per-stock targets are not backtested; fixed targets
    lowered the average in the study ("Exit-rule study").
  - The Confirm and `/oco` messages show **invested** (shares + buy charges) and, for each exit, **you get**
    (sale value minus sell charges) and the profit/loss after all charges.
  - With only a stop-loss or only a target, it's a single GTT.
  - It's placed together with the buy. If you already hold that stock, it waits until the new buy fills,
  so it can never sell shares you bought earlier.
- **Tracking:** the 5-minute tick (and `/orders`) reports when a trigger fires, when the buy fills
  (logged to `real_trades`), when it doesn't fill, and when the stop-loss fires. It also removes the
  stop-loss if the buy fails or lapses. This needs that day's Kite login; if you're not logged in, the
  bot reminds you once during market hours.
- **Limits:** ₹2,500 per order (`kite.max_order_value`, checked inside `kite.py` itself), 2 orders a
  day, a typo guard (price within 10% of the last price), stop-loss between 0.5% and 25%.
  `/stop` switches ordering off; `/orderson` switches it back on. `/cancel N` removes order N's triggers.
- **Prices come from Yahoo** because Kite quotes need the paid data plan, so they may lag slightly.
- The bot only touches triggers it created. Your own GTTs are never read for changes or deleted.
- Target sells check the quantity against your Kite holdings (at draft and again at Confirm), warn about any sell trigger already active on that stock, and don't count toward the 2-buys-a-day limit. Selling needs CDSL authorisation (DDPI, or authorise the holding that day).
- The time exit (`paper.hold_days`) isn't automated. Selling after the hold period is up to you.

## Safety

- Secrets live only in `.env`, which is git-ignored. Log lines are redacted, and httpx URL logging (which contains the bot token) is silenced.
- Only `TELEGRAM_CHAT_ID` is served. Messages from any other chat are ignored.
- No regular Kite order API anywhere. Only GTT triggers (CNC, LIMIT, value-capped) are sent, and only from `kite.py`. `tests/test_news_and_safety.py::test_no_regular_order_code` enforces it, and `selftest` checks it too.
- Every external call has timeouts and retries. If a source fails, the message says what's missing instead of crashing.
- If a job fails, you get a Telegram alert with a one-line traceback summary, and retries are capped at 3. Logs rotate in `logs/`.
