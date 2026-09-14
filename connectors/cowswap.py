"""CoW Protocol (CoW Swap) orderbook connector — quotes AND the full
off-chain order lifecycle (build -> EIP-712 typed data -> submit -> poll ->
cancel) used by compass_test's Swap hop (see compass_test/runners/cowswap.py
and compass_test/executor.py::_run_swap).

No private key ever enters this module: it produces the EIP-712 payload for
an order/cancellation and takes a finished signature back — signing itself
is compass_test.wallet.OperatingWallet.sign_typed_data's job.

Verified against CoW's own sources (2026-09-10), not guessed:
  - orderbook OpenAPI (cowprotocol/services crates/orderbook/openapi.yml):
    OrderCreation fields, the `appData`="{}" + fixed hash convention,
    OrderStatus enum, Trade.txHash, OrderCancellations struct.
  - GPv2Order.sol (cowprotocol/contracts): the Order EIP-712 type string
    whose keccak is TYPE_HASH 0xd5a25ba2...; src/ts/order.ts
    ORDER_TYPE_FIELDS (same fields, same order, same solidity types).
  - networks.json (cowprotocol/contracts): GPv2Settlement and
    GPv2VaultRelayer are the same address on every chain, including 56
    (BNB) and 42161 (Arbitrum).
  - Live: POST /bnb/api/v1/quote and /arbitrum_one/api/v1/quote both
    answered 200 for USDC->USDT with this wallet as `from` (2026-09-10).

Fee model: since CoW moved fees into surplus, a submitted order carries
`feeAmount: "0"` and the network cost is folded into the sell amount —
sellAmount = quote.sellAmount + quote.feeAmount (what leaves the wallet),
buyAmount = quote.buyAmount minus a slippage tolerance (the least we accept).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests

from graph.structures.DEXes import Chain

from .chain_metadata import get_metadata
from .exceptions import ConnectorAPIError, UnsupportedChainError
from .models import SwapQuote

COWSWAP_API_BASE_URL = "https://api.cow.fi"

# Display/registry name of the swap venue — what compass_test reports use in
# the `dex` slot of a Swap hop (there is no DEX behind a swap) and the key
# connectors/dex_measured_delays.json files measured swap delays under.
COWSWAP_VENUE_NAME = "CoW Swap"

# Same address on every supported chain (cowprotocol/contracts networks.json).
SETTLEMENT_CONTRACT = "0x9008D19f58AAbD9eD0D60971565AA8510560ab41"
# The contract that actually pulls the sell token (transferFrom) at
# settlement — THIS is what the ERC-20 approve must target, not the
# settlement contract.
VAULT_RELAYER = "0xC92E8bdf79f0507f65a392b0ab4667716BFE0110"

EIP712_DOMAIN_NAME = "Gnosis Protocol"
EIP712_DOMAIN_VERSION = "v2"

# The orderbook's documented "I don't care about appData" convention: send
# the literal string "{}" as `appData` and sign the order with this exact
# bytes32 (keccak256 of the UTF-8 bytes of "{}") — see OrderCreation.appData
# in the OpenAPI spec.
EMPTY_APP_DATA = "{}"
EMPTY_APP_DATA_HASH = "0xb48d38f93eaa084033fc5970bf96e559c33c4cdc07d889ab00b4d63f9590739d"

# Any address works as a placeholder "from"/"receiver" for a quote-only
# request; CoW's quote endpoint doesn't execute anything, it just prices.
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

ORDER_KIND_SELL = "sell"
TOKEN_BALANCE_ERC20 = "erc20"
SIGNING_SCHEME_EIP712 = "eip712"

ORDER_STATUS_OPEN = "open"
ORDER_STATUS_FULFILLED = "fulfilled"
ORDER_STATUS_CANCELLED = "cancelled"
ORDER_STATUS_EXPIRED = "expired"
ORDER_STATUS_PRESIGNATURE_PENDING = "presignaturePending"

# EIP-712 type of a CoW order — field names, order and solidity types must
# match GPv2Order.sol's TYPE_HASH string byte for byte or the settlement
# contract recovers a different signer and the orderbook rejects the order
# (InvalidSignature / WrongOwner). Cross-checked in
# compass_test/tests/test_cowswap_order.py against a hand-rolled
# keccak(TYPE_HASH || abi-encoded fields) digest.
ORDER_TYPE_FIELDS: list[dict[str, str]] = [
    {"name": "sellToken", "type": "address"},
    {"name": "buyToken", "type": "address"},
    {"name": "receiver", "type": "address"},
    {"name": "sellAmount", "type": "uint256"},
    {"name": "buyAmount", "type": "uint256"},
    {"name": "validTo", "type": "uint32"},
    {"name": "appData", "type": "bytes32"},
    {"name": "feeAmount", "type": "uint256"},
    {"name": "kind", "type": "string"},
    {"name": "partiallyFillable", "type": "bool"},
    {"name": "sellTokenBalance", "type": "string"},
    {"name": "buyTokenBalance", "type": "string"},
]
ORDER_TYPE_HASH = "0xd5a25ba2e97094ad7d83dc28a6572da797d6b3e7fc6663bd93efb789fc17e489"

# `OrderCancellations(bytes[] orderUids)` — the batch form the orderbook's
# DELETE /api/v1/orders takes (OpenAPI OrderCancellations).
CANCELLATIONS_TYPE_FIELDS: list[dict[str, str]] = [{"name": "orderUids", "type": "bytes[]"}]

_EIP712_DOMAIN_FIELDS = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]


@dataclass
class CowQuote:
    """What POST /api/v1/quote returned for a `kind: sell` request with
    `sellAmountBeforeFee` — the raw integer token units, never USD."""

    chain: Chain
    sell_token: str
    buy_token: str
    receiver: str
    # Net sell amount AFTER the network fee is taken out (the part that
    # actually gets traded), and that fee — their sum is sellAmountBeforeFee,
    # i.e. what leaves the wallet.
    sell_amount: int
    fee_amount: int
    buy_amount: int
    valid_to: int
    quote_id: int | None
    verified: bool
    expiration: str
    # Informational: the solver's gas estimate/price behind fee_amount.
    gas_amount: int | None = None
    gas_price: int | None = None

    @property
    def sell_amount_before_fee(self) -> int:
        return self.sell_amount + self.fee_amount


@dataclass
class CowOrder:
    """A fully specified order, ready to be turned into EIP-712 typed data
    (order_typed_data) and posted (submit_order). Amounts are integer token
    units. This is exactly the struct the signature commits to."""

    sell_token: str
    buy_token: str
    receiver: str
    sell_amount: int
    buy_amount: int
    valid_to: int
    app_data_hash: str = EMPTY_APP_DATA_HASH
    fee_amount: int = 0
    kind: str = ORDER_KIND_SELL
    partially_fillable: bool = False
    sell_token_balance: str = TOKEN_BALANCE_ERC20
    buy_token_balance: str = TOKEN_BALANCE_ERC20

    def as_eip712_message(self) -> dict:
        return {
            "sellToken": self.sell_token,
            "buyToken": self.buy_token,
            "receiver": self.receiver,
            "sellAmount": self.sell_amount,
            "buyAmount": self.buy_amount,
            "validTo": self.valid_to,
            "appData": self.app_data_hash,
            "feeAmount": self.fee_amount,
            "kind": self.kind,
            "partiallyFillable": self.partially_fillable,
            "sellTokenBalance": self.sell_token_balance,
            "buyTokenBalance": self.buy_token_balance,
        }


def build_order_from_quote(quote: CowQuote, slippage_bps: int, receiver: str | None = None) -> CowOrder:
    """The fee-in-surplus order shape: feeAmount 0, the whole
    sellAmountBeforeFee sold, buyAmount = quoted buy amount less
    `slippage_bps` basis points (the floor below which the order simply
    never settles — it expires rather than filling at a worse price)."""
    if not 0 <= slippage_bps <= 10_000:
        raise ValueError(f"slippage_bps must be within [0, 10000], got {slippage_bps}")
    min_buy_amount = quote.buy_amount * (10_000 - slippage_bps) // 10_000
    return CowOrder(
        sell_token=quote.sell_token,
        buy_token=quote.buy_token,
        receiver=receiver or quote.receiver,
        sell_amount=quote.sell_amount_before_fee,
        buy_amount=min_buy_amount,
        valid_to=quote.valid_to,
    )


def eip712_domain(chain: Chain) -> dict:
    chain_id = get_metadata(chain).chain_id
    if chain_id is None:
        raise UnsupportedChainError("CowSwapConnector", chain)
    return {
        "name": EIP712_DOMAIN_NAME,
        "version": EIP712_DOMAIN_VERSION,
        "chainId": chain_id,
        "verifyingContract": SETTLEMENT_CONTRACT,
    }


def order_typed_data(chain: Chain, order: CowOrder) -> dict:
    """Full EIP-712 message ({types, domain, primaryType, message}) in the
    shape eth_account.messages.encode_typed_data(full_message=...) takes."""
    return {
        "types": {"EIP712Domain": _EIP712_DOMAIN_FIELDS, "Order": ORDER_TYPE_FIELDS},
        "domain": eip712_domain(chain),
        "primaryType": "Order",
        "message": order.as_eip712_message(),
    }


def cancellations_typed_data(chain: Chain, order_uids: list[str]) -> dict:
    return {
        "types": {"EIP712Domain": _EIP712_DOMAIN_FIELDS, "OrderCancellations": CANCELLATIONS_TYPE_FIELDS},
        "domain": eip712_domain(chain),
        "primaryType": "OrderCancellations",
        "message": {"orderUids": [bytes.fromhex(uid.removeprefix("0x")) for uid in order_uids]},
    }


def order_creation_payload(order: CowOrder, signature: str, from_address: str, quote_id: int | None = None) -> dict:
    """Body of POST /api/v1/orders. `from` is deliberately set so the
    orderbook cross-checks the recovered signer against the address we
    THINK signed — a wrong signature encoding then fails loudly
    (WrongOwner) instead of the order silently belonging to nobody."""
    return {
        "sellToken": order.sell_token,
        "buyToken": order.buy_token,
        "receiver": order.receiver,
        "sellAmount": str(order.sell_amount),
        "buyAmount": str(order.buy_amount),
        "validTo": order.valid_to,
        "feeAmount": str(order.fee_amount),
        "kind": order.kind,
        "partiallyFillable": order.partially_fillable,
        "sellTokenBalance": order.sell_token_balance,
        "buyTokenBalance": order.buy_token_balance,
        "signingScheme": SIGNING_SCHEME_EIP712,
        "signature": signature,
        "from": from_address,
        "quoteId": quote_id,
        "appData": EMPTY_APP_DATA,
        "appDataHash": order.app_data_hash,
    }


class CowSwapConnector:
    def __init__(self, session: requests.Session | None = None, timeout_s: float = 20.0) -> None:
        self._session = session or requests.Session()
        self._timeout_s = timeout_s

    def _slug(self, chain: Chain) -> str:
        slug = get_metadata(chain).cowswap_slug
        if slug is None:
            raise UnsupportedChainError("CowSwapConnector", chain)
        return slug

    def _url(self, chain: Chain, path: str) -> str:
        return f"{COWSWAP_API_BASE_URL}/{self._slug(chain)}/api/v1/{path}"

    def _request(self, method: str, chain: Chain, path: str, json_body: dict | None = None):
        url = self._url(chain, path)
        response = self._session.request(method, url, json=json_body, timeout=self._timeout_s)
        if not response.ok:
            raise ConnectorAPIError("CowSwapConnector", url, f"{response.status_code}: {response.text}")
        if not response.content:
            return None
        return response.json()

    @staticmethod
    def supports(chain: Chain) -> bool:
        return get_metadata(chain).cowswap_slug is not None

    # --- Quotes ---------------------------------------------------------

    def request_quote(
        self,
        chain: Chain,
        sell_token: str,
        buy_token: str,
        sell_amount_before_fee: int,
        from_address: str = ZERO_ADDRESS,
        receiver: str | None = None,
        valid_for_seconds: int = 1800,
    ) -> CowQuote:
        """`kind: sell` quote for `sell_amount_before_fee` token units of
        `sell_token` — the amount that would leave the wallet, fee included.
        `from_address` matters for a real order (the orderbook verifies the
        quote against that account's balance — `verified: true`), a
        placeholder is fine for pricing only."""
        body = {
            "sellToken": sell_token,
            "buyToken": buy_token,
            "receiver": receiver or from_address,
            "from": from_address,
            "sellAmountBeforeFee": str(sell_amount_before_fee),
            "kind": ORDER_KIND_SELL,
            "validTo": int(time.time()) + valid_for_seconds,
            "signingScheme": SIGNING_SCHEME_EIP712,
            "partiallyFillable": False,
            "sellTokenBalance": TOKEN_BALANCE_ERC20,
            "buyTokenBalance": TOKEN_BALANCE_ERC20,
            "appData": EMPTY_APP_DATA,
            "appDataHash": EMPTY_APP_DATA_HASH,
        }
        data = self._request("POST", chain, "quote", body)
        quote = data["quote"]
        return CowQuote(
            chain=chain,
            sell_token=quote["sellToken"],
            buy_token=quote["buyToken"],
            receiver=quote.get("receiver") or (receiver or from_address),
            sell_amount=int(quote["sellAmount"]),
            fee_amount=int(quote["feeAmount"]),
            buy_amount=int(quote["buyAmount"]),
            valid_to=int(quote["validTo"]),
            quote_id=data.get("id"),
            verified=bool(data.get("verified", False)),
            expiration=str(data.get("expiration", "")),
            gas_amount=int(quote["gasAmount"]) if quote.get("gasAmount") is not None else None,
            gas_price=int(quote["gasPrice"]) if quote.get("gasPrice") is not None else None,
        )

    def get_quote(
        self,
        chain: Chain,
        sell_token: str,
        buy_token: str,
        sell_amount: int,
        from_address: str = ZERO_ADDRESS,
    ) -> SwapQuote:
        """Pricing-only view kept for existing callers (connectors.models.SwapQuote)."""
        quote = self.request_quote(chain, sell_token, buy_token, sell_amount, from_address=from_address)
        return SwapQuote(
            chain=chain,
            sell_token=sell_token,
            buy_token=buy_token,
            buy_amount=quote.buy_amount,
            fee_amount=quote.fee_amount,
            valid_to=quote.valid_to,
        )

    # --- Orders ---------------------------------------------------------

    def submit_order(self, chain: Chain, order: CowOrder, signature: str, from_address: str, quote_id: int | None = None) -> str:
        """POST /api/v1/orders -> the order UID (0x-prefixed, 56 bytes)."""
        uid = self._request("POST", chain, "orders", order_creation_payload(order, signature, from_address, quote_id))
        if not isinstance(uid, str) or not uid.startswith("0x"):
            raise ConnectorAPIError("CowSwapConnector", self._url(chain, "orders"), f"unexpected order UID response: {uid!r}")
        return uid

    def get_order(self, chain: Chain, order_uid: str) -> dict:
        """GET /api/v1/orders/{uid} — carries `status` (OrderStatus enum),
        `executedSellAmount`/`executedBuyAmount`/`executedFee`, `invalidated`."""
        return self._request("GET", chain, f"orders/{order_uid}")

    def get_trades(self, chain: Chain, order_uid: str) -> list[dict]:
        """GET /api/v1/trades?orderUid= — one entry per (partial) fill, each
        with the settlement `txHash` and executed amounts."""
        return self._request("GET", chain, f"trades?orderUid={order_uid}") or []

    def cancel_orders(self, chain: Chain, order_uids: list[str], signature: str) -> None:
        """DELETE /api/v1/orders with an EIP-712 `OrderCancellations`
        signature (see cancellations_typed_data)."""
        self._request(
            "DELETE",
            chain,
            "orders",
            {"orderUids": order_uids, "signature": signature, "signingScheme": SIGNING_SCHEME_EIP712},
        )
