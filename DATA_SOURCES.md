# Data Sources — Montrai

A reference for every data feed Montrai uses or can use, with cost, what it unlocks, and setup instructions.

---

## Currently Active (Free)

All sourced via **yfinance** — no API key needed.

| Signal | Ticker | What it tells the HMM |
|---|---|---|
| SPY returns + technicals | `SPY` | Core market direction |
| VIX | `^VIX` | Fear gauge |
| VIX 3-month | `^VIX3M` | VIX term structure |
| VVIX | `^VVIX` | Vol-of-vol: early warning before VIX spikes |
| TLT | `TLT` | Bond market / flight-to-safety |
| DXY | `DX-Y.NYB` | US dollar strength / liquidity tightening |
| HYG | `HYG` | High-yield credit / risk appetite |
| SMH | `SMH` | Semiconductor sector leadership |

---

## Free — Just Needs API Key

### FRED (Federal Reserve Economic Data)
- **Cost:** Free
- **Sign up:** https://fred.stlouisfed.org/docs/api/api_key.html
- **What it unlocks:**
  - Yield curve (10Y–2Y spread) — the best recession predictor
  - Fed Funds rate — context for rate-sensitive regimes
  - Credit spreads (IG and HY OAS)
- **Set in dashboard:** Data Sources → FRED API Key
- **Impact:** High. Yield curve inversion historically precedes bear regimes by 6–18 months.

---

## Paid — Recommended

### Polygon.io
- **Cost:** Free tier (delayed data) | $29/mo (real-time) | $79/mo (options)
- **Sign up:** https://polygon.io/
- **What it unlocks:**
  - Real-time quotes (vs yfinance's 15-min delay on some feeds)
  - Options chain data (volume, OI, IV by strike)
  - Tick-level trade data for intraday strategies
- **Set in dashboard:** Data Sources → Polygon API Key
- **Impact:** Medium now (swing trading tolerates 15-min delay). High when adding intraday or options strategies.

### SpotGamma
- **Cost:** ~$50/mo (Founder tier) | ~$110/mo (Pro)
- **Sign up:** https://spotgamma.com/
- **API:** None — SpotGamma is a web UI only, no public API.
- **Status:** GEX is **already calculated internally** in Montrai using the SPY options chain + Black-Scholes. `gex_per_spot` and `gamma_flip_dist` are always-active HMM features, no subscription needed.
  - Positive GEX → dealers long gamma → price mean-reverts (chop)
  - Negative GEX → dealers short gamma → moves get amplified (trending)
  - `gamma_flip_dist` — how far spot is from the zero-gamma level
- **Use SpotGamma for:** Visual cross-checking your bot's GEX readings. Their charts are excellent for manual review.
- **Impact:** Already captured — no action needed.

### Unusual Whales
- **Cost:** ~$50/mo
- **Sign up:** https://unusualwhales.com/
- **What it unlocks:**
  - Options flow: large unusual options bets (often precede moves by 1–3 days)
  - Dark pool prints: large institutional block trades
  - Congress/Senate trading disclosures
- **Set in dashboard:** Data Sources → Unusual Whales API Key
- **Impact:** Medium-High for individual stock signals. Best combined with existing swing signal scorer.

### Nasdaq Data Link (formerly Quandl)
- **Cost:** Free tier available | paid bundles vary ($50–$500+/mo)
- **Sign up:** https://data.nasdaq.com/
- **What it unlocks:**
  - Short interest data (days to cover, short float %)
  - Institutional ownership changes (13F filings, aggregated)
  - COT (Commitment of Traders) reports for futures positioning
- **Set in dashboard:** Data Sources → Nasdaq Data Link API Key
- **Impact:** Medium. Short interest is a useful contrarian signal; COT data is valuable for macro regime context.

---

## Not Recommended (Yet)

| Source | Reason to skip for now |
|---|---|
| **TradingView** | Charts and alerts only — no programmatic API for data ingestion |
| **Bloomberg Terminal** | $25K+/yr — overkill until this is institutional-scale |
| **Refinitiv/LSEG** | Same as Bloomberg at similar cost |
| **$TICK / $ADD / $TRIN** | Intraday breadth only — not available on daily bars via any free/cheap feed |

---

## Priority Order (Suggested Upgrade Path)

1. **FRED** — Free, done. Yield curve, fed funds rate, credit spread active.
2. **SpotGamma** — No API needed. GEX already calculated internally from yfinance options chain.
3. **Unusual Whales** — $50/mo. Add once the bot is consistently profitable in paper.
4. **Polygon.io** — $29–79/mo. Prioritize when adding intraday or options strategies.
5. **Nasdaq Data Link** — Add when short interest or COT signals are needed.

---

## Feature Unlock Summary

| Key configured | New HMM features unlocked |
|---|---|
| FRED | `yield_curve_spread`, `fed_funds_rate`, `hy_credit_spread` |
| Polygon | Higher-quality real-time quotes (replaces yfinance for live signals) |
| SpotGamma | `gex_level`, `gamma_flip_distance` |
| Unusual Whales | `options_flow_score` per symbol |
| Nasdaq Data Link | `short_interest_ratio`, `cot_net_positioning` |
