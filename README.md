# Montrai — AI Trading Platform

> *An intelligent, regime-aware trading platform built for scale.*

Montrai is a modular AI trading platform that starts with automated swing trading and is designed to grow into a full-spectrum trading ecosystem — spanning multiple strategies, asset classes, risk tiers, and eventually a multi-user SaaS product.

The name **Montrai** reflects the vision: a system that reads the market like a trained mind and executes with precision.

---

## Current State: Swing Trader (v0.1)

The first module is a fully automated swing trading engine connected to Robinhood. It detects market regimes using Hidden Markov Models, sizes positions dynamically, and protects capital through hard-coded circuit breakers.

### What it does today
- Detects market regimes (crash / bear / neutral / bull / euphoria) using a HMM trained on SPY
- Scans a configurable watchlist for swing trade setups (RSI, MACD, Bollinger Bands, ATR)
- Executes fractional orders via the Robinhood API
- Enforces strict safety rules independently of AI signal logic
- Walk-forward backtests strategies without look-ahead bias
- Visualizes everything in a live Streamlit dashboard

---

## Safety & Circuit Breakers

These are hard-coded in `risk/risk_manager.py` and cannot be overridden by any AI signal or strategy layer.

| Circuit Breaker | Trigger | Effect |
|---|---|---|
| Daily spend cap | Cumulative buys exceed **$500/day** | Trade blocked or trimmed |
| Daily loss halt | Portfolio drops **2%** from day open | Position sizes halved for remainder of day |
| Peak drawdown lockout | Portfolio drops **10%** from all-time high | Creates `LOCKOUT` file, bot exits, requires manual restart |
| Per-position stop-loss | Price falls **5%** from entry | Position closed immediately |
| Per-position take-profit | Price rises **12%** from entry | Position closed, gains locked |

**To restart after a drawdown lockout:** investigate the cause, then `rm LOCKOUT` before restarting.

---

## Project Structure

```
montrai/
├── config/
│   └── settings.py          # All tunable constants — start here
├── core/
│   ├── market_data.py        # Historical + live price feeds (yfinance)
│   ├── feature_engineering.py # Technical indicators + swing signal scorer
│   └── position_tracker.py  # Persistent state: positions, daily counters, P&L
├── regime/
│   ├── hmm_engine.py         # HMM: trains on SPY, auto-selects 3–7 regimes
│   └── strategies.py         # Regime → allocation multiplier + watchlist mapping
├── risk/
│   └── risk_manager.py       # Circuit breakers (stateless, regime-independent)
├── executor/
│   └── order_executor.py     # Robinhood API wrapper (paper + live modes)
├── backtester/
│   └── walk_forward.py       # Walk-forward engine with rolling train/test windows
├── dashboard/
│   └── app.py                # Streamlit real-time dashboard
├── monitoring/
│   └── logger.py             # Structured logging + trade CSV
├── tests/
│   ├── test_risk_manager.py
│   ├── test_feature_engineering.py
│   └── test_position_tracker.py
├── main.py                   # Orchestrator: Train → Monitor → Execute loop
├── requirements.txt
└── .env.example
```

---

## Quick Start

### 1. Install

```bash
cd montrai
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env`:
```
ROBINHOOD_USERNAME=your_email@example.com
ROBINHOOD_PASSWORD=your_password
TRADING_MODE=paper        # keep this as paper until you're confident
```

### 3. Run a backtest

Always backtest before running live.

```bash
python main.py --backtest --symbol AAPL
python main.py --backtest --symbol SPY
```

The walk-forward engine trains on rolling 252-day windows and tests on the following 63 days. No look-ahead bias.

### 4. Launch the dashboard

```bash
streamlit run dashboard/app.py
```

Opens at `http://localhost:8501`. Shows open positions, circuit breaker status, trade history, and regime legend.

### 5. Start the bot (paper mode)

```bash
python main.py
```

The bot runs an hourly cycle: detect regime → scan for exits → scan for entries. It retrains the HMM every 7 days automatically.

### 6. Go live (when ready)

Set `TRADING_MODE=live` in `.env`. Run in paper mode for **at least one month** first.

---

## Regime System

The HMM is trained on SPY's daily returns, realized volatility, ATR, Bollinger Band position, and volume ratio. It auto-selects the optimal number of regimes (3–7) using BIC scoring, and a stability filter requires N consecutive bars in the same regime before a flip is confirmed.

| Regime | Name | Allocation Multiplier | Behavior |
|---|---|---|---|
| 0 | Crash | 0.0 | No new trades |
| 1 | Bear | 0.3 | Defensive only (index ETFs, financials) |
| 2 | Neutral | 0.6 | Quality large-caps |
| 3 | Bull | 1.0 | Full universe, full size |
| 4 | Euphoria | 0.7 | Trim back (overheated market) |

---

## Configuration Reference

All settings live in `config/settings.py`. Key ones:

| Setting | Default | Description |
|---|---|---|
| `MAX_DAILY_SPEND_USD` | `500.0` | Hard cap on buy-side dollars per day |
| `MAX_POSITION_SIZE_PCT` | `0.15` | Max 15% of portfolio per position |
| `MAX_OPEN_POSITIONS` | `8` | Max concurrent swing trades |
| `STOP_LOSS_PCT` | `0.05` | 5% hard stop per position |
| `TAKE_PROFIT_PCT` | `0.12` | 12% take-profit target |
| `DAILY_LOSS_HALT_PCT` | `0.02` | 2% daily loss triggers size halving |
| `PEAK_DRAWDOWN_LOCKOUT_PCT` | `0.10` | 10% drawdown from peak → full lockout |
| `MIN_HOLD_DAYS` / `MAX_HOLD_DAYS` | `2` / `14` | Swing trade holding window |
| `WATCHLIST` | 13 symbols | Universe of tradeable instruments |

---

## Running Tests

```bash
python -m pytest tests/ -v
```

All 15 tests cover risk manager circuit breakers, feature engineering outputs, and position tracker state management.

---

## Roadmap

Montrai is version 0.1. Below is the intended expansion path toward a full AI trading platform.

### Phase 1 — Swing Trader (current)
- [x] HMM regime detection
- [x] Swing trade signal engine (RSI, MACD, BB, ATR)
- [x] Robinhood execution + paper mode
- [x] Circuit breakers + daily spend cap
- [x] Walk-forward backtester
- [x] Streamlit dashboard
- [ ] Email/webhook alerts on trades and circuit breaker triggers
- [ ] Multi-symbol backtesting with portfolio-level metrics (Sharpe, max drawdown)
- [ ] Trailing stop-loss implementation

### Phase 2 — Strategy Layer Expansion
- [ ] Momentum strategy module (trend-following, moving average crossovers)
- [ ] Mean reversion module (pairs trading, statistical arbitrage)
- [ ] Options strategy layer (covered calls, cash-secured puts for income)
- [ ] Crypto module (BTC, ETH, SOL via Robinhood crypto)
- [ ] Strategy selector: let the regime dictate which strategy module is active

### Phase 3 — Intelligence Upgrades
- [ ] LLM-powered news sentiment scoring per symbol (earnings, macro events)
- [ ] Earnings calendar awareness (avoid holding through earnings by default)
- [ ] Alternative data feeds (options flow, short interest, institutional filings)
- [ ] Reinforcement learning policy for position sizing (replace Kelly approximation)
- [ ] Self-evaluation: bot reviews its own closed trades and updates confidence priors

### Phase 4 — Infrastructure & Reliability
- [ ] Multi-broker support (Alpaca, Interactive Brokers, Schwab)
- [ ] PostgreSQL trade ledger (replace CSV)
- [ ] Scheduled retraining pipeline (cron or cloud task)
- [ ] Real-time WebSocket data feeds for intraday strategies
- [ ] Docker containerization + deployment to cloud (AWS/GCP)
- [ ] Monitoring and alerting via Grafana + PagerDuty

### Phase 5 — Platform (Open Source or SaaS)
- [ ] REST API layer exposing signals, positions, and performance metrics
- [ ] User authentication + per-user portfolio isolation
- [ ] Strategy marketplace: plug-in custom strategy modules
- [ ] Backtesting-as-a-service endpoint
- [ ] Web UI (React/Next.js) replacing Streamlit dashboard
- [ ] Subscription tiers: free (paper only), pro (live trading), enterprise (custom strategies)
- [ ] Open-source core with premium strategy modules

---

## Design Principles

**Safety first.** Circuit breakers are stateless, hard-coded, and run before every trade decision. They can never be disabled by strategy or AI logic.

**Modular by design.** Each layer (data, signals, regime, risk, execution) is independent. Swapping brokers or strategies does not require touching other modules.

**No look-ahead bias.** The backtester trains only on data available at decision time. Walk-forward validation is the minimum bar; any new strategy must pass it.

**Paper before live.** The default mode is paper trading. Going live is an intentional, explicit act.

**Audit trail always.** Every trade, every circuit breaker trigger, every regime change is logged. `logs/` and `logs/trades.csv` are the source of truth.

---

## License

To be determined — Montrai may become open source or a commercial product. All rights reserved for now.

---

*Built with Claude Code. Regime detection architecture inspired by Hidden Markov Model research in quantitative finance.*
