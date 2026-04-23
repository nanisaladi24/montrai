# Data Sources — Montrai

Reference for every feed Montrai uses, where it routes to, and what it costs.

Data architecture is **tiered with graceful fallback** — each fetcher tries the primary source first and falls back silently if it fails. You only need the Alpaca key to run the bot; everything else improves signal quality but isn't blocking.

---

## Required

### Alpaca
- **What it's used for:** Options chain + Greeks + IV, order execution (both paper and live), equity quotes
- **Cost:** Free for paper + market data (with broker-only tier)
- **Sign up:** https://alpaca.markets
- **Keys:** `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` in `.env`
- **Without it:** Bot won't start. No options data, no execution.

---

## Recommended (free tiers work)

### financial-datasets.ai
- **What it's used for:** Equity + ETF daily OHLCV (replaces yfinance for SEC-tickered securities), fundamentals (P/E, earnings, insider trades), news
- **Cost:** Free tier with 250 req/month, paid plans available
- **Sign up:** https://financialdatasets.ai
- **Key:** `FINANCIAL_DATASETS_API_KEY` in `.env`
- **Impact:** Cleaner equity data than yfinance, plus a fundamental overlay blended 30% into the swing signal (valuation, earnings beats, insider activity).

### Polygon / massive.com
- **What it's used for:**
  - VIX / VVIX / VIX3M index daily closes (for HMM macro features)
  - Stock + ETF daily + intraday minute bars
  - Options contract aggregates + chain history
  - Latest trade quotes
- **Cost:** Varies by tier. Many endpoints available on free or entry-level tiers; some (full-market snapshot, indices aggregates, S3 flat files) are gated to higher tiers.
- **Sign up:** https://massive.com
- **Key:** `POLYGON_API_KEY` in `.env`
- **Tier-gated endpoints that may return NOT_AUTHORIZED on your plan:**
  - `/v2/snapshot/locale/us/markets/stocks/gainers` and `/losers`
  - `/v2/snapshot/locale/us/markets/stocks/tickers` (full-market snapshot)
  - `/v2/aggs/ticker/I:VIX/range/...` (index aggregates)
  - S3 flat-file downloads
- **Fallback:** Bot falls back to Alpaca screener (for movers) and yfinance (for indices) silently. Existing functionality continues.
- **S3 flat files** (if included in your plan): historical options chains + minute bars going back years. Use `scripts/polygon_s3_sync.py` to pull locally and `core/polygon_s3.py` to read.

### FRED (Federal Reserve Economic Data)
- **What it's used for:** Yield curve spread (10Y-2Y), Fed Funds rate, HY credit spread — all three fed into the HMM as macro features
- **Cost:** Free, unlimited
- **Sign up:** https://fred.stlouisfed.org/docs/api/api_key.html
- **Key:** Set in dashboard → Data Sources tab, stored in `config/runtime.json` as `data_sources.fred_api_key`
- **Impact:** High. Yield curve inversion is historically the cleanest recession predictor.

---

## Fallback (no key required)

### yfinance
- **What it's used for:** Last-resort fallback for:
  - DXY (`DX-Y.NYB` — ICE futures, not available on financial-datasets or Polygon index aggregates on most tiers)
  - VIX / VVIX / VIX3M when Polygon indices return NOT_AUTHORIZED
  - Any SEC ticker when financial-datasets fails
- **Cost:** Free (unofficial Yahoo scraping)
- **Impact:** Nothing to configure. Fallbacks run automatically.
- **Warning:** yfinance is unofficial — breaks occasionally when Yahoo changes their internal API.

---

## HMM feature matrix (always 21 columns)

Stable schema — missing sources zero-fill rather than shrinking the column count. Prevents force-retrains on transient API outages.

| Feature | Source |
|---|---|
| `ret_1d`, `ret_5d`, `ret_20d`, `ret_60d` | SPY OHLC (financial-datasets → yfinance) |
| `realised_vol`, `atr_pct`, `bb_position`, `vol_ratio` | Computed from SPY OHLC |
| `vix_rank`, `vix_term_ratio` | VIX + VIX3M (Polygon → yfinance) |
| `vvix_rank`, `vvix_vix_ratio` | VVIX + VIX (Polygon → yfinance) |
| `tlt_ret`, `hyg_ret`, `smh_spy_rs` | TLT / HYG / SMH ETFs (financial-datasets) |
| `dxy_ret` | DXY (yfinance only) |
| `yield_curve_spread`, `fed_funds_rate`, `hy_credit_spread` | FRED |
| `gex_per_spot`, `gamma_flip_dist` | Alpaca options chain (Black-Scholes computed locally) |

---

## Why not Bloomberg / Refinitiv / LSEG?

$20K–$30K per year per seat. Not remotely appropriate for a personal or small-scale trading operation. What they offer beyond the stack above:
- Intraday order book depth (Level 2 / Level 3)
- OTC derivatives and fixed-income data
- Research feeds with institutional coverage

None of which unlocks additional edge for equity options day-trading.

---

## Priority order for new users

1. **Alpaca** — required. Sign up, paper account, copy keys to `.env`.
2. **financial-datasets** — free tier is enough for the fundamental overlay. Sign up, add key.
3. **FRED** — free and fast to set up. Add key via dashboard.
4. **Polygon / massive.com** — most valuable for intraday and options history. Free tier covers the basics; upgrade when you need more data.
5. Everything else is optional.
