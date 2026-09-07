"""Lighter — FAST withdrawal only (USDC, Arbitrum), by explicit instruction.
Deposit and the "secure" (on-chain contract) withdrawal path are NOT
implemented here — see build_deposit_tx below.

Signs via the same compiled Go signer binary the `lighter-sdk` PyPI package
vendors internally (see runners/lighter_signers/README.md) — loaded
directly with Python's built-in `ctypes`, NOT by depending on `lighter-sdk`
itself: that package's own metadata requires eth-account>=0.13.4, which
conflicts with web3==6.20.4's eth-account<0.13 and broke every other
connector in this repo when installed (2026-09-06, reverted). The exact
struct layouts / argtypes / call sequence below are ported faithfully from
that package's own lighter/signer_client.py (fetched 2026-09-06), not
guessed — see each function for what it mirrors.

A "fast withdraw" is actually an L2 TRANSFER (SignTransfer) to a special
"pool" account, not a distinct withdraw transaction type — the destination
L1 address rides in a 32-byte memo (20-byte address + 12 zero bytes). The
pool account index and transfer fee are both looked up live per-request
(GET /api/v1/fastwithdraw/info, GET /api/v1/transferFeeInfo), not
hardcoded. SignTransfer also returns a `messageToSign` string that needs a
SEPARATE plain personal_sign (EIP-191) from the account's own Ethereum
wallet, injected into the signed tx_info as `L1Sig` before submission —
ported from signer_client.py's __decode_and_sign_tx_info.

Credentials (all already in piggybank-arb/.env, used for trading there):
LIGHTER_ACCOUNT_INDEX, LIGHTER_API_KEY_INDEX, LIGHTER_API_KEY (Lighter's own
StarkEx-style key pair — unrelated to any Ethereum key, loaded into the
binary's internal state via CreateClient). The transfer's L1Sig ALSO needs
an Ethereum private key controlling the account: a live
GET /api/v1/account?by=index&value=<LIGHTER_ACCOUNT_INDEX> call (2026-09-06)
confirmed this account's l1_address is
0x477899D0e04E8b510ADA7EcD1Db22d1BdF3F1650 — the same operating wallet
already used everywhere else (compass_test.wallet.OperatingWallet /
COMPASS_TEST_WALLET_PRIVATE_KEY), so no new key material was needed.
"""

from __future__ import annotations

import ctypes
import json
import platform
import time
from pathlib import Path

import requests
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

from graph.structures.DEXes import Chain, Stable

from .. import config
from .base import DexConnector, WithdrawResult

_BASE_URL = "https://mainnet.zklighter.elliot.ai"
_CHAIN_ID = 304  # Lighter app-chain mainnet (300 = testnet) — NOT Arbitrum's own chain id.
_ROUTE_PERP = 0
_ASSET_ID_USDC = 3
_USDC_SCALE = 10**6
_SIGNERS_DIR = Path(__file__).resolve().parent / "lighter_signers"


class _SignedTxResponse(ctypes.Structure):
    _fields_ = [
        ("txType", ctypes.c_uint8),
        ("txInfo", ctypes.c_void_p),
        ("txHash", ctypes.c_void_p),
        ("messageToSign", ctypes.c_void_p),
        ("err", ctypes.c_void_p),
    ]


class _StrOrErr(ctypes.Structure):
    _fields_ = [("str", ctypes.c_void_p), ("err", ctypes.c_void_p)]


def _load_signer():
    """The vendored native signer (lighter_signers/README.md) — struct
    layouts and argtypes copied verbatim from lighter-sdk's own
    lighter/signer_client.py so the C ABI call is byte-for-byte what that
    package itself would make."""
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Darwin" and machine in ("arm64", "aarch64"):
        libname = "lighter-signer-darwin-arm64.dylib"
    else:
        raise RuntimeError(
            f"Lighter: no vendored signer binary for {system}/{machine} — only macOS ARM64 is "
            f"vendored here (see {_SIGNERS_DIR}/README.md to add another platform's binary)"
        )
    lib = ctypes.CDLL(str(_SIGNERS_DIR / libname))

    lib.CreateClient.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_longlong]
    lib.CreateClient.restype = ctypes.c_void_p

    lib.CheckClient.argtypes = [ctypes.c_int, ctypes.c_longlong]
    lib.CheckClient.restype = ctypes.c_void_p

    lib.CreateAuthToken.argtypes = [ctypes.c_longlong, ctypes.c_int, ctypes.c_longlong]
    lib.CreateAuthToken.restype = _StrOrErr

    # 10 params, NOT 11: the vendored .h header alongside this exact binary
    # (lighter_signers/lighter-signer-darwin-arm64.h) declares SignTransfer
    # with no separate skip_nonce field — that came from copying the newer
    # pip package's Python-level wrapper (which inserts skip_nonce before
    # nonce) instead of this binary's own real C signature. With the extra
    # arg, every parameter after `memo` was shifted one slot, so
    # api_key_index/account_index arrived as garbage
    # ("client is not created for apiKeyIndex: 14 accountIndex: 10" instead
    # of the real 10/732859) — confirmed live 2026-09-06, fixed by dropping
    # skip_nonce entirely.
    lib.SignTransfer.argtypes = [
        ctypes.c_longlong,
        ctypes.c_int16,
        ctypes.c_int8,
        ctypes.c_int8,
        ctypes.c_longlong,
        ctypes.c_longlong,
        ctypes.c_char_p,
        ctypes.c_longlong,
        ctypes.c_int,
        ctypes.c_longlong,
    ]
    lib.SignTransfer.restype = _SignedTxResponse

    # No Free() here on purpose: this vendored binary (an older build than
    # lighter-sdk's current signer_client.py/its .h header assume — checked
    # 2026-09-06 with `nm -gU`, it exports none of Free/GenerateAPIKey's
    # newer siblings like SignApproveIntegrator) doesn't export that symbol
    # at all; calling it raises. piggybank-arb's own koffi binding against
    # this exact same file never calls it either (confirmed by reading
    # signer.ts) — a small per-call string leak, acceptable for occasional
    # manual withdrawal testing rather than a long-running hot loop.
    return lib


def _decode(ptr) -> str | None:
    if not ptr:
        return None
    value = ctypes.cast(ptr, ctypes.c_char_p).value
    return value.decode("utf-8") if value is not None else None


class LighterConnector(DexConnector):
    name = "Lighter"
    supported_chains = frozenset({Chain.ARBITRUM})
    supported_stables = frozenset({Stable.USDC})

    def __init__(self):
        self._account_index = int(config.require_env("LIGHTER_ACCOUNT_INDEX"))
        self._api_key_index = int(config.require_env("LIGHTER_API_KEY_INDEX"))
        api_key = config.require_env("LIGHTER_API_KEY")
        self._eth_private_key = config.require_env("COMPASS_TEST_WALLET_PRIVATE_KEY")

        self._lib = _load_signer()
        key_hex = api_key[2:] if api_key.startswith("0x") else api_key
        err = _decode(
            self._lib.CreateClient(
                _BASE_URL.encode("utf-8"),
                key_hex.encode("utf-8"),
                _CHAIN_ID,
                self._api_key_index,
                self._account_index,
            )
        )
        if err is not None:
            raise RuntimeError(f"Lighter: CreateClient failed: {err}")

    def withdraw(self, chain: Chain, stable: Stable, amount_usd: float, to_address: str) -> WithdrawResult:
        check_err = _decode(self._lib.CheckClient(self._api_key_index, self._account_index))
        if check_err is not None:
            raise RuntimeError(f"Lighter: CheckClient failed: {check_err}")

        requestedAt = time.time()

        auth_result = self._lib.CreateAuthToken(int(time.time()) + 600, self._api_key_index, self._account_index)
        auth_err = _decode(auth_result.err)
        if auth_err is not None:
            raise RuntimeError(f"Lighter: CreateAuthToken failed: {auth_err}")
        auth_token = _decode(auth_result.str)
        headers = {"Authorization": auth_token}

        pool_response = requests.get(
            f"{_BASE_URL}/api/v1/fastwithdraw/info",
            params={"account_index": self._account_index},
            headers=headers,
            timeout=15,
        )
        pool_response.raise_for_status()
        pool_info = pool_response.json()
        if pool_info.get("code") != 200:
            raise RuntimeError(f"Lighter: fastwithdraw/info -> {pool_info}")
        to_account_index = pool_info["to_account_index"]

        fee_response = requests.get(
            f"{_BASE_URL}/api/v1/transferFeeInfo",
            params={"account_index": self._account_index, "to_account_index": to_account_index},
            headers=headers,
            timeout=15,
        )
        fee_response.raise_for_status()
        fee_info = fee_response.json()
        if fee_info.get("code") != 200:
            raise RuntimeError(f"Lighter: transferFeeInfo -> {fee_info}")
        fee_usdc_units = fee_info["transfer_fee_usdc"]  # int, already in USDC base units

        addr_bytes = Web3.to_bytes(hexstr=to_address)
        if len(addr_bytes) != 20:
            raise RuntimeError(f"Lighter: destination address {to_address!r} is not a 20-byte EVM address")
        memo_hex = (addr_bytes + b"\x00" * 12).hex()

        nonce_response = requests.get(
            f"{_BASE_URL}/api/v1/nextNonce",
            params={"account_index": self._account_index, "api_key_index": self._api_key_index},
            timeout=15,
        )
        nonce_response.raise_for_status()
        nonce = nonce_response.json()["nonce"]

        sign_result = self._lib.SignTransfer(
            to_account_index,
            _ASSET_ID_USDC,
            _ROUTE_PERP,
            _ROUTE_PERP,
            int(round(amount_usd * _USDC_SCALE)),
            fee_usdc_units,
            memo_hex.encode("utf-8"),
            nonce,
            self._api_key_index,
            self._account_index,
        )
        sign_err = _decode(sign_result.err)
        tx_info_str = _decode(sign_result.txInfo)
        tx_hash = _decode(sign_result.txHash)
        message_to_sign = _decode(sign_result.messageToSign)
        if sign_err is not None:
            raise RuntimeError(f"Lighter: SignTransfer failed: {sign_err}")

        # The L2 signature above covers only the transfer itself; Lighter
        # separately requires the account's own L1 wallet to personal_sign
        # a plain message the binary hands back (`messageToSign`), proving
        # the request really comes from the account owner — ported from
        # lighter-sdk's __decode_and_sign_tx_info, not guessed. `.hex()`
        # rather than the newer `.to_0x_hex()` (which signer_client.py
        # itself uses): hexbytes==0.3.1 (pinned for web3 compatibility,
        # see requirements.txt) predates that method.
        account = Account.from_key(self._eth_private_key)
        l1_signature = account.sign_message(encode_defunct(text=message_to_sign))
        tx_info = json.loads(tx_info_str)
        tx_info["L1Sig"] = "0x" + bytes(l1_signature.signature).hex()

        submit_response = requests.post(
            f"{_BASE_URL}/api/v1/fastwithdraw",
            data={"tx_info": json.dumps(tx_info), "to_address": to_address},
            headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        submit_response.raise_for_status()
        result = submit_response.json()
        if result.get("code") != 200:
            raise RuntimeError(f"Lighter: fastwithdraw -> {result}")

        return WithdrawResult(
            externalId=tx_hash,
            requestedAt=requestedAt,
            acceptedAt=time.time(),
            amountRequestedUsd=amount_usd,
            quotedFeeUsd=fee_usdc_units / _USDC_SCALE,
        )

    def build_deposit_tx(self, w3: Web3, from_address: str, chain: Chain, stable: Stable, amount_usd: float) -> dict:
        raise NotImplementedError(
            "Lighter: deposit was explicitly excluded — only the fast withdrawal path was requested"
        )

    def poll_balance_usd(self, stable: Stable) -> float:
        # Same read as balances.py::_lighter — a plain public GET, no
        # signing needed. Kept here too (rather than imported) because this
        # is the ABC's own contract, used by the executor to detect a
        # deposit landing — irrelevant to withdraw itself, but required to
        # satisfy DexConnector.
        response = requests.get(
            f"{_BASE_URL}/api/v1/account",
            params={"by": "index", "value": str(self._account_index)},
            timeout=15,
        )
        response.raise_for_status()
        accounts = response.json().get("accounts", [])
        return sum(float(a.get("collateral", 0.0)) for a in accounts)
