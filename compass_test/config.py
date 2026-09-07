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
# rather than hanging forever.
POLL_TIMEOUT_SECONDS = float(os.getenv("COMPASS_TEST_POLL_TIMEOUT_SECONDS", "600"))
POLL_INTERVAL_SECONDS = float(os.getenv("COMPASS_TEST_POLL_INTERVAL_SECONDS", "5"))

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
