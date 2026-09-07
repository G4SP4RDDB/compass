"""compass_test — executes real Withdraw/Deposit hops chosen by the compass
solver (see src/graph) against live DEX APIs and on-chain wallets, then
compares the actual gas paid and actual elapsed time against the graph's own
Fee(e)/Time(e) estimate (src/graph/costing.py) for that exact edge.

Always defaults to dry-run (no funds moved, no authenticated calls). A real
transfer requires BOTH `--live` on the CLI AND COMPASS_TEST_ALLOW_LIVE=1 in
the environment (see config.py) — belt-and-suspenders so a stray flag alone
can never move real money.

See README.md for setup, scope, and the list of DEXes not yet instrumented.
"""

import sys
from pathlib import Path

# Every submodule here imports from src/graph and src/connectors (the same
# code the solver/frontend use) as if `src` were on PYTHONPATH — which it
# normally has to be set manually for (see visualization/server.py, run via
# `PYTHONPATH=src python -m visualization.server`). Doing it here, in the
# package __init__ that always runs before any submodule's own top-level
# imports, means `python -m compass_test.cli ...` works with no PYTHONPATH
# footwork required.
_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))
