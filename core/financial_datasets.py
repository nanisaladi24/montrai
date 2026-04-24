"""
Financial Datasets client — fundamentals overlay for the swing signal.

Data source priority:
  1. REST API    — direct HTTP to api.financialdatasets.ai using
                   FINANCIAL_DATASETS_API_KEY (loaded from .env). Primary path.
  2. Local cache — data/fundamentals_cache.json (weekly snapshot).

The bot MUST NOT call the Claude CLI or any MCP server — it burns the user's
Claude plan tokens silently. The `_claude_fetch` helper is hard-disabled below.
Do not re-enable it without explicit written authorization from the user.
"""
import os
import json
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from monitoring.logger import get_logger

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = get_logger("financial_datasets")

_BASE        = "https://api.financialdatasets.ai"
_CACHE_PATH  = Path(__file__).parent.parent / "data" / "fundamentals_cache.json"
_cache: Optional[dict] = None


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(_CACHE_PATH) as f:
            _cache = json.load(f)
        age_days = (datetime.utcnow() - datetime.fromisoformat(
            _cache.get("fetched_at", "2000-01-01T00:00:00").replace("Z", "")
        )).days
        if age_days > 7:
            logger.warning(f"Fundamentals cache is {age_days} days old. Run --refresh-fundamentals.")
        return _cache
    except Exception:
        return {}


def _cache_symbol(ticker: str) -> dict:
    return _load_cache().get("symbols", {}).get(ticker, {})


# ── Claude CLI / MCP helpers ──────────────────────────────────────────────────

def _claude_fetch(tool: str, ticker: str, extra_params: str = "") -> Optional[dict]:
    """
    HARD-DISABLED. The bot must never spawn `claude -p` — it consumes the
    user's Claude plan tokens without any in-process visibility. All callers
    of this function fall through to the local cache on None, which is the
    intended behavior.

    Do not re-enable this without explicit written authorization from the
    user. See module docstring.
    """
    return None


# ── Optional REST API helpers ──────────────────────────────────────────────────

def _api_key() -> str:
    key = os.getenv("FINANCIAL_DATASETS_API_KEY", "")
    if key:
        return key
    try:
        import config.runtime_config as _rc
        return _rc.load().get("data_sources", {}).get("financial_datasets_api_key", "")
    except Exception:
        return ""


def _get(path: str, params: dict):
    api_key = _api_key()
    if not api_key:
        return None
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{_BASE}{path}?{qs}"
    # User-Agent is required: the API's Cloudflare layer returns 403/1010
    # for the default `Python-urllib/*` UA.
    req = urllib.request.Request(
        url,
        headers={"X-API-KEY": api_key, "User-Agent": "montrai/0.1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.debug(f"financial_datasets REST {path}: {e}")
        return None


# ── Public fetch functions (cache-first) ──────────────────────────────────────

def get_metrics_snapshot(ticker: str) -> dict:
    # 1. REST API — response shape: {"snapshot": {...metrics...}}
    data = _get("/financial-metrics/snapshot", {"ticker": ticker})
    snap = (data or {}).get("snapshot") if isinstance(data, dict) else None
    if isinstance(snap, dict) and "price_to_earnings_ratio" in snap:
        return snap
    # 2. Claude CLI via MCP (fallback when no API key) — returns flat dict
    data = _claude_fetch("get_financial_metrics_snapshot", ticker)
    if data and isinstance(data, dict) and "price_to_earnings_ratio" in data:
        return data
    # 3. Cache
    return _cache_symbol(ticker).get("metrics", {})


def get_earnings(ticker: str) -> dict:
    # 1. REST API — response shape: {"earnings": {"quarterly": {...}, ...}}
    data = _get("/earnings", {"ticker": ticker})
    inner = (data or {}).get("earnings") if isinstance(data, dict) else None
    if isinstance(inner, dict) and inner.get("quarterly"):
        return inner
    # 2. Claude CLI via MCP (fallback) — returns already-unwrapped dict
    data = _claude_fetch("get_earnings", ticker)
    if data and isinstance(data, dict) and data.get("quarterly"):
        return data
    # 3. Cache (flat shape → wrap in quarterly key)
    cached = _cache_symbol(ticker).get("earnings", {})
    if cached and "eps_surprise" in cached:
        return {"quarterly": cached}
    return {}


def get_analyst_estimates(ticker: str, limit: int = 4) -> list[dict]:
    # 1. REST API
    data = _get("/analyst-estimates", {"ticker": ticker, "period": "quarterly", "limit": limit})
    if data:
        return data if isinstance(data, list) else (data.get("analyst_estimates") or [])
    # 2. Claude CLI via MCP (fallback)
    data = _claude_fetch("get_analyst_estimates", ticker,
                         extra_params=f" with period=quarterly limit={limit}")
    if data:
        return data if isinstance(data, list) else (data.get("analyst_estimates") or [])
    return []


def get_insider_trades(ticker: str, limit: int = 30) -> list[dict]:
    # 1. REST API
    data = _get("/insider-trades", {"ticker": ticker, "limit": limit})
    if data:
        return data if isinstance(data, list) else (data.get("insider_trades") or [])
    # 2. Claude CLI via MCP (fallback)
    data = _claude_fetch("get_insider_trades", ticker, extra_params=f" limit={limit}")
    if data:
        return data if isinstance(data, list) else (data.get("insider_trades") or [])
    return []


# ── Composite fundamental score ────────────────────────────────────────────────

def fundamental_score(ticker: str) -> dict:
    """
    Compute −1.0 to +1.0 fundamental score from:
      • Valuation  — P/E, P/B              (±0.25)
      • Quality    — net margin, ROE, D/E  (±0.25)
      • Earnings   — beat/miss vs estimate (±0.10)
      • Estimates  — forward EPS trend     (±0.10)
      • Insiders   — open-market P vs S    (±0.10)

    ETFs (SPY/QQQ/IWM) return score 0 — no fundamental bias.
    Gracefully returns score 0 when data is unavailable.
    """
    score = 0.0
    details: dict = {}

    # ── 1. Valuation & quality ─────────────────────────────────────────────────
    metrics = get_metrics_snapshot(ticker)
    if metrics:
        pe  = metrics.get("price_to_earnings_ratio")
        pb  = metrics.get("price_to_book_ratio")
        margin = metrics.get("net_margin") or 0
        roe    = metrics.get("return_on_equity") or 0
        de     = metrics.get("debt_to_equity") or 0

        val_score = 0.0
        if pe is not None:
            if 0 < pe < 20:
                val_score += 0.15
            elif pe > 50:
                val_score -= 0.10
        if pb is not None:
            if 0 < pb < 3:
                val_score += 0.10
            elif pb > 15:
                val_score -= 0.05

        qual_score = 0.0
        if margin > 0.15:
            qual_score += 0.10
        elif margin < 0:
            qual_score -= 0.15
        if roe > 0.15:
            qual_score += 0.10
        elif roe < 0:
            qual_score -= 0.10
        if 0 < de < 0.5:
            qual_score += 0.05
        elif de > 2.0:
            qual_score -= 0.10

        score += val_score + qual_score
        details.update({
            "pe": pe, "pb": pb,
            "margin": round(margin, 3), "roe": round(roe, 3), "debt_equity": round(de, 3),
            "valuation_score": round(val_score, 3),
            "quality_score": round(qual_score, 3),
        })

    # ── 2. Most recent earnings beat/miss ──────────────────────────────────────
    earnings = get_earnings(ticker)
    quarterly = earnings.get("quarterly", {}) if earnings else {}
    if quarterly:
        surprise = (quarterly.get("eps_surprise") or "").upper()
        s_score = 0.10 if surprise == "BEAT" else (-0.10 if surprise == "MISS" else 0.0)
        score += s_score
        details["eps_surprise"] = surprise or "unknown"
        details["surprise_score"] = s_score

    # ── 3. Forward EPS trajectory (REST API only — skipped on cache) ───────────
    estimates = get_analyst_estimates(ticker, limit=4)
    valid_eps = [e["earnings_per_share"] for e in estimates if e.get("earnings_per_share")]
    if len(valid_eps) >= 2:
        nearest, furthest = valid_eps[0], valid_eps[-1]
        if nearest:
            trajectory = (furthest - nearest) / abs(nearest)
            traj_score = max(-0.10, min(0.10, trajectory * 0.3))
            score += traj_score
            details["eps_trajectory_pct"] = round(trajectory, 3)
            details["trajectory_score"] = round(traj_score, 3)

    # ── 4. Insider open-market buying vs selling (REST API only) ──────────────
    trades = get_insider_trades(ticker, limit=30)
    if trades:
        cutoff = datetime.today() - timedelta(days=90)
        buy_shares = sell_shares = 0
        for t in trades:
            raw_date = t.get("transaction_date") or t.get("date") or ""
            try:
                tx_date = datetime.strptime(raw_date[:10], "%Y-%m-%d")
            except Exception:
                continue
            if tx_date < cutoff:
                continue
            tx_type = (t.get("transaction_type") or "").strip()
            shares = abs(t.get("transaction_shares") or t.get("shares") or 0)
            if tx_type == "Purchase":
                buy_shares += shares
            elif tx_type == "Sale":
                sell_shares += shares

        total = buy_shares + sell_shares
        if total > 0:
            insider_score = ((buy_shares - sell_shares) / total) * 0.10
            score += insider_score
            details["insider_buy_shares"] = int(buy_shares)
            details["insider_sell_shares"] = int(sell_shares)
            details["insider_score"] = round(insider_score, 3)

    details["total_score"] = round(max(-1.0, min(1.0, score)), 3)
    return {"score": details["total_score"], "details": details}
