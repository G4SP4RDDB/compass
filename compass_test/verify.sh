#!/usr/bin/env bash
# Safe, non-destructive verification pipeline for compass_test's connectors.
#
# Every step here is either pure math (pytest), a read-only authenticated
# call (check-auth, poll_balance_usd), or a DRY RUN (run/run-hop without
# --live — real gas estimates and quotes, nothing signed or broadcast). No
# funds move. Run from the compass/ repo root:
#
#   compass_test/verify.sh
#
# A real (--live) smoke test is deliberately NOT part of this script — see
# the printed instructions at the end for how to run one yourself, one DEX
# at a time, once you're ready to move a real (capped) dollar amount.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."  # repo root (compass/)

PY=.venv/bin/python
PYTEST=.venv/bin/pytest

pass=0
fail=0
_step() {
    echo
    echo "== $1 =="
}
_check() {
    if "$@"; then
        pass=$((pass + 1))
    else
        fail=$((fail + 1))
        echo "!! FAILED: $*"
    fi
}

_step "1. Unit tests (comparator/safety-cap math, no network)"
_check env PYTHONPATH=src "$PYTEST" compass_test/tests -q

_step "2. Wiring sanity — which DEXes are connector-backed"
_check env PYTHONPATH=src "$PY" -m compass_test.cli list-hops

_step "3. check-auth — one real read-only balance call per connector"
_check env PYTHONPATH=src "$PY" -m compass_test.cli check-auth --dex MEXC --stable USDT
_check env PYTHONPATH=src "$PY" -m compass_test.cli check-auth --dex Aster --stable USDT
_check env PYTHONPATH=src "$PY" -m compass_test.cli check-auth --dex Hyperliquid --stable USDC
_check env PYTHONPATH=src "$PY" -m compass_test.cli check-auth --dex Lighter --stable USDC

_step "4. Per-connector dry runs (real gas estimates/quotes, nothing signed or sent)"
_check env PYTHONPATH=src "$PY" -m compass_test.cli run-hop --dex Hyperliquid --hop deposit  --chain ARBITRUM --stable USDC --amount 5
_check env PYTHONPATH=src "$PY" -m compass_test.cli run-hop --dex Hyperliquid --hop withdraw --chain ARBITRUM --stable USDC --amount 5
_check env PYTHONPATH=src "$PY" -m compass_test.cli run-hop --dex Lighter    --hop withdraw --chain ARBITRUM --stable USDC --amount 5
_check env PYTHONPATH=src "$PY" -m compass_test.cli run-hop --dex Aster     --hop withdraw --chain BSC       --stable USDT --amount 1
_check env PYTHONPATH=src "$PY" -m compass_test.cli run-hop --dex Aster     --hop deposit  --chain BSC       --stable USDT --amount 1
_check env PYTHONPATH=src "$PY" -m compass_test.cli run-hop --dex MEXC     --hop withdraw --chain BSC       --stable USDT --amount 1
_check env PYTHONPATH=src "$PY" -m compass_test.cli run-hop --dex MEXC     --hop deposit  --chain BSC       --stable USDT --amount 1

_step "5. End-to-end journey dry runs (both hops of an actual in-scope route)"
_check env PYTHONPATH=src "$PY" -m compass_test.cli run --from Aster    --to MEXC
_check env PYTHONPATH=src "$PY" -m compass_test.cli run --from Lighter --to Hyperliquid

echo
echo "== Summary: $pass passed, $fail failed =="
if [ "$fail" -ne 0 ]; then
    exit 1
fi

cat <<'EOF'

Nothing above moved any funds. To actually prove a connector moves money,
run ONE hop live yourself, with a small amount within the configured caps
(COMPASS_TEST_MAX_USD_PER_HOP, default $10):

    COMPASS_TEST_ALLOW_LIVE=1 PYTHONPATH=src .venv/bin/python -m compass_test.cli \
        run-hop --dex Hyperliquid --hop deposit --chain ARBITRUM --stable USDC --amount 5 --live

(swap in --hop withdraw, or a different --dex, as needed). This is
deliberately not automated here — see README.md "Safety model".
EOF
