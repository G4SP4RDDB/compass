"""Real balance readers — two kinds, both read straight from a live source,
never zfund/sentinel at runtime, never cached:

  - Per-DEX equity (get_real_balance/list_all_balances), broken down per
    stablecoin (USDT/USDC — most of these DEXes only ever hold one, see each
    reader's docstring): each DEX's own API, using the credentials already
    in piggybank-arb/.env. get_real_balance_usd is a back-compat shim
    summing the two into one USD figure, for src/main.py's demo-imbalance
    generator.
  - The operating wallet's own on-chain holdings (get_wallet_balance_usd/
    list_wallet_balances): a plain ERC-20 balanceOf on Arbitrum/BSC, USDC/
    USDT only for now — see compass_test/wallet.py and GET
    /api/wallet-balances (visualization/server.py), which feeds the
    frontend's "Wallet" panel.

Read-only: nothing here ever signs or moves anything, safe to call any time
regardless of the "local mock tests only" policy on the execution side (see
executor.py/runners/ — unrelated, still real-money-capable code that this
module does not touch).

Deliberately separate from runners/ (DexConnector requires withdraw()/
build_deposit_tx() too, which most of these DEXes don't have built — this
module is balance-only, and covers more DEXes than runners/registry.py does
as a result).

Endpoint/auth choices verified live 2026-09-04. Aster, Aden and Ondo Perps
were NOT figured out from public docs (which are wrong or incomplete for
all three — see the git history on this file for the earlier, broken
attempts): they're ported from sentinelBackend/sentinel/src/connectors/
{aster,aden,ondo}/http.py, an existing, already-working balance-streaming
service for the same accounts. Each function below names the exact source
file/function it mirrors.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass

import requests

from . import config


@dataclass
class DexBalanceResult:
    dex: str
    balances: dict[str, float] | None  # {"USDT": ..., "USDC": ...}
    fetchedAt: float
    error: str | None = None


def _mexc() -> dict[str, float]:
    """GET /api/v3/account (signed) — per-asset `free` balance, read once per
    stable (MEXC's own registry lists it as USDT-only today, but the account
    can hold either)."""
    from graph.structures.DEXes import Stable

    from .runners.mexc import MexcConnector

    connector = MexcConnector()
    return {
        "USDT": connector.poll_balance_usd(Stable.USDT),
        "USDC": connector.poll_balance_usd(Stable.USDC),
    }


def _aster() -> dict[str, float]:
    """GET /fapi/v3/account (signed) — `marginBalance` of the USDT and USDC
    asset rows specifically (an account can hold both as separate collateral
    assets; earlier code summed EVERY nonzero asset row, which folded in any
    other collateral the account holds too). Signing: see aster_signing.py —
    NOT what Aster's public docs describe."""
    from .aster_signing import v3_signed_query

    account = config.require_env("ASTER_USER")
    signer_address = config.require_env("ASTER_SIGNER")
    signer_key = config.require_env("ASTER_PRIVATE_KEY")

    query = v3_signed_query(account, signer_address, signer_key)
    url = f"https://fapi.asterdex.com/fapi/v3/account?{query}"
    response = requests.get(url, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15)
    response.raise_for_status()
    data = response.json()
    by_asset = {a.get("asset"): float(a.get("marginBalance") or 0) for a in data.get("assets", [])}
    return {"USDT": by_asset.get("USDT", 0.0), "USDC": by_asset.get("USDC", 0.0)}


def _aden() -> dict[str, float]:
    """GET /api/v1/dex_futures/usdt/accounts (signed) — equity = total +
    unrealised_pnl. USDT-only by construction: the endpoint path itself
    (.../usdt/accounts) is Aden's one and only settlement currency (see
    sentinelBackend's `_SETTLE_CURRENCY = "USDT"`, src/connectors/aden/
    http.py) — there is no equivalent USDC endpoint to read.

    Ported from sentinelBackend's AdenHttpConnector._sign /
    _authenticated_request — Aden's PUBLIC API docs
    (aden-perp-api-docs.aden.io) only cover market data, this authenticated
    endpoint isn't documented there at all. Signing is Gate.io-style:
    HMAC-SHA512 over
    f"{METHOD}\\n{path}\\n{query_string}\\n{sha512(body).hexdigest()}\\n{timestamp}",
    sent as KEY/Timestamp/SIGN headers.
    """
    api_key = config.require_env("ADEN_API_KEY")
    api_secret = config.require_env("ADEN_API_SECRET")
    path = "/api/v1/dex_futures/usdt/accounts"
    timestamp = int(time.time())
    body_hash = hashlib.sha512(b"").hexdigest()
    message = f"GET\n{path}\n\n{body_hash}\n{timestamp}"
    signature = hmac.new(api_secret.encode(), message.encode(), hashlib.sha512).hexdigest()
    headers = {"KEY": api_key, "Timestamp": str(timestamp), "SIGN": signature}
    response = requests.get(f"https://api.aden.io{path}", headers=headers, timeout=15)
    response.raise_for_status()
    data = response.json()
    return {"USDT": float(data.get("total") or 0) + float(data.get("unrealised_pnl") or 0), "USDC": 0.0}


def _ondo() -> dict[str, float]:
    """GET /v1/perps/balance (signed) — `marginBalance` field. USDC-only:
    Ondo's whole perps product settles in USDC (sentinelBackend's
    `SETTLEMENT_ASSET = "USDC"`, src/connectors/ondo/http.py) — the response
    has no per-asset breakdown to read a USDT figure from.

    Ported from sentinelBackend's OndoHttpConnector._sign_rest /
    _authenticated_request. Ondo's PUBLIC Builder Integration Guide
    describes Bearer-JWT-via-SIWE auth for third-party app builders — a
    different, more involved flow from the one an already-provisioned API
    key pair actually uses in production: ONDO-KEY-ID/ONDO-TIMESTAMP/
    ONDO-SIGN headers, where
    ONDO-SIGN = hex(HMAC-SHA256(secret, timestamp + METHOD + path + body)).
    """
    api_key = config.require_env("ONDO_API_KEY")
    api_secret = config.require_env("ONDO_API_SECRET")
    path = "/v1/perps/balance"
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}GET{path}"
    signature = hmac.new(api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    headers = {"ONDO-KEY-ID": api_key, "ONDO-TIMESTAMP": timestamp, "ONDO-SIGN": signature, "Accept": "application/json"}
    response = requests.get(f"https://api.ondoperps.xyz{path}", headers=headers, timeout=15)
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise RuntimeError(f"ondo api error: {data.get('error') or data.get('error_code')}")
    return {"USDC": float(data["result"]["marginBalance"]), "USDT": 0.0}


def _hyperliquid() -> dict[str, float]:
    """Mirrors piggybank-arb/src/dex/hyperliquid/index.ts's
    fetchBalanceFromRest for USDC exactly: spot USDC total + builder-dex
    (HIP-3: "xyz", "hyna") equity, which is USDC-margined. The DEFAULT perp
    dex's clearinghouseState is deliberately NOT added on top — its
    collateral is already folded into the spot USDC total since a
    Hyperliquid API change (confirmed by piggybank-arb's own comment; a
    naive clearinghouseState-only read gave $0.00 here even though real
    funds existed in the spot balance). USDT is read the same way, straight
    off the spot balances list — Hyperliquid spot can hold either.
    """
    address = config.require_env("HYPERLIQUID_WALLET_ADDRESS")

    def info(payload: dict) -> dict:
        response = requests.post("https://api.hyperliquid.xyz/info", json=payload, timeout=15)
        response.raise_for_status()
        return response.json()

    spot = info({"type": "spotClearinghouseState", "user": address})
    spot_by_coin = {b.get("coin"): float(b.get("total") or 0) for b in spot.get("balances", [])}

    builderDexEquityUsd = 0.0
    for dex in ("xyz", "hyna"):
        state = info({"type": "clearinghouseState", "user": address, "dex": dex})
        builderDexEquityUsd += float(state.get("marginSummary", {}).get("accountValue", 0.0))

    return {
        "USDC": spot_by_coin.get("USDC", 0.0) + builderDexEquityUsd,
        "USDT": spot_by_coin.get("USDT", 0.0),
    }


def _dydx() -> dict[str, float]:
    """GET https://indexer.dydx.trade/v4/addresses/{address} — public
    indexer, no signing needed for a read. Sums `equity` across every
    subaccount (usually only subaccount 0 is funded, but nothing guarantees
    that). USDC-only: dYdX v4 subaccounts are exclusively USDC-collateralized,
    there is no USDT equivalent."""
    address = config.require_env("DYDX_PERP_ADDRESS")
    response = requests.get(f"https://indexer.dydx.trade/v4/addresses/{address}", timeout=15)
    response.raise_for_status()
    subaccounts = response.json().get("subaccounts", [])
    return {"USDC": sum(float(s.get("equity", 0.0)) for s in subaccounts), "USDT": 0.0}


def _extended() -> dict[str, float]:
    """GET /api/v1/user/balance on the Starknet instance — a plain API key
    (X-Api-Key header) is enough for this READ; the Stark signature is only
    needed for write operations (orders, transfers, withdrawals), per
    Extended's own docs. A 404 specifically means a zero balance (documented
    behavior), not a real error. USDC-only: Extended's perps collateral is
    exclusively USDC."""
    api_key = config.require_env("EXTENDED_API_KEY")
    response = requests.get(
        "https://api.starknet.extended.exchange/api/v1/user/balance",
        headers={"X-Api-Key": api_key},
        timeout=15,
    )
    if response.status_code == 404:
        return {"USDC": 0.0, "USDT": 0.0}
    response.raise_for_status()
    return {"USDC": float(response.json()["data"]["equity"]), "USDT": 0.0}


def _lighter() -> dict[str, float]:
    """GET /api/v1/account?by=index&value=<LIGHTER_ACCOUNT_INDEX> — a public
    read endpoint, no API key needed at all. Sums `collateral` across every
    account entry returned (normally just one, for this account index).
    USDC-only: zkLighter's perps collateral is exclusively USDC."""
    account_index = config.require_env("LIGHTER_ACCOUNT_INDEX")
    response = requests.get(
        "https://mainnet.zklighter.elliot.ai/api/v1/account",
        params={"by": "index", "value": account_index},
        timeout=15,
    )
    response.raise_for_status()
    accounts = response.json().get("accounts", [])
    return {"USDC": sum(float(a.get("collateral", 0.0)) for a in accounts), "USDT": 0.0}


_READERS = {
    "MEXC": _mexc,
    "Aster": _aster,
    "Aden": _aden,
    "Ondo Perps": _ondo,
    "Hyperliquid": _hyperliquid,
    "dYdX": _dydx,
    "Extended": _extended,
    "Lighter": _lighter,
}

# Every registry DEX has a reader now (see module docstring — Aster/Aden/Ondo
# Perps were unblocked by porting sentinelBackend's working connectors
# rather than trusting each DEX's public docs). Kept as an explicit map
# (not just "reader missing") so a FUTURE unsupported DEX still gets a named
# reason instead of a bare "no reader" — same spirit as runners/unsupported.py.
_NO_API_REASON: dict[str, str] = {}


def get_real_balance(dex_name: str) -> DexBalanceResult:
    """Never raises — a missing reader, missing credentials, or a failed
    live call all come back as DexBalanceResult(balances=None, error=...)
    rather than an exception, so a caller listing every DEX doesn't have to
    guard each one individually. `balances` is `{"USDT": ..., "USDC": ...}`
    — each DEX's reader fills in 0.0 for whichever stable it doesn't
    actually hold (see each reader's docstring for which that is)."""
    reader = _READERS.get(dex_name)
    if reader is None:
        reason = _NO_API_REASON.get(dex_name, "no balance reader implemented for this DEX")
        return DexBalanceResult(dex=dex_name, balances=None, fetchedAt=time.time(), error=reason)
    try:
        return DexBalanceResult(dex=dex_name, balances=reader(), fetchedAt=time.time())
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        return DexBalanceResult(dex=dex_name, balances=None, fetchedAt=time.time(), error=str(exc))


def list_all_balances(dex_names: list[str]) -> list[DexBalanceResult]:
    return [get_real_balance(name) for name in dex_names]


def get_real_balance_usd(dex_name: str) -> "BalanceResultUsd":
    """Back-compat shim for callers that only want one combined USD figure
    (src/main.py's demo-imbalance generator — stablecoins are treated as
    dollar-par there, same as everywhere else in this codebase) — sums
    `get_real_balance(dex_name).balances` across USDT+USDC rather than
    tracking its own separate reader."""
    result = get_real_balance(dex_name)
    total = sum(result.balances.values()) if result.balances is not None else None
    return BalanceResultUsd(dex=result.dex, balanceUsd=total, fetchedAt=result.fetchedAt, error=result.error)


@dataclass
class BalanceResultUsd:
    dex: str
    balanceUsd: float | None
    fetchedAt: float
    error: str | None = None


@dataclass
class WalletBalanceResult:
    chain: str  # graph.structures.DEXes.Chain member name
    stable: str  # graph.structures.DEXes.Stable member name
    balanceUsd: float | None
    fetchedAt: float
    error: str | None = None


# Real on-chain WalletNode holdings — the operating wallet (see
# wallet.OperatingWallet / README.md "Operating wallet") actually sitting on
# a chain, in a stable, RIGHT NOW. Distinct from the DEX balances above
# (an exchange account's equity) and from a WalletNode in the SOLVED graph
# (which only ever means "money in transit through this hop", never a
# standing balance) — this is the one place a WalletNode's balance is a real,
# persistent, queryable fact. Scoped for now to USDC/USDT on
# Arbitrum/BSC, per instruction — the same four (chain, stable) pairs
# stable_tokens.py already has verified contract addresses for.
def _wallet_targets() -> list[tuple]:
    # Built lazily (not at import time): needs `graph.structures.DEXes`,
    # which needs `src` on sys.path — already guaranteed once compass_test's
    # own __init__ has run, but not necessarily before, so a module-level
    # list built at import time would be a needless ordering hazard here.
    from graph.structures.DEXes import Chain, Stable

    return [
        (Chain.ARBITRUM, Stable.USDC),
        (Chain.ARBITRUM, Stable.USDT),
        (Chain.BSC, Stable.USDC),
        (Chain.BSC, Stable.USDT),
    ]


def get_wallet_balance_usd(chain, stable) -> WalletBalanceResult:
    """Real on-chain ERC-20 balanceOf for the operating wallet, on `chain` in
    `stable`. Needs only the wallet's PUBLIC address (config.
    OPERATING_WALLET_ADDRESS, or derived from the signing key if that isn't
    set) — a plain read, no private key material required to actually
    resolve it. Never raises — same contract as get_real_balance_usd."""
    from . import chain_ops, config
    from .wallet import OperatingWallet

    try:
        wallet = OperatingWallet(known_address=config.OPERATING_WALLET_ADDRESS)
        w3 = chain_ops.get_web3(chain)
        balance = chain_ops.get_stable_balance_usd(w3, chain, stable, wallet.address)
        return WalletBalanceResult(chain=chain.name, stable=stable.name, balanceUsd=balance, fetchedAt=time.time())
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        return WalletBalanceResult(chain=chain.name, stable=stable.name, balanceUsd=None, fetchedAt=time.time(), error=str(exc))


def list_wallet_balances() -> list[WalletBalanceResult]:
    return [get_wallet_balance_usd(chain, stable) for chain, stable in _wallet_targets()]
