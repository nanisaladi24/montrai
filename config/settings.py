from typing import Dict, List
import os
from dotenv import load_dotenv

load_dotenv()

# ── Safety Limits (hard-coded, never override via AI) ──────────────────────────
MAX_DAILY_SPEND_USD = 500.0          # Max buy-side dollars transacted per day
MAX_POSITION_SIZE_PCT = 0.15         # Max 15% of portfolio in one stock
MAX_OPEN_POSITIONS = 8               # Cap concurrent swing trades
DAILY_LOSS_HALT_PCT = 0.02           # 2% daily loss → halve sizes
PEAK_DRAWDOWN_LOCKOUT_PCT = 0.10     # 10% drawdown from peak → full stop
STOP_LOSS_PCT = 0.05                 # 5% hard stop-loss per position
TAKE_PROFIT_PCT = 0.12               # 12% take-profit target per position

# ── Swing Trading Timeframe ────────────────────────────────────────────────────
MIN_HOLD_DAYS = 2
MAX_HOLD_DAYS = 14
SIGNAL_INTERVAL_MINUTES = 60        # Re-evaluate every hour during market hours

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
