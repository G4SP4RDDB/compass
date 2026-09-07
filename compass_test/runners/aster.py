"""Aster — deposit is a documented on-chain vault contract call; withdraw
needs Aster's own REST auth (V3 "Pro API-Key", an EIP-712 signature over the
query string by the agent key — see aster_signing.py, verified live
2026-09-07) PLUS a separate EIP-712 wallet signature authorizing the
withdrawal itself.

Source: https://github.com/asterdex/api-docs/blob/master/demo/aster-deposit-withdrawal.md
(fetched verbatim 2026-09-04). Cross-checked the vault contract addresses
against two independent fetches of that file (summarized + raw) — they
matched byte-for-byte — but this is still the single highest-consequence
hardcoded constant in this whole tool: RE-VERIFY on Arbiscan/BscScan/
Etherscan (search "Aster" vault/deposit contract) before the first live
Aster deposit. See README.md "Verify before live".

    VAULT (depositFor):  Ethereum 0x604DD02d620633Ae427888d41bfd15e38483736E
                          BSC      0x128463A60784c4D3f46c23Af3f65Ed859Ba87974
                          Arbitrum 0x9E36CB86a159d479cEd94Fa05036f235Ac40E1d5
    depositFor(address currency, address forAddress, uint256 amount, uint256 broker)

`forAddress` is an explicit parameter (not `msg.sender`) — Aster credits
whichever account we name there regardless of which wallet actually pays the
gas, so the deposit is credited to ASTER_USER (piggybank-arb's real Aster
account) even though the tx may be signed by a different operating wallet.

`broker` = 1000 is documented for SPOT deposits only; Aster's perp/futures
broker code is NOT documented in the source above. Defaults to 1000 with a
loud warning — override via ASTER_DEPOSIT_BROKER once you've confirmed the
correct perp code with Aster, rather than trusting this guess on a live run.

The withdrawal EIP-712 "Action" struct's field names were re-confirmed
2026-09-05 against the raw doc (not the summarized first pass that guessed
"destinationChain"/"asterChain" were camelCase and the space in "destination
Chain" a docs-extraction artifact) — the space IS literal, Aster's backend
expects "destination Chain" and "aster chain" verbatim (see
_withdraw_action_signature). An incorrect struct FAILS CLOSED (Aster's
backend rejects a bad signature, funds never move) and is indistinguishable
from the outer v3 auth failure this connector also had, since Aster reports
both under the same generic -1000 "Signature check failed" — which is
exactly why the first fix (query string vs body) looked like it hadn't
worked when tested live.

Aster's own dedicated env vars (ASTER_USER / ASTER_SIGNER / ASTER_PRIVATE_KEY,
already in piggybank-arb/.env for trading) are reused directly here rather
than the generic operating wallet — they are unambiguous for this one DEX,
unlike the cross-DEX operating wallet key (see README.md "Operating wallet").

Re-fetched 2026-09-07 from docs.asterdex.com's own withdrawal doc (same
underlying asterdex/api-docs source, current version) while chasing a live
withdraw signature failure with keys already confirmed correct. Everything
above was re-checked field-by-field against it and held up EXCEPT one thing
the doc states only under the Solana Ed25519 example but which applies
equally here: "Important: Strip trailing zeros from Amount and Fee (e.g.,
1.20 -> 1.2)" — the backend recomputes its own canonical amount/fee string
when it re-verifies a signature, so a client that signs a zero-padded string
(this file's `f"{x:.6f}"`, e.g. "10.500000") produces a digest the backend
never reproduces. See `_trimmed_decimal`.
"""

from __future__ import annotations

import os
import time

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

from connectors.chain_metadata import get_metadata
from graph.structures.DEXes import Chain, Stable

from .. import chain_ops, config
from ..aster_signing import v3_signed_query
from ..wallet import OperatingWallet
from .base import DexConnector, WithdrawResult

_FUTURES_BASE_URL = "https://fapi.asterdex.com"
_PUBLIC_BASE_URL = "https://www.asterdex.com/bapi/futures/v1/public"

_VAULT_ADDRESS_BY_CHAIN = {
    Chain.BSC: "0x128463A60784c4D3f46c23Af3f65Ed859Ba87974",
    Chain.ARBITRUM: "0x9E36CB86a159d479cEd94Fa05036f235Ac40E1d5",
}

_DEPOSIT_VAULT_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "currency", "type": "address"},
            {"name": "forAddress", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "broker", "type": "uint256"},
        ],
        "name": "depositFor",
        "outputs": [],
        "type": "function",
    }
]

_ASSET_BY_STABLE = {Stable.USDT: "USDT", Stable.USDC: "USDC"}
_DEFAULT_SPOT_BROKER = 1000

# The withdrawal EIP-712 domain's chainId is fixed at Aster's own chain (BSC),
# regardless of which chain the withdrawal actually lands on — confirmed
# 2026-09-05 against asterdex/api-docs' aster-deposit-withdrawal.md.
_EIP712_DOMAIN_CHAIN_ID = 56


class AsterConnector(DexConnector):
    name = "Aster"
    supported_chains = frozenset({Chain.BSC})
    supported_stables = frozenset({Stable.USDT})

    def __init__(self):
        self._user = config.require_env("ASTER_USER")
        self._signer = config.require_env("ASTER_SIGNER")
        self._signer_key = config.require_env("ASTER_PRIVATE_KEY")
        self._signer_account = Account.from_key(self._signer_key)
        # The withdraw Action's `userSignature` (below) is a DIFFERENT
        # signature from the outer v3 request auth above: sentinelBackend's
        # own Aster README documents a registered agent's permissions as
        # canSpotTrade/canPerpTrade/canWithdraw INDEPENDENTLY, and its own
        # registered agent has canWithdraw=false — a delegated trading agent
        # is not generally trusted to authorize a withdrawal. Confirmed
        # 2026-09-06: ASTER_PRIVATE_KEY derives ASTER_SIGNER (the agent),
        # while COMPASS_TEST_WALLET_PRIVATE_KEY derives ASTER_USER (the main
        # account) exactly — the operating wallet key already in `.env` for
        # signing on-chain deposits IS this account's own key. Lazy, same as
        # everywhere else OperatingWallet is used: reads no key material
        # unless a withdraw is actually attempted.
        self._operating_wallet = OperatingWallet()
        self._broker = int(os.getenv("ASTER_DEPOSIT_BROKER", str(_DEFAULT_SPOT_BROKER)))
        self._session = requests.Session()

    def _v3_auth_query(self, extra: dict) -> str:
        """V3 Pro API-Key request auth — see aster_signing.py."""
        return v3_signed_query(self._user, self._signer, self._signer_key, extra)

    @staticmethod
    def _trimmed_decimal(value: float, decimals: int = 6) -> str:
        # docs.asterdex.com's withdrawal doc (fetched 2026-09-07), under the
        # Solana Ed25519 message format, states in bold: "Important: Strip
        # trailing zeros from Amount and Fee (e.g., 1.20 -> 1.2)" — and gives
        # a worked example (Amount=1.2, not 1.20). That instruction exists
        # because the backend recomputes its OWN canonical amount/fee string
        # from the submitted numeric value when it re-verifies a signature;
        # a client that signs a differently-formatted string (zero-padded)
        # produces a digest the backend never reproduces, which is
        # indistinguishable from every other cause of Aster's generic -1000
        # "Signature check failed". Nothing in the EVM section repeats the
        # warning, but the Action struct's `amount`/`fee` are the same
        # backend-recomputed "string" fields under the hood, so the fix
        # applies here too — `f"{x:.6f}"` (the previous formatting, e.g.
        # "10.500000") is exactly the padded shape the doc warns against.
        text = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
        return text or "0"

    def _withdraw_action_signature(self, chain: Chain, coin: str, amount_usd: float, fee_usd: float, to_address: str) -> tuple[dict, str]:
        # ms * 1000, NOT a true nanosecond timestamp. The doc calls this a
        # "Nanosecond timestamp" in the param table and "milliseconds
        # multiplied by 1000" in the struct description; its own Solana
        # sample settles which it means: `Date.now() * 1000; // nanosecond
        # timestamp`, example value 1773741793787000 (16 digits). A real
        # ns value (19 digits) is 1000x outside the backend's window and is
        # rejected live with -1000 "Your signature has expired" (2026-09-07).
        # An earlier ms*1000 attempt was judged a regression while the outer
        # V3 auth was still broken, so that test was inconclusive.
        nonce = int(time.time() * 1000) * 1000
        # Field names have LITERAL spaces ("destination Chain", "aster
        # chain") — confirmed 2026-09-05 against the raw doc
        # (raw.githubusercontent.com/asterdex/api-docs/master/demo/
        # aster-deposit-withdrawal.md), not a docs-extraction artifact as
        # first guessed. Signing with the camelCase guess produces a
        # different EIP-712 digest than Aster's backend computes, so
        # userSignature never validates — indistinguishable from the outer
        # v3 auth failure fixed earlier, since Aster reports both under the
        # same generic -1000 "Signature check failed".
        message = {
            "type": "Withdraw",
            "destination": Web3.to_checksum_address(to_address),
            "destination Chain": _CHAIN_NAME.get(chain, chain.name),
            "token": coin,
            "amount": self._trimmed_decimal(amount_usd),
            "fee": self._trimmed_decimal(fee_usd),
            "nonce": nonce,
            "aster chain": "Mainnet",
        }
        # Also confirmed fixed at 56 (BSC) regardless of destination chain —
        # NOT get_metadata(chain).chain_id, which happened to coincide with
        # this for BSC (the only chain this connector supports today) but
        # would have been wrong the moment Arbitrum support was added.
        # `verifyingContract` re-added 2026-09-06: the raw doc's "EVM
        # Withdraw Signature" section lists FOUR domain fields, not three —
        # {"name": "Aster", "version": "1", "chainId": 56,
        # "verifyingContract": "0x0000...0000"} — omitting it changes the
        # EIP712Domain type array eth_account derives from this dict's keys,
        # which changes the signed digest regardless of which key signs it.
        # This, not the signer identity, is the more likely cause of a
        # -1000 that persisted even after switching to the main wallet's key.
        domain = {
            "name": "Aster",
            "version": "1",
            "chainId": _EIP712_DOMAIN_CHAIN_ID,
            "verifyingContract": "0x0000000000000000000000000000000000000000",
        }
        # Explicit documented types (not inferred): destination is an
        # `address`, nonce a `uint256`, everything else `string` — see
        # module docstring for the source.
        field_types = {
            "type": "string",
            "destination": "address",
            "destination Chain": "string",
            "token": "string",
            "amount": "string",
            "fee": "string",
            "nonce": "uint256",
            "aster chain": "string",
        }
        signable = encode_typed_data(
            domain_data=domain,
            message_types={"Action": [{"name": k, "type": field_types[k]} for k in message]},
            message_data=message,
        )
        # Signed by the MAIN account wallet, NOT self._signer_account (the
        # delegated trading agent) — see __init__.
        signed = self._operating_wallet.account.sign_message(signable)
        return message, signed.signature.hex()

    def _estimate_withdraw_fee_usd(self, chain: Chain, coin: str) -> float:
        response = self._session.get(
            f"{_PUBLIC_BASE_URL}/future/aster/estimate-withdraw-fee",
            params={"chainId": get_metadata(chain).chain_id, "network": "EVM", "currency": coin, "accountType": "perp"},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        payload = data.get("data", data)
        # `gasCost` is the documented field ("Estimated withdrawal fee in
        # token units") and is what the withdraw Action's `fee` must carry.
        # An earlier version preferred `gasUsdValue` because the two happened
        # to be equal that day; live 2026-09-07 they were not (gasCost 0.11,
        # gasUsdValue 0.10, and the signed user-withdraw-info endpoint's
        # withdrawFee agreed with gasCost) — signing the USD figure would
        # bake a fee the backend does not expect into the signature. The
        # value is also reported as `quotedFeeUsd`, which is exact only for
        # a $1-pegged stable; that is all this connector supports.
        for key in ("gasCost", "gasUsdValue", "fee", "withdrawFee", "estimatedFee"):
            if key in payload and payload[key] is not None:
                return float(payload[key])
        raise RuntimeError(f"Aster: could not find a fee field in estimate-withdraw-fee response: {payload!r}")

    def withdraw(self, chain: Chain, stable: Stable, amount_usd: float, to_address: str) -> WithdrawResult:
        coin = _ASSET_BY_STABLE[stable]
        fee_usd = self._estimate_withdraw_fee_usd(chain, coin)
        message, signature = self._withdraw_action_signature(chain, coin, amount_usd, fee_usd, to_address)

        requestedAt = time.time()
        query = self._v3_auth_query(
            {
                "amount": message["amount"],
                "chainId": get_metadata(chain).chain_id,
                "asset": coin,
                "fee": message["fee"],
                "receiver": message["destination"],
                "userNonce": message["nonce"],
                "userSignature": signature,
            }
        )
        # Signed params go in the URL QUERY STRING with an empty body — the
        # V3 doc's own `send_by_url` sample POSTs exactly this way (see
        # aster_signing.py for the source). Its `send_by_body` alternative
        # would need the signature inside the form data instead; mixing the
        # two (signing the query, sending a body) fails closed with -1000.
        response = self._session.post(
            f"{_FUTURES_BASE_URL}/fapi/v3/aster/user-withdraw?{query}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        if not response.ok:
            raise RuntimeError(f"Aster withdraw -> {response.status_code}: {response.text}")
        data = response.json()

        return WithdrawResult(
            externalId=str(data.get("withdrawId", data.get("hash", ""))),
            requestedAt=requestedAt,
            acceptedAt=time.time(),
            amountRequestedUsd=amount_usd,
            quotedFeeUsd=fee_usd,
        )

    def build_deposit_tx(self, w3: Web3, from_address: str, chain: Chain, stable: Stable, amount_usd: float) -> dict:
        vault_address = _VAULT_ADDRESS_BY_CHAIN.get(chain)
        if vault_address is None:
            raise RuntimeError(f"Aster: no vault contract address known for {chain.name}")
        from connectors.stable_tokens import get_stable_token_address

        token_address = Web3.to_checksum_address(get_stable_token_address(chain, stable))
        vault = w3.eth.contract(address=Web3.to_checksum_address(vault_address), abi=_DEPOSIT_VAULT_ABI)
        call = vault.functions.depositFor(
            token_address,
            Web3.to_checksum_address(self._user),
            chain_ops.usd_to_token_units(chain, stable, amount_usd),
            self._broker,
        )
        return chain_ops.build_contract_call_tx(w3, chain, from_address, call)

    def poll_balance_usd(self, stable: Stable) -> float:
        # GET /fapi/v3/account — proven live 2026-09-04 (see
        # balances.py::_aster, same query/endpoint), unlike the previously
        # used /fapi/v3/aster/user-withdraw-info which was never actually
        # confirmed working. Sums `marginBalance` across every asset row
        # with a nonzero `walletBalance` — same convention as balances.py,
        # not filtered to just `stable`'s asset (Aster's account balance
        # isn't usefully split per-stable for this purpose).
        query = self._v3_auth_query({})
        response = self._session.get(
            f"{_FUTURES_BASE_URL}/fapi/v3/account?{query}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        if not response.ok:
            raise RuntimeError(f"Aster account -> {response.status_code}: {response.text}")
        data = response.json()
        return sum(
            float(a.get("marginBalance") or 0)
            for a in data.get("assets", [])
            if float(a.get("walletBalance") or 0) != 0
        )


_CHAIN_NAME = {Chain.BSC: "BSC", Chain.ARBITRUM: "Arbitrum"}
