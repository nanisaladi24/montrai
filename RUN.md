# Running Montrai Manually

Everything here is run by hand — no cron, no launchd, nothing auto-starting at login. You are always the one who decides when it runs.

All paths assume you're in the repo root (`cd montrai`).

---

## 1. First-time / one-time setup

Only do these once. Skip on daily runs.

```bash
cd montrai

# Create venv + install deps (only if .venv/ doesn't exist)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Copy and fill in your API keys
cp .env.example .env
$EDITOR .env

# Must have at minimum:
#   BROKER=alpaca
#   TRADING_MODE=paper
#   ALPACA_API_KEY=PK...
#   ALPACA_SECRET_KEY=...
#   ALPACA_PAPER=true
#
# Recommended additional:
#   FINANCIAL_DATASETS_API_KEY   (free tier at financialdatasets.ai)
#   POLYGON_API_KEY              (free tier at massive.com)
#
# Verify the file before proceeding
cat .env
```

Run the test suite once to confirm everything installed cleanly:

```bash
.venv/bin/python -m pytest tests/ -q
# Expect: 52 passed in ~1s
```

---

## 2. Daily run — two terminals

### Terminal A — trading bot

```bash
cd montrai
.venv/bin/python main.py
```

What it does:
- Loads saved state from `bot_state.json` (any open paper positions resume).
- Refreshes the dynamic daily watchlist once per day pre-market.
- If market is closed, sleeps 5 min and re-checks.
- During market hours: 30-min cycles (or 5-min if intraday enabled). Each cycle: refresh regime → options execute phase → multi-leg phase → covered-call phase → intraday phase.

Leave the terminal open. The bot owns that terminal. Don't close it.

### Terminal B — dashboard

```bash
cd montrai
STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
  .venv/bin/streamlit run dashboard/app.py --server.port 8501
```

Open http://localhost:8501. The dashboard reads the same state + log files the bot writes, so as the bot works the session the dashboard updates live.

**Top banner is always visible:** `📄 PAPER TRADING` or `🚨 LIVE TRADING`. Check it before flipping any strategy toggle.

The dashboard is independent of the bot. You can start/stop it whenever.

---

## 3. Stopping cleanly

In either terminal: **Ctrl-C**.

The bot's Ctrl-C handler saves state to `bot_state.json` before exiting. Open positions remain open at the broker — they'll be picked up again on next start.

**Rule of thumb:** if you hold open positions and the market is open, the bot should be running. Stops are evaluated in-process every cycle, not as bracket orders at the broker, so a stopped bot = no active stop-loss protection until you restart.

Safe to stop: after 16:00 ET close, or any time you have no open positions.

---

## 4. One-shot commands (don't need the main loop)

```bash
cd montrai

# Rank the whole watchlist right now, no orders placed
.venv/bin/python main.py --scan

# Retrain the HMM regime model manually
.venv/bin/python main.py --train

# Walk-forward backtest, single symbol
.venv/bin/python main.py --backtest --symbol AAPL

# Walk-forward backtest, entire watchlist (slow)
.venv/bin/python main.py --backtest-all

# Sync Polygon S3 flat files (requires tier that includes download access)
.venv/bin/python scripts/polygon_s3_sync.py --dataset indices --days 30
```

---

## 5. Checking what's running

```bash
# Is the bot process alive?
ps aux | grep "main.py" | grep -v grep

# Is the dashboard alive?
ps aux | grep "streamlit run dashboard" | grep -v grep
```

Or open the dashboard — the bot-status pill at top says Running / Sleeping / Stopped / Lockout based on `bot_heartbeat.json`.

---

## 6. Reading logs

All logs live in `logs/`.

```bash
# Main bot log (regime, signals, orders, errors)
tail -f logs/main.log

# Broker fills + order IDs
tail -f logs/executor.log

# Risk-manager decisions (stops, blocks, lockouts)
tail -f logs/risk_manager.log

# Feature pipeline (FRED, GEX, macro)
tail -f logs/feature_eng.log
```

State + trade history:

```bash
cat bot_state.json             # positions, daily spend, dynamic watchlist
cat bot_heartbeat.json         # last cycle timestamp, regime, toggle state
cat logs/trades.csv            # append-only trade log
```

---

## 7. Enabling strategies

Everything is off by default. Flip toggles from the dashboard Settings tab — they write to `config/runtime.json` and take effect on the next bot cycle (no restart).

| Toggle | What it does | Default |
|---|---|---|
| `options_trading_enabled` | Long calls / long puts | **ON** |
| `spreads_enabled` | Vertical credit spreads (bull put credit, bear call credit) | OFF |
| `iron_condor_enabled` | Iron condor when \|score\| < 0.3 | OFF |
| `covered_call_enabled` | Write calls against held shares | OFF |
| `intraday_enabled` | Opening Range Breakout strategy (5-min cycles) | OFF |
| `stock_trading_enabled` | Swing stock trades | OFF |
| `dynamic_watchlist_enabled` | Pre-market mover discovery | **ON** |
| `paper_force_top_score` | Paper-only: fire top-\|score\| if nothing filled by EOD | OFF |

Same tab also lets you tune thresholds (score gates, delta targets, DTE windows, TP/SL percentages).

---

## 8. Going live

**Alpaca live path:**
1. Get a funded Alpaca live account.
2. Edit `.env`:
   ```
   TRADING_MODE=live
   ALPACA_PAPER=false
   ALPACA_API_KEY=<live paper key, DIFFERENT from paper>
   ALPACA_SECRET_KEY=<live secret>
   ```
3. Restart bot.
4. Dashboard banner flips to red 🚨 LIVE TRADING.

**Robinhood live path:**
1. `.env`:
   ```
   BROKER=robinhood
   TRADING_MODE=live
   ROBINHOOD_USERNAME=your_email
   ROBINHOOD_PASSWORD=your_password
   ROBINHOOD_MFA_CODE=<TOTP secret, not a one-time code>
   ```
2. Restart bot.

**Caveats for live:**
- **Paper-test first.** At least one month of paper execution on the same watchlist and regime before touching live.
- **Robinhood has no paper mode.** First fill is real money. Start with single-leg long options, small size.
- **PDT rule:** if account equity < $25k, you're capped at 3 day-trades per 5 rolling business days. Disable intraday or expect throttling.
- **`robin_stocks` is community-maintained.** Pin the version in `requirements.txt` once you have one that works.

---

## 9. After you edit source files

Python caches imported modules in memory. If you edit a `.py` file while `main.py` is running, **the bot keeps using the old code** until you restart. Always Ctrl-C and re-run `main.py` after editing code. Same for the dashboard — Streamlit auto-reloads for most changes, but if something looks stuck, Ctrl-C and restart it.

---

## 10. Troubleshooting

| Symptom | Check |
|---|---|
| `ModuleNotFoundError` on start | You ran with system `python3` instead of `.venv/bin/python`. Use the venv. |
| Bot sleeps forever without trading | Market closed, regime is `crash` (0), or no scores crossed threshold. Check `logs/main.log`. |
| `financialdatasets.ai` 403 errors | Key missing or revoked. Verify `.env` has `FINANCIAL_DATASETS_API_KEY=...` and test via curl. |
| Polygon `NOT_AUTHORIZED` errors | Some endpoints (gainers/losers, full-market snapshot, indices aggregates) are tier-gated. Fallbacks to Alpaca / yfinance run automatically. |
| Lockout file present | `LOCKOUT` file exists (peak drawdown breach). Delete manually after review to resume. |
| Stale behavior after code edit | Ctrl-C the bot, start it again. |
| Dashboard shows 🔴 Stopped but `ps` shows bot running | Heartbeat file went stale. Bot is alive but hasn't cycled yet — wait one cycle. |

---

## 11. Full stop — nothing runs, nothing schedules

No launchd agents, no cron entries, no scheduled triggers. Nothing starts unless you type a command. To confirm:

```bash
ls ~/Library/LaunchAgents/ | grep -i montrai  # should print nothing (macOS)
crontab -l 2>/dev/null | grep -i montrai      # should print nothing
```

If either prints something, it wasn't added by this project. Inspect before removing.
