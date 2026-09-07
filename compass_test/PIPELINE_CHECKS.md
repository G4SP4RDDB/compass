# Pipeline health checks

All commands below are run from the repo root (`compass/`), with the venv
active:

```bash
source .venv/bin/activate
```

Everything in sections 1-4 is read-only or dry-run — nothing moves funds.
Section 5 (live) is the only exception and is opt-in on purpose.

## 1. Environment sanity

```bash
# .env is actually gitignored (should print the .env line, no error)
git check-ignore -v .env

# Confirm the live-run gate's current state — should read "1" only if you
# deliberately want live runs enabled right now
grep COMPASS_TEST_ALLOW_LIVE .env
```

## 2. Unit test suite

```bash
# Whole repo (graph/solver tests + compass_test tests)
python -m pytest -q

# compass_test only
python -m pytest compass_test/ -q
```

## 3. Real balance readers (compass_test/balances.py) — all 8 DEXes + wallet

No CLI subcommand wraps `balances.py` directly (the CLI's `check-auth` only
covers the two DEXes with a full connector — see section 4). Call it inline:

```bash
# Per-DEX equity, split USDT/USDC (the same reader the frontend's Details
# panel calls via GET /api/dex-balances/<name>)
python -c "
from compass_test import balances
for name in ['MEXC', 'Aster', 'Aden', 'Ondo Perps', 'Hyperliquid', 'dYdX', 'Extended', 'Lighter']:
    r = balances.get_real_balance(name)
    print(f'{name:12s} {r.balances if r.balances is not None else r.error}')
"

# Operating wallet's on-chain USDC/USDT on Arbitrum + BSC (same reader as
# GET /api/wallet-balances / the frontend's Wallet panel)
python -c "
from compass_test import balances
for r in balances.list_wallet_balances():
    print(f'{r.chain:10s} {r.stable:5s} {r.balanceUsd if r.error is None else r.error}')
"
```

A working pipeline prints a real number (or `0.0`) for every row. An
`error` string instead of a number means that DEX's credentials are
missing/expired or its API is unreachable — check the matching `*_API_KEY`
/`*_API_SECRET` (or `*_WALLET_ADDRESS`/`*_ACCOUNT_INDEX`) vars in `.env`.

## 4. Connector smoke test + dry-run hops (MEXC, Aster only)

These two are the only DEXes with a full withdraw/deposit connector
(`compass_test/runners/registry.py`); the other six are balance-read-only.

```bash
# Auth smoke test — one signed balance call per DEX/stable
python -m compass_test.cli check-auth --dex MEXC  --stable USDT
python -m compass_test.cli check-auth --dex Aster --stable USDT

# What the solver's current solved graph would actually run
python -m compass_test.cli list-hops

# Dry-run a full journey between two DEXes (no --live -> nothing moves,
# still exercises fee-quoting/network-resolution code on each connector)
python -m compass_test.cli run --from Aster --to MEXC --amount 5

# Dry-run ONE hop directly, independent of the solver's current journeys
python -m compass_test.cli run-hop --dex MEXC  --hop withdraw --chain ARBITRUM --stable USDT --amount 1
python -m compass_test.cli run-hop --dex Aster --hop deposit  --chain BSC       --stable USDT --amount 1
```

Each dry run writes a report (path printed at the end,
`compass_test/reports/` by default) comparing estimated vs. "actual"
cost/time — in dry-run mode "actual" is whatever the connector's own
fee/route quoting API returns, just without submitting the transaction.

## 5. Frontend + server

```bash
# Regenerate graph.html from the current solved graph (also re-seeds the
# demo imbalances, grounded in the real balances read in section 3)
python src/main.py

# Start the Flask server (serves graph.html + the /api/* endpoints)
python -m visualization.server
# -> http://127.0.0.1:8765
```

With the server running, in another terminal:

```bash
# Real per-stable balance for one DEX (what the Details panel calls)
curl -s http://127.0.0.1:8765/api/dex-balances/Aster | python -m json.tool

# Real on-chain wallet balances (what the Wallet panel calls)
curl -s http://127.0.0.1:8765/api/wallet-balances | python -m json.tool

# Past test-run reports (Test Results tab)
curl -s http://127.0.0.1:8765/api/test-runs | python -m json.tool
```

Then open `http://127.0.0.1:8765` in a browser, click a DEX node, and
confirm the "Real balance (live)" row in the Details panel matches the
`curl` output above.

## 6. Live hop (real funds — opt-in, do not run casually)

Requires **both** `COMPASS_TEST_ALLOW_LIVE=1` in `.env` (section 1) and
`--live` on the command line; without `--yes` it also asks for a typed
`YES` confirmation showing exactly what will move where. Hard caps from
`compass_test/config.py`: `$MAX_USD_PER_HOP` per hop, `$MAX_USD_PER_RUN` per
run.

```bash
python -m compass_test.cli run-hop --dex MEXC --hop withdraw --chain ARBITRUM --stable USDT --amount 1 --live
```

Omit `--live` (or unset `COMPASS_TEST_ALLOW_LIVE`) to go back to dry-run —
that's the default, keep it that way outside of a deliberate live check.
