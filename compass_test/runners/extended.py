"""Extended — both withdraw and deposit are bridged via Rhino.fi to/from
Arbitrum.

Extended is a StarkEx-derived perp exchange (formerly "X10"); every write
operation (orders, transfers, withdrawals) is authorized by a STARK-curve
signature, never a plain API-key call — see api.docs.extended.exchange
"Authentication" (fetched verbatim 2026-09-06). Reads (balance, account
info, assets, bridge quotes) only need EXTENDED_API_KEY (X-Api-Key header),
already used by balances.py::_extended.

Withdraw is a 4-step Rhino.fi bridge flow, Extended's own documented path
for withdrawing to an EVM chain (not something reverse-engineered):
  1. GET  /user/bridge/config          -> bridge contract per chain
  2. GET  /user/bridge/quote            -> quote id + bridge fee, for
                                           chainIn=STRK, chainOut=<EVM chain>
  3. POST /user/bridge/quote?id=<id>    -> commits the quote
  4. POST /user/withdrawal              -> the STARK-signed withdrawal
                                           request itself; Rhino.fi relays
                                           it to the destination chain

Step 4's `settlement` object is signed with the account's STARK private key
via fast_stark_crypto (Extended's own signing library — see
runners/extended_signers/README.md for why that runs in a separate venv).
The message hash construction (get_withdrawal_msg_hash) and its exact
arguments mirror x10-python-trading-starknet's
x10/signing/withdrawal_object.py::create_withdrawal_object byte for byte
(github.com/x10xchange/python_sdk, starknet branch, fetched 2026-09-06) —
not re-derived, since a wrong STARK message hash is a silent wrong
signature, not a loud error.

`recipient` in that settlement is the account's OWN `bridgeStarknetAddress`
(from GET /user/account/info) for an EVM-chain withdrawal — confirmed
against the SDK's own account_module.py::withdraw, not guessed: Rhino.fi's
bridge is what actually routes funds from there to the EVM chain named in
the quote. Neither the quote nor the withdrawal request names a destination
EVM ADDRESS at all — "Withdrawals are only permitted to wallets that are
linked to the authorised account" (docs, verbatim), so the destination is
whatever EVM wallet was linked at account creation, not something this
call can point elsewhere. `to_address` (the operating wallet) is therefore
NOT sent anywhere here; if it isn't the linked wallet, Extended's own server
rejects the withdrawal rather than silently misdirecting it — a real gap
versus Aster/Hyperliquid (where this connector can check the signer/account
match itself), documented here rather than silently worked around.

Deposit is the symmetric flow, quote direction reversed (chainIn=<EVM
chain>, chainOut=STRK), and its final step is an on-chain call instead of a
STARK-signed request: `depositWithId(token, amount, commitmentId)` on
Rhino.fi's OWN bridge contract (the same address `/user/bridge/config`
already returns) on the source chain — `commitmentId` is the committed
quote's id, reinterpreted as a uint256
(`int(quoteId, 16)`, confirmed against Rhino.fi's own reference
implementation, see below). That contract pulls funds via `transferFrom`
(not a plain `transfer`), so it needs an ERC-20 `approve` first if the
operating wallet's allowance to it isn't already large enough —
build_deposit_tx checks this and returns the `approve` tx instead of the
real deposit when that's the case, meaning a first-ever deposit takes TWO
calls: run this hop once to approve, again (after that confirms) to
actually deposit. Every deposit after the first covered by that allowance
is a single call.

Rhino.fi's bridge contract is a genuine THIRD-PARTY contract (not
Extended's own), so its ABI came from Rhino.fi's own docs
(docs.rhino.fi/contracts/evm) — `depositWithId(address token, uint256
amount, uint256 commitmentId)` — cross-checked against the actual deployed
source on GitHub
(github.com/rhinofi/contracts_public/blob/master/bridge-deposit/DVFDepositContract.sol,
fetched verbatim 2026-09-06): a plain `IERC20Upgradeable.safeTransferFrom`
+ an event emit, nothing more exotic, matching the ABI exactly.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from decimal import Decimal
from pathlib import Path

import requests
from web3 import Web3

from graph.structures.DEXes import Chain, Stable

from .. import chain_ops, config
from .base import DexConnector, WithdrawResult

_BASE_URL = "https://api.starknet.extended.exchange/api/v1"
_USER_AGENT = "compass_test/1.0"

_SIGNERS_DIR = Path(__file__).resolve().parent / "extended_signers"
_SIGN_VENV_PYTHON = _SIGNERS_DIR / ".venv39" / "bin" / "python"
_SIGN_SCRIPT = _SIGNERS_DIR / "sign_withdrawal.py"

# STARK signatures are only valid until this expiration — 15 days out, the
# same buffer x10-python-trading-starknet's own SDK uses
# (SETTLEMENT_EXPIRATION_BUFFER_DAYS), not a value invented here.
_SETTLEMENT_EXPIRATION_BUFFER_SECONDS = 15 * 24 * 60 * 60

# github.com/x10xchange/python_sdk, starknet branch, x10/config.py::MAINNET_CONFIG.
_MAINNET_STARKNET_DOMAIN = {"name": "Perpetuals", "version": "v0", "chain_id": "SN_MAIN", "revision": "1"}

# Verified live 2026-09-06 against GET /user/bridge/config — Extended's own
# code for Arbitrum ("BNB" for BSC there, notably not "BSC", in case this
# table is ever extended).
_CHAIN_CODE = {Chain.ARBITRUM: "ARB"}

# Rhino.fi's bridge contract — docs.rhino.fi/contracts/evm, cross-checked
# against the real deployed source (github.com/rhinofi/contracts_public,
# bridge-deposit/DVFDepositContract.sol, fetched verbatim 2026-09-06).
_BRIDGE_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "token", "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
            {"internalType": "uint256", "name": "commitmentId", "type": "uint256"},
        ],
        "name": "depositWithId",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


class ExtendedConnector(DexConnector):
    name = "Extended"
    supported_chains = frozenset({Chain.ARBITRUM})
    supported_stables = frozenset({Stable.USDC})

    def __init__(self):
        self._api_key = config.require_env("EXTENDED_API_KEY")
        self._private_key_hex = config.require_env("EXTENDED_STARK_PRIVATE_KEY")
        self._session = requests.Session()
        self._session.headers.update({"X-Api-Key": self._api_key, "User-Agent": _USER_AGENT})

    def _get(self, path: str, params: dict | None = None) -> dict:
        response = self._session.get(f"{_BASE_URL}{path}", params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "OK":
            raise RuntimeError(f"Extended GET {path} -> {data}")
        return data["data"]

    def _post(self, path: str, params: dict | None = None, json_body: dict | None = None):
        # Not every write returns a `data` payload — the quote-commit
        # endpoint's real response is just `{"status": "OK"}` (confirmed
        # live 2026-09-06), no `data` key at all, unlike every read here.
        response = self._session.post(f"{_BASE_URL}{path}", params=params, json=json_body, timeout=15)
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "OK":
            raise RuntimeError(f"Extended POST {path} -> {data}")
        return data.get("data")

    def _collateral_asset(self) -> dict:
        assets = self._get("/info/assets", {"collateral": "true"})
        if not assets:
            raise RuntimeError("Extended: no collateral asset returned by /info/assets")
        return assets[0]

    def _sign_withdrawal(self, account: dict, asset: dict, amount_stark_units: int, salt: int, expiration: int) -> dict:
        # STARK signing itself runs in a SEPARATE Python 3.9 venv (see
        # extended_signers/README.md) — fast_stark_crypto has no Python 3.14
        # wheel and refuses to build for it at all. The private key crosses
        # this boundary on stdin only, never argv (never in `ps`), never
        # logged.
        payload = json.dumps(
            {
                "recipient_hex": account["bridgeStarknetAddress"],
                "position_id": int(account["l2Vault"]),
                "collateral_id_hex": asset["starkexId"],
                "amount": amount_stark_units,
                "expiration": expiration,
                "salt": salt,
                "public_key_hex": account["l2Key"],
                "private_key_hex": self._private_key_hex,
                "domain_name": _MAINNET_STARKNET_DOMAIN["name"],
                "domain_version": _MAINNET_STARKNET_DOMAIN["version"],
                "domain_chain_id": _MAINNET_STARKNET_DOMAIN["chain_id"],
                "domain_revision": _MAINNET_STARKNET_DOMAIN["revision"],
            }
        )
        result = subprocess.run(
            [str(_SIGN_VENV_PYTHON), str(_SIGN_SCRIPT)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Extended: STARK signing subprocess failed: {result.stderr}")
        return json.loads(result.stdout)

    def withdraw(self, chain: Chain, stable: Stable, amount_usd: float, to_address: str) -> WithdrawResult:
        chain_code = _CHAIN_CODE.get(chain)
        if chain_code is None:
            raise RuntimeError(f"Extended: no bridge chain code known for {chain.name}")

        account = self._get("/user/account/info")
        asset = self._collateral_asset()

        quote = self._get(
            "/user/bridge/quote",
            {"chainIn": "STRK", "chainOut": chain_code, "amount": amount_usd, "asset": "USD"},
        )
        fee_usd = float(quote["fee"])
        if fee_usd >= amount_usd:
            raise RuntimeError(
                f"Extended: bridge fee (${fee_usd}) would consume the entire ${amount_usd} withdrawal or "
                "more — refusing rather than send it and lose the difference."
            )
        self._post("/user/bridge/quote", {"id": quote["id"]})

        salt = int.from_bytes(os.urandom(4), "big")  # uint32, same range as the SDK's own generate_nonce()
        expiration = int(time.time()) + _SETTLEMENT_EXPIRATION_BUFFER_SECONDS
        stark_amount = round(Decimal(str(amount_usd)) * int(asset["starkexResolution"]))

        signature = self._sign_withdrawal(account, asset, stark_amount, salt, expiration)

        requestedAt = time.time()
        result = self._post(
            "/user/withdrawal",
            json_body={
                "accountId": account["accountId"],
                "amount": str(amount_usd),
                "chainId": chain_code,
                "asset": "USD",
                "quoteId": quote["id"],
                "settlement": {
                    "recipient": account["bridgeStarknetAddress"],
                    "positionId": int(account["l2Vault"]),
                    "collateralId": asset["starkexId"],
                    "amount": stark_amount,
                    "expiration": {"seconds": expiration},
                    "salt": salt,
                    "signature": signature,
                },
            },
        )
        return WithdrawResult(
            externalId=str(result),
            requestedAt=requestedAt,
            acceptedAt=time.time(),
            amountRequestedUsd=amount_usd,
            quotedFeeUsd=fee_usd,
        )

    def build_deposit_tx(self, w3: Web3, from_address: str, chain: Chain, stable: Stable, amount_usd: float) -> dict:
        chain_code = _CHAIN_CODE.get(chain)
        if chain_code is None:
            raise RuntimeError(f"Extended: no bridge chain code known for {chain.name}")

        bridge_config = self._get("/user/bridge/config")
        bridge_address = next((c["contractAddress"] for c in bridge_config["chains"] if c["chain"] == chain_code), None)
        if bridge_address is None:
            raise RuntimeError(f"Extended: no bridge contract address returned for chain {chain_code}")
        bridge_address = Web3.to_checksum_address(bridge_address)

        token_units = chain_ops.usd_to_token_units(chain, stable, amount_usd)
        allowance = chain_ops.get_stable_allowance(w3, chain, stable, from_address, bridge_address)
        if allowance < token_units:
            # Pull-based deposit (transferFrom) — needs approval FIRST. This
            # returns the approve() tx instead of the real deposit; running
            # this hop again after it confirms will take this branch's
            # `else` and return the actual depositWithId tx. A live run
            # during this step spends real (necessary) gas but does NOT
            # credit Extended — the executor's balance-poll will correctly
            # time out and report "unconfirmed", which is accurate: nothing
            # was deposited yet, only approved.
            return chain_ops.build_erc20_approve_tx(w3, chain, stable, from_address, bridge_address, amount_usd)

        quote = self._get(
            "/user/bridge/quote",
            {"chainIn": chain_code, "chainOut": "STRK", "amount": amount_usd, "asset": "USD"},
        )
        fee_usd = float(quote["fee"])
        if fee_usd >= amount_usd:
            raise RuntimeError(
                f"Extended: bridge fee (${fee_usd}) would consume the entire ${amount_usd} deposit or more "
                "— refusing rather than send it and lose the difference."
            )
        self._post("/user/bridge/quote", {"id": quote["id"]})

        token_address = Web3.to_checksum_address(chain_ops.erc20_contract(w3, chain, stable).address)
        bridge_contract = w3.eth.contract(address=bridge_address, abi=_BRIDGE_ABI)
        # commitmentId is the committed quote's id, reinterpreted as a
        # uint256 — confirmed against Rhino.fi's own reference
        # implementation (docs.rhino.fi/contracts/evm: `BigInt('0x' +
        # commitmentId)`), not guessed.
        commitment_id = int(quote["id"], 16)
        call = bridge_contract.functions.depositWithId(token_address, token_units, commitment_id)
        return chain_ops.build_contract_call_tx(w3, chain, from_address, call)

    def poll_balance_usd(self, stable: Stable) -> float:
        # Same read as balances.py::_extended (a 404 there means a real zero
        # balance, documented behavior, not an error) — kept here directly
        # rather than imported, same pattern as every other connector's
        # poll_balance_usd.
        response = self._session.get(f"{_BASE_URL}/user/balance", timeout=15)
        if response.status_code == 404:
            return 0.0
        response.raise_for_status()
        return float(response.json()["data"]["equity"])
