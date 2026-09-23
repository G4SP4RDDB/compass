"""Environment/config for compass_test.

Secrets are NOT copied into this folder. We load compass's own .env (for
ALCHEMY_API_KEY, used to read real gas prices/USD conversion — same source
costing.py itself uses, so the comparison isn't biased by a different price
feed) plus piggybank-arb's .env directly BY PATH (its DEX credentials:
MEXC_API_KEY/SECRET, ASTER_*, etc.) — one secret at rest, not two.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

COMPASS_TEST_DIR = Path(__file__).resolve().parent
COMPASS_ROOT = COMPASS_TEST_DIR.parent
REPO_ROOT = COMPASS_ROOT.parent  # .../piggyBank
REPORTS_DIR = COMPASS_TEST_DIR / "reports"

DEFAULT_PIGGYBANK_ARB_ENV = REPO_ROOT / "piggybank-arb" / ".env"
PIGGYBANK_ARB_ENV_PATH = Path(os.getenv("PIGGYBANK_ARB_ENV_PATH", str(DEFAULT_PIGGYBANK_ARB_ENV)))

# Order matters: compass's own .env first (ALCHEMY_API_KEY etc.), then
# piggybank-arb's (DEX credentials) — override=False on both so a var
# already set in the real process environment always wins over either file.
load_dotenv(COMPASS_ROOT / ".env", override=False)
if PIGGYBANK_ARB_ENV_PATH.exists():
    load_dotenv(PIGGYBANK_ARB_ENV_PATH, override=False)


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set. Checked {COMPASS_ROOT / '.env'} and {PIGGYBANK_ARB_ENV_PATH} "
            "(and the real process environment)."
        )
    return value


# --- Safety gate -------------------------------------------------------
# A real transfer requires BOTH `--live` on the CLI (see cli.py) AND this
# environment flag — a stray `--live` alone is never enough. Deliberately
# two independent switches: one you type on the command line, one that has
# to already be set in your shell/`.env` before you start.
ALLOW_LIVE = os.getenv("COMPASS_TEST_ALLOW_LIVE") == "1"

# Hard USD caps enforced by executor.py regardless of --live/--yes. A hop
# above these is refused outright, not just warned about.
MAX_USD_PER_HOP = float(os.getenv("COMPASS_TEST_MAX_USD_PER_HOP", "10"))
MAX_USD_PER_RUN = float(os.getenv("COMPASS_TEST_MAX_USD_PER_RUN", "20"))

# Timeouts for polling an on-chain wallet balance / a DEX account balance
# for a credit before giving up and reporting the hop as "unconfirmed"
# rather than hanging forever. The interval bounds the measurement's
# granularity: actualTimeSeconds overshoots the real credit time by up to
# one interval, and those measurements now feed the solver's Time(e)
# directly (see calibration.py) — 2s keeps that bias small next to the
# fastest real hops seen so far (~3s Ondo withdraw on Arbitrum) without
# hammering the RPC/DEX API.
POLL_TIMEOUT_SECONDS = float(os.getenv("COMPASS_TEST_POLL_TIMEOUT_SECONDS", "600"))
POLL_INTERVAL_SECONDS = float(os.getenv("COMPASS_TEST_POLL_INTERVAL_SECONDS", "2"))

# --- Swap hop (CoW Swap, see runners/cowswap.py) -------------------------
# Test amount for a Swap hop when none is given: unlike Withdraw/Deposit
# there's no per-DEX "minimum" to fall back on (a swap has no DEX), only
# CoW's own implicit floor — the network fee must fit inside the sell
# amount (a ~$0.01 fee on a $1 USDC->USDT order was quoted fine on both BSC
# and Arbitrum, 2026-09-10).
DEFAULT_SWAP_TEST_USD = float(os.getenv("COMPASS_TEST_SWAP_TEST_USD", "1"))
# Slippage tolerance folded into the order's minimum buy amount (basis
# points). CoW never fills below it — a worse market simply lets the order
# expire unfilled (status "expired"), it never executes at a worse price.
# 50 bps = 0.5%: generous for a stable/stable pair (quoted spread was ~1
# bp), tight enough that a real depeg can't be traded through.
SWAP_SLIPPAGE_BPS = int(os.getenv("COMPASS_TEST_SWAP_SLIPPAGE_BPS", "50"))

# --- CCTP bridge hop (Arbitrum->Solana, see connectors/cctp.py, cctp_runner.py) ---
# Test amount when none is given: like a Swap, a CCTP bridge has no DEX-side
# minimum to fall back on (see hop_runner.resolve_cctp_bridge_hop) — Circle
# imposes no protocol minimum on a V1 depositForBurn either, so this is a
# nominal small amount, not a floor discovered live like Aden's.
DEFAULT_CCTP_TEST_USD = float(os.getenv("COMPASS_TEST_CCTP_TEST_USD", "1"))

# Name of the env var (checked via the same load chain above) holding the
# operating wallet's private key — the wallet that receives DEX withdrawals
# and signs on-chain deposits (plain ERC-20 transfer or a DEX's deposit
# contract call). Different DEXes' piggybank-arb keys authenticate very
# different things (trading vs. a general wallet) — see README.md "Operating
# wallet" — so this is deliberately a pointer to a var name, never a key
# itself, resolved lazily by wallet.py only when an on-chain tx is actually
# being built/signed.
OPERATING_WALLET_KEY_VAR = os.getenv("COMPASS_TEST_WALLET_KEY_VAR", "")

# Optional: the wallet's public address, so dry runs (gas estimation,
# balance polling) work with zero private key material present. Not
# required for a live run — the address is always re-derived from the real
# key at signing time there (see wallet.OperatingWallet.address).
OPERATING_WALLET_ADDRESS = os.getenv("COMPASS_TEST_WALLET_ADDRESS")

# Solana counterpart of the two vars above (see solana_wallet.SolanaWallet) —
# ed25519 keypair, entirely separate key material from the EVM wallet.
# Signs the Solana-side receiveMessage of the Arbitrum->Solana CCTP withdraw
# pipeline (see connectors/cctp.py) and receives the minted USDC. Same
# pointer-indirection convention as OPERATING_WALLET_KEY_VAR: this names the
# env var holding the key, never the key itself.
SOLANA_WALLET_KEY_VAR = os.getenv("COMPASS_TEST_SOLANA_WALLET_KEY_VAR", "")
SOLANA_WALLET_ADDRESS = os.getenv("COMPASS_TEST_SOLANA_WALLET_ADDRESS")

# --- Metrics dashboard (TimescaleDB, see ../docker-compose.yml) ---------
# reporter.save_report() best-effort projects every report into this DB
# (see metrics_db.py) alongside the JSON files above, which stay the
# source of truth — an unreachable/misconfigured DB never blocks a report
# from saving. Default points at the local `docker compose up -d` service.
TIMESCALE_DB_URL = os.getenv(
    "TIMESCALE_DB_URL", "postgresql://compass:compass_dev_only@localhost:5433/compass_metrics"
)

# Hardcoded deposit addresses, one env var per (DEX, chain) — the PRIMARY
# source for a CEX connector's deposit address (see runners/mexc.py::
# build_deposit_tx), not merely a cross-check: MEXC's own live
# GET /api/v3/capital/deposit/address returned `[]` for this account/USDT/BSC
# even with the address confirmed to exist on MEXC's own site (2026-09-05),
# so that endpoint is no longer consulted at all for this — copy the address
# from the exchange's UI directly instead, one (dex, chain) pair at a time as
# each is confirmed. Naming: COMPASS_TEST_EXPECTED_DEPOSIT_ADDRESS_<DEX>_<CHAIN>,
# e.g. COMPASS_TEST_EXPECTED_DEPOSIT_ADDRESS_MEXC_BSC. A (dex, chain) pair
# with nothing set here simply can't build a deposit tx yet (see
# build_deposit_tx's RuntimeError) rather than guessing or hitting a live
# endpoint that's demonstrated unreliable for this purpose.
_EXPECTED_DEPOSIT_ADDRESS_PREFIX = "COMPASS_TEST_EXPECTED_DEPOSIT_ADDRESS_"


def expected_deposit_addresses() -> dict[str, str]:
    """{env var name: address} for every hardcoded deposit address currently
    set — used both by runners building a deposit tx (see
    runners/mexc.py::build_deposit_tx) and by
    tests/test_deposit_addresses.py's blanket sanity check."""
    return {
        name: value
        for name, value in os.environ.items()
        if name.startswith(_EXPECTED_DEPOSIT_ADDRESS_PREFIX) and value
    }


def expected_deposit_address(dex: str, chain: str) -> str | None:
    return os.getenv(f"{_EXPECTED_DEPOSIT_ADDRESS_PREFIX}{dex.upper()}_{chain.upper()}")
