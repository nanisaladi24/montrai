# Running Montrai Manually

Everything here is run by hand — no cron, no launchd, nothing auto-starting at login. You are always the one who decides when it runs.

---

## 1. First-time / one-time setup

Only do these once. Skip on daily runs.

```bash
cd /Users/nanisaladi/Nani/Projects/AI_trading/montrai

# Create venv + install deps (only if .venv/ doesn't exist)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Confirm .env has all required keys
cat .env
# Must have: BROKER, TRADING_MODE, ALPACA_API_KEY, ALPACA_SECRET_KEY,
#            FINANCIAL_DATASETS_API_KEY
```

---

## 2. Daily run — two terminals

### Terminal A — trading bot

```bash
cd /Users/nanisaladi/Nani/Projects/AI_trading/montrai
.venv/bin/python main.py
```

What it does:
- Loads saved state from `bot_state.json` (any open paper positions resume).
- If market is closed, sleeps 5 min and re-checks.
- During market hours (8:30 AM – 3:00 PM CT): scans watchlist, places paper orders via Alpaca, enforces stop-loss / take-profit each 60-min cycle.

Leave the terminal open. The bot owns that terminal. Don't close it.

### Terminal B — dashboard

```bash
cd /Users/nanisaladi/Nani/Projects/AI_trading/montrai
.venv/bin/streamlit run dashboard/app.py
```

Streamlit will print a URL — usually `http://localhost:8501`. Open it in a browser. The dashboard reads the same state/log files the bot writes, so as the bot works the session the dashboard updates live.

The dashboard is independent of the bot. You can start/stop it whenever.

---

## 3. Stopping cleanly

In either terminal: **Ctrl-C**.

The bot's Ctrl-C handler saves state to `bot_state.json` before exiting. Positions remain open in Alpaca's paper account — they'll be picked up again on next start.

**Rule of thumb:** if you hold open positions and the market is open, the bot should be running. Stops are evaluated in-process every cycle, not as bracket orders at Alpaca, so a stopped bot = no active stop-loss protection until you restart.

Safe to stop: after 3:00 PM CT close, or any time you have no open positions.

---

## 4. One-shot commands (don't need the main loop)

```bash
cd /Users/nanisaladi/Nani/Projects/AI_trading/montrai

# Rank the whole watchlist right now, no orders placed
.venv/bin/python main.py --scan

# Retrain the HMM regime model
.venv/bin/python main.py --train

# Walk-forward backtest, single symbol
.venv/bin/python main.py --backtest --symbol AAPL

# Walk-forward backtest, entire watchlist (slow)
.venv/bin/python main.py --backtest-all
```

---

## 5. Checking what's running

```bash
# Is the bot process alive?
ps aux | grep "main.py" | grep -v grep

# Is the dashboard alive?
ps aux | grep "streamlit run dashboard" | grep -v grep
```

If a process appears in the output → running. If not → stopped.

---

## 6. Reading logs

All logs live in `montrai/logs/`.

```bash
# The main bot log (regime, signals, orders, errors)
tail -f logs/main.log

# Broker interaction log
tail -f logs/executor.log

# Risk-manager decisions (stops, blocks, lockouts)
tail -f logs/risk_manager.log

# Fundamental data calls (REST API responses, cache hits)
tail -f logs/financial_datasets.log
```

Press Ctrl-C to exit `tail -f` — it doesn't stop the bot, just the tail.

State and trade history:

```bash
cat bot_state.json             # current positions, daily spend, peak equity
cat logs/trades.csv            # append-only trade log (open this in a spreadsheet too)
```

---

## 7. After you edit source files

Python caches imported modules in memory. If you edit a `.py` file while `main.py` is running, **the bot keeps using the old code** until you restart. Always Ctrl-C and re-run `main.py` after editing code. Same for the dashboard — Streamlit does auto-reload for most changes, but if something looks stuck, Ctrl-C and restart it.

---

## 8. Troubleshooting

| Symptom | Check |
|---|---|
| `ModuleNotFoundError` on start | You ran with system `python3` instead of `.venv/bin/python`. Use the venv. |
| Bot sleeps forever without trading | Market closed, regime is `crash` (0), or `can_open_new_position` returned false (max positions or allocation rule). Check `logs/main.log`. |
| `financialdatasets.ai` 403 errors | Key missing or revoked. Verify `.env` has `FINANCIAL_DATASETS_API_KEY=...` and test with a quick curl: `curl -H "X-API-KEY: $key" "https://api.financialdatasets.ai/financials/income-statements?ticker=AAPL&period=annual&limit=1"` |
| Lockout file present | `montrai/LOCKOUT` exists (peak drawdown breach). Delete manually after review to resume. |
| Stale behavior after code edit | Ctrl-C the bot, start it again. |

---

## 9. Full stop — nothing runs, nothing schedules

You are here already. No launchd agents, no cron entries, no scheduled triggers. Nothing starts unless you type a command. To confirm:

```bash
ls ~/Library/LaunchAgents/ | grep -i montrai  # should print nothing
crontab -l 2>/dev/null | grep -i montrai      # should print nothing
```

If either prints something, tell me and I'll remove it.
