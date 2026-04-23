from typing import Dict, List
import os
from dotenv import load_dotenv

load_dotenv()

# ── Trade Mode Switches ────────────────────────────────────────────────────────
# Options is the primary execution path. Stocks are off by default and require
# an explicit knob flip in runtime.json or the dashboard Settings tab.
STOCK_TRADING_ENABLED   = os.getenv("STOCK_TRADING_ENABLED", "false").lower() == "true"
OPTIONS_TRADING_ENABLED = os.getenv("OPTIONS_TRADING_ENABLED", "true").lower() == "true"
INTRADAY_ENABLED        = os.getenv("INTRADAY_ENABLED", "false").lower() == "true"

# ── Safety Limits ──────────────────────────────────────────────────────────────
# Two independent daily caps, one per asset class. Each is enforced on its own
# counter in BotState so options risk is isolated from stock risk.
OPTIONS_MAX_DAILY_USD = 1000.0       # Per-day premium spend cap on options
STOCK_MAX_DAILY_USD   = 5000.0       # Per-day notional cap on stock buys (can raise)
MAX_DAILY_SPEND_USD   = 1000.0       # Legacy combined cap; kept for back-compat
MAX_POSITION_SIZE_PCT = 0.15         # Max 15% of portfolio in one stock
MAX_OPEN_POSITIONS = 8               # Cap concurrent swing trades
DAILY_LOSS_HALT_PCT = 0.02           # 2% daily loss → halve sizes
PEAK_DRAWDOWN_LOCKOUT_PCT = 0.10     # 10% drawdown from peak → full stop
STOP_LOSS_PCT = 0.05                 # 5% hard stop-loss per position
TAKE_PROFIT_PCT = 0.12               # 12% take-profit target per position

# ── Options Exit Rules (% of premium paid) ─────────────────────────────────────
OPTIONS_TAKE_PROFIT_PCT = 0.50       # +50% on premium → close long / -50% decay to close short
OPTIONS_STOP_LOSS_PCT   = 0.50       # -50% on premium → close long / 2× premium to close short
OPTIONS_MIN_DTE_EXIT    = 7          # Close when days-to-expiry drops below this
OPTIONS_TARGET_DTE      = (30, 45)   # Preferred expiry window at entry
OPTIONS_TARGET_DELTA    = 0.40       # Target absolute delta for long-call/long-put strike

# ── Dynamic Daily Watchlist ───────────────────────────────────────────────────
# Pre-market discovery of top movers via Alpaca screener. Filtered for options
# liquidity (ATM OI ≥ threshold). Merges with the static watchlist each day.
DYNAMIC_WATCHLIST_ENABLED   = True
DYNAMIC_WATCHLIST_LIMIT     = 20      # max additions per day on top of base
DYNAMIC_WATCHLIST_MIN_PRICE = 5.0     # skip penny stocks (options are usually thin)
DYNAMIC_WATCHLIST_MIN_OI    = 500     # ATM OI threshold at 30-45 DTE
DYNAMIC_WATCHLIST_REFRESH_HOUR_ET = 9  # refresh at 09:00 ET (before market open)

# ── Signal Thresholds ─────────────────────────────────────────────────────────
# Score gates for entry. Dropped from ±0.6 to ±0.4 for more firing opportunity;
# still filtered, just less strict. Raise back to 0.6 for conservative mode.
SIGNAL_SCORE_THRESHOLD_LONG  =  0.4
SIGNAL_SCORE_THRESHOLD_SHORT = -0.4

# Paper-only safety valve: if no trade has fired all day by 15:30 ET, open a
# minimum-size position on the top-|score| symbol so the execution pipeline is
# actually exercised. Skipped automatically in live mode.
PAPER_FORCE_TOP_SCORE = False
PAPER_FORCE_AFTER_HOUR_ET = 15       # Hour (ET) after which the force path activates
PAPER_FORCE_AFTER_MIN_ET  = 30

# ── Intraday (Opening Range Breakout) ─────────────────────────────────────────
INTRADAY_OPENING_RANGE_MIN    = 15   # Define range from 9:30-9:45 ET
INTRADAY_FORCE_CLOSE_HOUR_ET  = 15   # Flatten intraday books at 15:55 ET
INTRADAY_FORCE_CLOSE_MIN_ET   = 55
INTRADAY_SCAN_INTERVAL_MIN    = 5    # Override swing's 30-min cycle when intraday on
INTRADAY_MAX_DAILY_USD        = 500.0
INTRADAY_TARGET_DTE           = (1, 7)    # Short-dated options; high gamma per dollar
INTRADAY_TARGET_DELTA         = 0.50      # ATM-ish for fast move capture
INTRADAY_STOP_LOSS_PCT        = 0.30      # Tighter stops than swing — no room for reversal
INTRADAY_TAKE_PROFIT_PCT      = 0.40
INTRADAY_MIN_ORB_WIDTH_PCT    = 0.003     # 0.3% minimum opening-range width (skip dead tape)

# ── Multi-leg Spreads + Iron Condor ────────────────────────────────────────────
# Phase 2: defined-risk vertical spreads and iron condors. Off by default —
# flip via runtime.json or dashboard to activate.
SPREADS_ENABLED          = os.getenv("SPREADS_ENABLED", "false").lower() == "true"
IRON_CONDOR_ENABLED      = os.getenv("IRON_CONDOR_ENABLED", "false").lower() == "true"
SPREAD_TARGET_SHORT_DELTA = 0.30    # Short leg of credit spread sits ~30Δ (typical)
SPREAD_WING_WIDTH        = 5.0      # Dollar distance between short and long legs
SPREAD_TAKE_PROFIT_PCT   = 0.50     # Close at 50% of max profit
SPREAD_STOP_LOSS_PCT     = 0.50     # Close at 50% loss (of debit) / 2× credit received
SPREAD_MIN_CREDIT        = 0.20     # Skip if credit < $0.20 per spread (not worth slippage)
IRON_CONDOR_SHORT_DELTA  = 0.15     # Wider OTM for condors — lower assignment risk
IRON_CONDOR_WING_WIDTH   = 5.0

# ── Covered Calls ──────────────────────────────────────────────────────────────
# Writes short calls against 100-share lots of underlyings on the watchlist.
# Disabled by default because the $5000 stock cap can't buy 100 shares of most
# watchlist names. Enable + raise stock_max_daily_usd to activate acquisition.
COVERED_CALL_ENABLED       = os.getenv("COVERED_CALL_ENABLED", "false").lower() == "true"
COVERED_CALL_TARGET_DELTA  = 0.25    # OTM — low-Δ call so assignment risk stays low
COVERED_CALL_TARGET_DTE    = (30, 45)
COVERED_CALL_AUTO_ACQUIRE  = False   # If True, buy 100 shares when none held and cap allows

# ── Swing Trading Timeframe ────────────────────────────────────────────────────
MIN_HOLD_DAYS = 2
MAX_HOLD_DAYS = 14
SIGNAL_INTERVAL_MINUTES = 30        # Re-evaluate every 30 minutes during market hours

# ── Regime Detection ──────────────────────────────────────────────────────────
HMM_N_REGIMES_RANGE = (3, 7)        # Test 3–7 regimes, pick best BIC
HMM_LOOKBACK_DAYS = 504             # ~2 years of daily data for training
HMM_STABILITY_WINDOW = 3            # Require N consecutive bars to confirm regime flip
REGIME_NAMES = {
    0: "crash",
    1: "bear",
    2: "neutral",
    3: "bull",
    4: "euphoria",
}

# ── Regime → Allocation multiplier (applied to base position size) ─────────────
REGIME_ALLOCATION: Dict[int, float] = {
    0: 0.0,    # crash: flat, no new trades
    1: 0.3,    # bear: very small
    2: 0.6,    # neutral: moderate
    3: 1.0,    # bull: full size
    4: 0.7,    # euphoria: reduce (overheated)
}

# ── Swing Trade Universe ───────────────────────────────────────────────────────
WATCHLIST: List[str] = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
    "META", "TSLA", "AMD", "JPM", "BAC",
    "SPY", "QQQ", "IWM",
]

# ── Extended Hours ────────────────────────────────────────────────────────────
# Off by default — flip to True to enable pre-market (4–9:30 ET) and
# after-hours (16–20 ET) trading. Alpaca requires limit orders outside
# regular hours; EXTENDED_HOURS_LIMIT_OFFSET_PCT sets how aggressively
# the limit price chases the last quote (buys slightly above, sells slightly below).
EXTENDED_HOURS_ENABLED: bool = False
EXTENDED_HOURS_LIMIT_OFFSET_PCT: float = 0.001   # 0.1% offset from last price

# ── Broker ─────────────────────────────────────────────────────────────────────
# "alpaca"     → Alpaca API (paper or live); recommended for all testing
# "robinhood"  → Robinhood via robin_stocks; live trading only
BROKER = os.getenv("BROKER", "alpaca").lower()
TRADING_MODE = os.getenv("TRADING_MODE", "paper")   # "paper" or "live"

# ── Alpaca ─────────────────────────────────────────────────────────────────────
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"
# Paper endpoint base URL — used for reference/logging only; SDK reads ALPACA_PAPER flag
ALPACA_BASE_URL = (
    "https://paper-api.alpaca.markets"
    if ALPACA_PAPER
    else "https://api.alpaca.markets"
)

# ── Robinhood (live only) ──────────────────────────────────────────────────────
RH_USERNAME = os.getenv("ROBINHOOD_USERNAME", "")
RH_PASSWORD = os.getenv("ROBINHOOD_PASSWORD", "")
RH_MFA_CODE = os.getenv("ROBINHOOD_MFA_CODE", "")

# ── Financial Datasets (financialdatasets.ai) ──────────────────────────────────
# Free tier: 250 req/month. Set key to enable fundamental overlay in swing signals.
# Get key at: https://financialdatasets.ai
FINANCIAL_DATASETS_API_KEY = os.getenv("FINANCIAL_DATASETS_API_KEY", "")

# ── Logging / Monitoring ───────────────────────────────────────────────────────
LOG_DIR = "logs"
TRADE_LOG_FILE = f"{LOG_DIR}/trades.csv"
STATE_FILE = "bot_state.json"
LOCKOUT_FILE = "LOCKOUT"            # Presence of this file halts the bot

# ── Backtester ─────────────────────────────────────────────────────────────────
BACKTEST_TRAIN_DAYS = 252           # Training window per fold
BACKTEST_TEST_DAYS = 63             # Test window per fold (~1 quarter)

# ── Dashboard ─────────────────────────────────────────────────────────────────
DASHBOARD_REFRESH_SECONDS = 30
