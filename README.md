# Montrai — AI Trading Platform

> *A regime-aware, options-primary trading bot that reads the market like a trained mind and executes with defined-risk discipline.*

Montrai is a modular trading platform that detects market regimes with Hidden Markov Models, scans a daily-refreshed watchlist for setups, and executes a multi-strategy options book — all while enforcing hard-coded circuit breakers that can't be overridden by signal logic.

## What it does (today)

- **HMM regime detection** on SPY — classifies the market as crash / bear / neutral / bull / euphoria using 21 features across price, volatility, macro (yield curve, VIX term structure), and options positioning (GEX). Retrains weekly.
- **Options-primary execution**:
  - Long calls + long puts (directional, defined risk = premium paid)
  - Vertical credit spreads (bull put credit, bear call credit — harvest IV with capped downside)
  - Iron condor (neutral regime — collect premium on chop)
  - Covered calls (write short calls against held stock)
  - Opening Range Breakout intraday (short-dated ATM options on first-hour breakouts)
  - Paper-only safety valve: forced-top-score fire if no trades fill by EOD (observability, not conviction)
- **Dynamic daily watchlist** — pre-market each day, merges top gainers + losers + most-actives from Alpaca's screener on top of a static base. Filters penny stocks, warrants, and anything with thin ATM options. Defensive regimes (bear/euphoria) narrow to blue chips only.
- **Dual broker support** — Alpaca (paper + live) or Robinhood (live only via community SDK).
- **Real-time dashboard** — Streamlit at `localhost:8501`, paper/live banner always visible, bot status pill (Running/Sleeping/Stopped/Lockout), open positions by asset class, dynamic watchlist, every strategy knob as a toggle.
- **Full test suite** — 52 unit tests covering risk math, signed-qty spread P&L, direction-aware exits, HMM schema stability, and broker OCC-symbol parsing.

## Safety first — circuit breakers

Hard-coded in `risk/risk_manager.py`. Cannot be disabled by AI signal, strategy, or dashboard toggle.

| Circuit Breaker | Trigger | Effect |
|---|---|---|
| Options daily cap | Premium outlay exceeds **$1,000/day** | Trade blocked or trimmed |
| Stock daily cap | Notional exceeds **$5,000/day** | Trade blocked or trimmed |
| Intraday daily cap | Premium exceeds **$500/day** | Separate from swing cap |
| Daily loss halt | Portfolio drops **2%** from day open | Position sizes halved rest of day |
| Peak drawdown lockout | Portfolio drops **10%** from all-time high | Writes `LOCKOUT` file, bot exits, manual restart |
| Per-option stops | Premium ±50% (long) / premium doubled (short credit) | Position closed |
| Intraday force flatten | **15:55 ET** daily | All intraday positions closed regardless of P&L |

## Architecture

```
montrai/
├── config/
│   ├── settings.py              # Static defaults — hard-coded safety limits live here
│   ├── runtime_config.py         # Hot-reloadable overrides (no restart needed)
│   └── runtime.json              # Actual runtime values (gitignored — your knobs)
├── core/
│   ├── market_data.py            # OHLCV + quotes (financial-datasets primary, yfinance fallback)
│   ├── feature_engineering.py    # 21-col HMM feature matrix + swing signal scorer
│   ├── options_data.py           # Alpaca options chain, pick_contract / pick_vertical_spread / pick_iron_condor
│   ├── polygon_client.py         # Polygon REST (indices, intraday bars, options history)
│   ├── financial_datasets.py     # Fundamentals + daily OHLCV via financialdatasets.ai
│   └── position_tracker.py       # BotState (Positions + OptionsPositions + MultiLegPositions)
├── regime/
│   ├── hmm_engine.py             # Gaussian HMM on SPY, 3–7 regimes, BIC selection, stability filter
│   └── strategies.py             # Regime → allocation multiplier + watchlist narrowing
├── risk/
│   └── risk_manager.py           # All circuit breakers + direction-aware exits
├── executor/
│   ├── base.py                   # Abstract broker interface
│   ├── alpaca_broker.py          # Alpaca: stocks, options, multi-leg (MLEG)
│   ├── robinhood_broker.py       # Robinhood via robin_stocks (live only, community SDK)
│   ├── options_strategies.py     # Decision tree: regime × score → long / spread / condor pick
│   └── order_executor.py         # Public facade — never import broker impls directly
├── intraday/
│   └── orb.py                    # Opening Range Breakout strategy (1-7 DTE options)
├── discovery/
│   └── dynamic_watchlist.py      # Pre-market mover + most-actives discovery with liquidity filter
├── backtester/
│   └── walk_forward.py           # Walk-forward train/test, no look-ahead bias
├── dashboard/
│   └── app.py                    # Streamlit — 4 tabs (Live / Options / Settings / Data Sources)
├── scripts/
│   └── polygon_s3_sync.py        # Polygon S3 flat-file ingester (if plan tier includes it)
├── tests/                        # 52 tests, pytest, 1.1s
├── main.py                       # Train → Monitor → Execute orchestrator
└── requirements.txt
```

## Quickstart (for anyone cloning)

### 1. Prerequisites
- Python 3.12+
- An Alpaca account (free paper at https://alpaca.markets) — required for options chain data even if you plan to trade elsewhere
- Optional: Polygon / massive.com, financial-datasets, FRED keys for fuller data stack

### 2. Install
```bash
git clone https://github.com/nanisaladi24/montrai.git
cd montrai
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. Configure
```bash
cp .env.example .env
# Edit .env — at minimum set ALPACA_API_KEY + ALPACA_SECRET_KEY (paper keys)
```

Minimum `.env`:
```
BROKER=alpaca
TRADING_MODE=paper
ALPACA_API_KEY=PK...
ALPACA_SECRET_KEY=...
ALPACA_PAPER=true
```

Full recommended `.env` also sets `FINANCIAL_DATASETS_API_KEY`, `POLYGON_API_KEY`, and the FRED key (via dashboard or `config/runtime.json`).

### 4. Run tests
```bash
.venv/bin/python -m pytest tests/ -v
```
Expect 52/52 green in ~1 second.

### 5. Start bot + dashboard (separate terminals)
```bash
# Terminal A — trading bot
.venv/bin/python main.py

# Terminal B — dashboard
STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
  .venv/bin/streamlit run dashboard/app.py --server.port 8501
```

Dashboard: http://localhost:8501 — big banner confirms 📄 PAPER or 🚨 LIVE.

### 6. Enable strategies (from dashboard Settings tab)

Everything is OFF by default. Flip toggles when ready:

- `options_trading_enabled` (on by default) — long calls / long puts
- `intraday_enabled` — Opening Range Breakout (minute-bar cycles during market hours)
- `spreads_enabled` — vertical credit spreads
- `iron_condor_enabled` — neutral-regime iron condors
- `covered_call_enabled` — write calls against held shares
- `stock_trading_enabled` (off by default) — swing stock trades
- `paper_force_top_score` — paper-only EOD observability fire

See [`RUN.md`](RUN.md) for day-to-day operating instructions.

## Data stack

| Source | Used for | Status |
|---|---|---|
| **Alpaca** | Options chain + Greeks + execution | Required |
| **financial-datasets** | Equity/ETF OHLCV + fundamentals | Recommended (has free tier) |
| **Polygon / massive.com** | VIX/VVIX indices, intraday minute bars, options history | Recommended (has free tier) |
| **FRED** | Yield curve, fed funds, HY credit spread | Recommended (free) |
| **yfinance** | Fallback for indices + DXY | Always on (no key needed) |

See [`DATA_SOURCES.md`](DATA_SOURCES.md) for signup links, costs, and what each source unlocks.

## Strategy decision tree

```
Score / regime / score_magnitude
  ├─ BULLISH regime (bull / neutral / euphoria):
  │    ├─ score ≥ +0.6    → long_call (if spreads off) OR bull_put_credit (if spreads on)
  │    └─ +0.4 ≤ score < +0.6 → long_call (threshold-gated)
  ├─ BEARISH regime (crash / bear / neutral):
  │    ├─ score ≤ −0.6    → long_put (if spreads off) OR bear_call_credit (if spreads on)
  │    └─ −0.6 < score ≤ −0.4 → long_put (threshold-gated)
  ├─ NEUTRAL + |score| < 0.3 + iron_condor_enabled → iron_condor
  └─ Nothing above fires → wait
```

Regime allocation multiplier: crash 0× (no trades), bear 0.3×, neutral 0.6×, bull 1.0×, euphoria 0.7×.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
# 52 passed in 1.14s
```

Coverage highlights:
- Signed-qty math for long + short options (debit + credit spreads)
- Direction-aware exit rules (credit TP at 50% decay; debit TP at +50% on debit)
- Iron condor position math (width, max profit, max loss)
- HMM schema stability (21 cols always, even when FRED/GEX fail)
- BotState JSON roundtrip with all position types
- Dynamic watchlist filtering (rejects penny stocks, warrants, dedups sources)

## Roadmap

- [x] HMM regime detection (21 features, BIC-selected 3-7 regimes)
- [x] Options-primary execution on Alpaca + Robinhood
- [x] Multi-leg spreads (credit + debit) + iron condor
- [x] Covered calls with strict cap enforcement
- [x] Opening Range Breakout intraday (1-7 DTE, HMM-filtered)
- [x] Dynamic daily watchlist (movers + most-actives, liquidity-filtered)
- [x] Paper/live mode banner + bot heartbeat
- [x] Full test suite (52 tests)
- [ ] Historical options backtesting (Polygon S3 flat files — gated by plan tier)
- [ ] Sector rotation + pairs trading strategies
- [ ] Reinforcement learning for position sizing
- [ ] WebSocket streaming for sub-minute intraday reactions
- [ ] REST API + web UI (multi-user SaaS direction)

## Design principles

- **Safety first.** Circuit breakers are stateless, hard-coded, and evaluated before every trade decision.
- **Modular by design.** Brokers, strategies, data sources are independent. Swapping one doesn't require touching others.
- **No look-ahead bias.** Backtester trains only on data available at decision time.
- **Paper before live.** Default mode is paper. Going live is an intentional env-variable flip with a red banner warning.
- **Audit trail always.** Every trade, circuit breaker trigger, and regime change is logged. `logs/` + `logs/trades.csv` are source of truth.

## License

To be determined — Montrai may become open source or a commercial product. All rights reserved for now.

---

*Built with Claude Code. Regime detection architecture inspired by Hidden Markov Model research in quantitative finance.*
