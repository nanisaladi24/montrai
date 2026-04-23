# Montrai — Claude Code instructions

This file is auto-loaded as project-scoped guidance for any Claude Code session working in this repo. It defines mandatory workflow items and the conventions the codebase depends on.

---

## Mandatory pre-push checklist

Before running `git push` **for any reason**, Claude MUST complete every item below. If any fails, fix the issue first — do not push.

### 1. Run the full test suite
```bash
.venv/bin/python -m pytest tests/ -q
```
All tests must pass. If any test fails, either fix the code or update the assertion (with the user's confirmation that the behavior change is intentional). Never skip or delete a failing test to make a push go through.

### 2. Secret scan across staged files
```bash
.venv/bin/python -c "
import subprocess, re
files = subprocess.check_output(['git','ls-files','-m','-o','--exclude-standard']).decode().splitlines()
known_secrets = [
    ('Alpaca key',    'PK[A-Z0-9]{15,}'),
    ('Alpaca secret', '[A-Za-z0-9]{40,}'),
    ('FD key',        '[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}'),
    ('Polygon key',   '[A-Za-z0-9_-]{30,}'),
    ('FRED key',      '[a-f0-9]{32}'),
]
hits = []
for f in files:
    try: txt = open(f).read()
    except: continue
    for lbl, pat in known_secrets:
        for m in re.finditer(pat, txt):
            v = m.group(0)
            if 'your_' in v.lower() or 'example' in v.lower() or v.startswith('placeholder'): continue
            # Only flag if the file is NOT .env and NOT runtime.json (both gitignored)
            if f in ('.env', 'config/runtime.json'): continue
            hits.append((f, lbl, v[:10]+'...'))
print('🚨 SECRETS FOUND — DO NOT PUSH:' if hits else '✓ secret scan clean')
for h in hits: print(f'  {h}')
"
```
`.env` and `config/runtime.json` are gitignored. Any secret pattern in a tracked file is a bug — fix it before pushing.

### 3. Verify `.env` and `config/runtime.json` are not staged
```bash
git status --short | grep -E "\.env$|config/runtime\.json$" && echo "⚠ secret file staged" || echo "✓ no secret files staged"
```

### 4. Update documentation if behavior changed
The repo has three user-facing markdown files:
- **`README.md`** — architecture overview, strategy list, quickstart, circuit breakers, data stack
- **`RUN.md`** — day-to-day operating instructions, toggles, troubleshooting
- **`DATA_SOURCES.md`** — per-source setup, costs, what features each unlocks

If the current commit changes any of the following, the relevant doc **must** be updated in the same commit:

| Code change | Update docs |
|---|---|
| New strategy added (selector / execution path) | README.md strategy list + decision tree · RUN.md toggles table |
| New config knob in `runtime.json` | RUN.md toggles table · README.md if user-facing |
| New data source or API integration | DATA_SOURCES.md + README.md data stack table |
| Circuit breaker added / modified | README.md safety table |
| File structure change (new module / moved files) | README.md architecture tree |
| Minimum dependency / Python version change | README.md quickstart |

If no user-visible behavior changed (refactor, internal rename, test-only change), docs are allowed to stay as-is — but note this in the commit message so the reviewer knows it was intentional.

### 5. Commit message discipline
**Keep commit messages short.** Prefer a single-line title. Add body bullets only when they're genuinely non-obvious — 3 bullets max, one line each. No narrative paragraphs, no section headers, no exhaustive file lists. Readers can run `git show` for details.

```
Short imperative title under 70 chars

- Key bullet 1 (only when non-obvious)
- Key bullet 2

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

Never skip hooks (`--no-verify`) or sign options (`--no-gpg-sign`) without explicit user authorization.

---

## Core conventions

### Safety first
The hard-coded circuit breakers in `risk/risk_manager.py` (daily spend caps, drawdown lockout, per-position stops) are invariants. Never weaken them without the user explicitly authorizing the change for a specific threshold. A "bot runs but never trades" day is a correct outcome — don't force trades to make the pipeline feel active.

### Paper before live
Default is paper. Going live requires explicit env-variable flip AND the red 🚨 banner at the top of the dashboard. Any new strategy must be paper-testable and have tests before it's eligible for live use.

### Hot-reload configuration
Operational knobs (thresholds, caps, toggles) live in `config/runtime.json` and are read every bot cycle. No restart needed. New config additions must:
- Have a typed default in `config/settings.py`
- Appear in the `_DEFAULTS` dict in `config/runtime_config.py`
- Appear in `config/runtime.json` (committed template values only — never real secrets)
- Appear in the dashboard Settings tab as a toggle / slider / input
- Have a test asserting the key is exposed

### Broker-agnostic core
The trading logic never imports broker implementations directly. All broker calls go through `executor/order_executor.py`. New broker methods are added to `executor/base.py` as abstract (or `raise NotImplementedError` with a `supports_X()` gate) and implemented per broker.

### No look-ahead bias
Backtester trains on data available at decision time. Walk-forward validation is minimum; any new strategy needs passing backtests before live consideration.

### Signed-qty for directional positions
`OptionsPosition.qty > 0` = long (debit paid). `qty < 0` = short (credit received). `MultiLegPosition` follows the same convention. Exit logic is direction-aware via `is_short` / `is_credit` properties.

### Stable HMM feature schema
`HMM_FEATURE_COLUMNS` in `core/feature_engineering.py` is a fixed 21-column list. Missing data sources zero-fill. Changing the schema forces a full HMM retrain — only do this when deliberately adding a new feature, and update the memory + note the retrain in the commit message.

---

## When in doubt

- User safety > feature ambition. When a strategy might put on bad trades, surface the risk and ask rather than ship.
- Reversible changes (edits, local tests) are free. Irreversible (pushes, public-repo flips, destructive git ops) need explicit confirmation.
- If a test assertion clashes with new behavior, the assertion is often the thing to update — but confirm with the user that the behavior change is intentional first.
- Never commit secrets. If a secret is accidentally staged, unstage + add to `.gitignore` + rotate the secret before any push.

---

## Current state snapshot (as of public-release push)

- 52 tests, typically green in ~1.1s
- Options-primary architecture with dual broker support (Alpaca default, Robinhood available)
- 5 live strategy modules: long options, covered calls, vertical spreads, iron condor, ORB intraday
- Dynamic daily watchlist (pre-market movers + most-actives via Alpaca screener)
- Paper-only safety valve for observability fires
- Full `bot_heartbeat.json` telemetry for dashboard status
