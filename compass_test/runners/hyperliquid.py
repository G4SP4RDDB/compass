"""Hyperliquid — deposit is a plain USDC transfer to Hyperliquid's own
Arbitrum bridge contract (a fixed, audited, publicly documented address —
not a per-account address copied off a CEX deposit screen like MEXC's);
withdraw is a single EIP-712-signed "withdraw3" action submitted to the
exchange endpoint, no separate on-chain transaction needed (Hyperliquid's own
validators execute the withdrawal from the bridge on the user's behalf).

Source: https://hyperliquid.gitbook.io/hyperliquid-docs/hypercore/usdc and
.../for-developers/api/exchange-endpoint (fetched verbatim 2026-09-06).

    BRIDGE (legacy — still what live deposits/withdrawals use; CCTP is
    documented as the newer preferred path but isn't a plain-ERC20-transfer
    flow, see the USDC doc page):
      Arbitrum 0x2Df1c51e09aecf9cacb7bc98cb1742757f163dF7
      (https://arbiscan.io/address/0x2df1c51e09aecf9cacb7bc98cb1742757f163df7,
      source: https://github.com/hyperliquid-dex/contracts/blob/master/Bridge2.sol)

Deposit is a PLAIN ERC-20 transfer of USDC to that address — "The user sends
native USDC to the bridge, and it is credited to the account that sent it in
less than 1 minute" (docs, verbatim). **Minimum deposit is 5 USDC — anything
below that is silently lost, not credited or returned** (docs, verbatim).
Credited to whichever address SENDS the transfer — unlike Aster's
`depositFor`, there is no separate "credit this other account" parameter —
so this only works if the operating wallet's own on-chain address IS the
account HYPERLIQUID_WALLET_ADDRESS trades from; build_deposit_tx refuses
outright otherwise rather than silently crediting the wrong account.

Withdraw is a wallet-signed EIP-712 "Withdraw" action — a "user-signed
action", NOT the L1 order-signing scheme orders/cancels use (see
for-developers/api/signing.md: two distinct schemes, sign_l1_action vs
sign_user_signed_action, in the Python SDK). The signer's address IS the
account withdrawn from (no vaultAddress/forAddress override for a plain
withdrawal), so this signs with the OPERATING WALLET only and refuses
outright if that wallet's address doesn't match HYPERLIQUID_WALLET_ADDRESS —
signing with a mismatched key would attempt (and fail, or withdraw from an
unintended account) rather than move funds for the account this connector's
balance reads actually check. Hyperliquid's piggybank-arb trading credentials
(HYPERLIQUID_API_KEY/HYPERLIQUID_API_ADDRESS) are a delegated API/agent
wallet — same "trading key, not a general wallet" situation as Aster's
ASTER_SIGNER, and deliberately NOT used here; see runners/aster.py and
README.md "Operating wallet".

Docs' own signing note (for-developers/api/signing.md): "recommended to
lowercase any address before signing and sending" — addresses in the signed
message and the action payload are lowercased here, unlike Aster's
checksummed style, because that's this venue's documented convention rather
than a stylistic choice.
"""

from __future__ import annotations

import time

import requests
from eth_account.messages import encode_typed_data
from web3 import Web3

from graph.structures.DEXes import Chain, Stable

from .. import chain_ops, config
from ..wallet import OperatingWallet
from .base import DexConnector, WithdrawResult

_EXCHANGE_URL = "https://api.hyperliquid.xyz/exchange"
_INFO_URL = "https://api.hyperliquid.xyz/info"

_BRIDGE_ADDRESS = "0x2Df1c51e09aecf9cacb7bc98cb1742757f163dF7"

# "If you send an amount less than this, it will not be credited and be
# lost forever." (docs, verbatim) — checked here so a caller with the wrong
# --amount fails BEFORE broadcasting, not after.
_MIN_DEPOSIT_USD = 5.0

# The docs' own "$1 fee for withdrawing at the time of this writing" is a
# snapshot, not a live-queryable value — no endpoint returns the CURRENT fee
# up front, it's only ever deducted server-side (or, if the amount doesn't
# clear it, rejected with `{"status": "err", "response": "Withdrawal is
# smaller than fee."}` — hit live 2026-09-06 requesting exactly the
# then-configured $1.00 floor, meaning the real fee is already at or past
# that "at the time of this writing" figure, or the venue requires STRICTLY
# more than the fee rather than "at least"). This is set well above that
# stale snapshot rather than matched to it, so a fee that has crept up
# further still clears it.
_MIN_WITHDRAW_USD = 2.0

# The EIP-712 domain's chainId for a "user-signed action" like withdraw3 —
# Arbitrum, regardless of which chain the withdrawal actually lands on (the
# withdrawal only ever lands on Arbitrum here anyway, dex_registry.py wires
# Hyperliquid to Chain.ARBITRUM exclusively). Distinct from the ACTION's own
# `signatureChainId` field below, even though both happen to name Arbitrum.
_EIP712_DOMAIN_CHAIN_ID = 42161
_SIGNATURE_CHAIN_ID_HEX = "0xa4b1"


class HyperliquidConnector(DexConnector):
    name = "Hyperliquid"
    supported_chains = frozenset({Chain.ARBITRUM})
    supported_stables = frozenset({Stable.USDC})

    def __init__(self):
        self._account_address = Web3.to_checksum_address(config.require_env("HYPERLIQUID_WALLET_ADDRESS"))
        self._operating_wallet = OperatingWallet()
        self._session = requests.Session()

    def _require_operating_wallet_is_account(self) -> None:
        signer = Web3.to_checksum_address(self._operating_wallet.address)
        if signer != self._account_address:
            raise RuntimeError(
                f"Hyperliquid: operating wallet {signer} is not the account HYPERLIQUID_WALLET_ADDRESS "
                f"({self._account_address}) — a withdraw signed by any OTHER address withdraws from THAT "
                "address's Hyperliquid account, not this one. Set COMPASS_TEST_WALLET_KEY_VAR to the env "
                "var holding HYPERLIQUID_WALLET_ADDRESS's own private key."
            )

    def _withdraw_signature(self, amount_usd: float, to_address: str) -> tuple[dict, dict]:
        nonce = int(time.time() * 1000)
        message = {
            "hyperliquidChain": "Mainnet",
            "destination": Web3.to_checksum_address(to_address).lower(),
            "amount": f"{amount_usd:.6f}",
            "time": nonce,
        }
        domain = {
            "name": "HyperliquidSignTransaction",
            "version": "1",
            "chainId": _EIP712_DOMAIN_CHAIN_ID,
            "verifyingContract": "0x0000000000000000000000000000000000000000",
        }
        field_types = {"hyperliquidChain": "string", "destination": "string", "amount": "string", "time": "uint64"}
        signable = encode_typed_data(
            domain_data=domain,
            message_types={"HyperliquidTransaction:Withdraw": [{"name": k, "type": field_types[k]} for k in message]},
            message_data=message,
        )
        # Signed by the operating wallet, checked ABOVE to be
        # HYPERLIQUID_WALLET_ADDRESS itself — never a delegated agent key.
        signed = self._operating_wallet.account.sign_message(signable)
        action = {
            "type": "withdraw3",
            "hyperliquidChain": message["hyperliquidChain"],
            "signatureChainId": _SIGNATURE_CHAIN_ID_HEX,
            "amount": message["amount"],
            "time": nonce,
            "destination": message["destination"],
        }
        signature = {"r": hex(signed.r), "s": hex(signed.s), "v": signed.v}
        return action, signature

    def withdraw(self, chain: Chain, stable: Stable, amount_usd: float, to_address: str) -> WithdrawResult:
        if amount_usd < _MIN_WITHDRAW_USD:
            raise RuntimeError(
                f"Hyperliquid: {amount_usd} USDC is below this connector's ${_MIN_WITHDRAW_USD:.0f} withdraw "
                "floor — the venue rejects any amount that doesn't clear its withdrawal fee "
                '(`{"status": "err", "response": "Withdrawal is smaller than fee."}`).'
            )
        self._require_operating_wallet_is_account()
        action, signature = self._withdraw_signature(amount_usd, to_address)
        requestedAt = time.time()
        response = self._session.post(
            _EXCHANGE_URL,
            json={"action": action, "nonce": action["time"], "signature": signature},
            timeout=15,
        )
        if not response.ok:
            raise RuntimeError(f"Hyperliquid withdraw -> {response.status_code}: {response.text}")
        data = response.json()
        if data.get("status") != "ok":
            raise RuntimeError(f"Hyperliquid withdraw rejected: {data}")
        return WithdrawResult(
            externalId=str(action["time"]),
            requestedAt=requestedAt,
            acceptedAt=time.time(),
            amountRequestedUsd=amount_usd,
            # Docs: "$1 fee for withdrawing at the time of this writing" —
            # not itself returned by the endpoint (response is just
            # {"type": "default"}), so left None; the executor falls back to
            # diffing the destination wallet's on-chain balance instead.
            quotedFeeUsd=None,
        )

    def build_deposit_tx(self, w3: Web3, from_address: str, chain: Chain, stable: Stable, amount_usd: float) -> dict:
        if amount_usd < _MIN_DEPOSIT_USD:
            raise RuntimeError(
                f"Hyperliquid: {amount_usd} USDC is below the venue's ${_MIN_DEPOSIT_USD:.0f} minimum deposit — "
                "anything under that is silently lost, not credited (docs, verbatim)."
            )
        if Web3.to_checksum_address(from_address) != self._account_address:
            raise RuntimeError(
                f"Hyperliquid: deposit is credited to whichever address SENDS the transfer, not a chosen "
                f"recipient — {from_address} would credit a DIFFERENT account than HYPERLIQUID_WALLET_ADDRESS "
                f"({self._account_address})."
            )
        return chain_ops.build_erc20_transfer_tx(w3, chain, stable, from_address, _BRIDGE_ADDRESS, amount_usd)

    def poll_balance_usd(self, stable: Stable) -> float:
        # Mirrors balances.py::_hyperliquid's spot-USDC + builder-dex-equity
        # formula exactly (see its docstring for why the default perp dex's
        # clearinghouseState is deliberately NOT added again on top).
        spot_resp = self._session.post(
            _INFO_URL, json={"type": "spotClearinghouseState", "user": self._account_address}, timeout=15
        )
        spot_resp.raise_for_status()
        spot = spot_resp.json()
        spot_usdc = next((float(b.get("total") or 0) for b in spot.get("balances", []) if b.get("coin") == "USDC"), 0.0)

        builder_dex_equity = 0.0
        for dex in ("xyz", "hyna"):
            state_resp = self._session.post(
                _INFO_URL, json={"type": "clearinghouseState", "user": self._account_address, "dex": dex}, timeout=15
            )
            state_resp.raise_for_status()
            state = state_resp.json()
            builder_dex_equity += float(state.get("marginSummary", {}).get("accountValue", 0.0))

        return spot_usdc + builder_dex_equity
