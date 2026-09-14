"""CoW Swap — the venue behind compass_test's Swap hop (same-chain USDC <->
USDT on BSC and Arbitrum). NOT a DexConnector: a swap has no DEX account to
withdraw from or credit — it's the operating wallet trading against CoW's
batch auction, so the surface is quote / approve / place order / wait /
cancel, driven by executor._run_swap.

Confirmed live 2026-09-10, against the real orderbook, with THIS wallet:
  - POST /{bnb,arbitrum_one}/api/v1/quote answers for USDC->USDT (~$0.01
    network fee on a $1 order on both chains, `verified: true`).
  - The EIP-712 order digest eth_account computes matches a hand-rolled
    keccak(TYPE_HASH || abi.encode(...)) of GPv2Order.sol's struct exactly
    (see tests/test_cowswap_order.py), and the orderbook recovered this
    wallet as the signer (POST /orders -> 201) for a deliberately
    UNFILLABLE limit order (buy 2 USDT per USDC), then accepted its
    EIP-712 `OrderCancellations` (status open -> cancelled, executed
    amounts 0). No funds moved.
  - Finding from that test: the orderbook does NOT check the sell token
    allowance at submission — an order with no approval is accepted and
    then just sits `open` until it expires. So ensure_allowance() below is
    mandatory BEFORE place_order(), not an optimisation.

What we pay, and what "actual cost" means for this hop: the wallet never
pays gas for the trade itself (the winning solver does, and recovers it
through the network fee taken out of the sell amount); what we observe is
simply `sold - bought`, fee and price impact included. The one-off ERC-20
approve to CoW's vault relayer IS a real tx we pay gas for, so its gas is
added on top when it was needed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from web3 import Web3

from connectors import cowswap
from connectors.cowswap import CowOrder, CowQuote, CowSwapConnector
from connectors.stable_tokens import get_stable_token_address
from graph.structures.DEXes import Chain, Stable

from .. import chain_ops, config
from ..wallet import OperatingWallet

VENUE_NAME = cowswap.COWSWAP_VENUE_NAME

# The two chains this hop is wired for (see README.md scope) — both have a
# CoW orderbook (connectors.chain_metadata.cowswap_slug) and both stables
# known in connectors.stable_tokens.
SUPPORTED_CHAINS = frozenset({Chain.BSC, Chain.ARBITRUM})
SUPPORTED_STABLES = frozenset({Stable.USDC, Stable.USDT})

# A CoW order that hasn't filled within the polling window is CANCELLED
# rather than left dangling (executor._run_swap): otherwise it could still
# settle minutes later, unobserved, after the harness already reported
# "unconfirmed". validTo is nonetheless kept short as a second net in case
# the cancellation itself fails.
ORDER_VALIDITY_MARGIN_SECONDS = 120


@dataclass
class PlacedOrder:
    uid: str
    order: CowOrder
    quote: CowQuote
    submitted_at: float


@dataclass
class SwapOutcome:
    status: str  # cowswap.ORDER_STATUS_* as last observed
    executed_sell_units: int
    executed_buy_units: int
    tx_hash: str | None
    finished_at: float


def _hex(signature) -> str:
    raw = signature.signature.hex()
    return raw if raw.startswith("0x") else "0x" + raw


class CowSwapRunner:
    name = VENUE_NAME

    def __init__(self, connector: CowSwapConnector | None = None):
        self._connector = connector or CowSwapConnector()

    @staticmethod
    def supports(chain: Chain, stable_in: Stable, stable_out: Stable) -> bool:
        return (
            chain in SUPPORTED_CHAINS
            and stable_in in SUPPORTED_STABLES
            and stable_out in SUPPORTED_STABLES
            and stable_in != stable_out
            and CowSwapConnector.supports(chain)
        )

    # --- Quote ------------------------------------------------------------

    def quote(self, chain: Chain, stable_in: Stable, stable_out: Stable, amount_usd: float, from_address: str) -> CowQuote:
        """Real quote for selling `amount_usd` of stable_in (fee included —
        that's what leaves the wallet) from `from_address`, valid long
        enough to cover the whole polling window."""
        return self._connector.request_quote(
            chain,
            get_stable_token_address(chain, stable_in),
            get_stable_token_address(chain, stable_out),
            chain_ops.usd_to_token_units(chain, stable_in, amount_usd),
            from_address=from_address,
            valid_for_seconds=int(config.POLL_TIMEOUT_SECONDS) + ORDER_VALIDITY_MARGIN_SECONDS,
        )

    # --- Allowance --------------------------------------------------------

    def allowance_shortfall_units(self, w3: Web3, chain: Chain, stable_in: Stable, owner: str, sell_units: int) -> int:
        """How many token units of approval to CoW's vault relayer are
        MISSING for an order selling `sell_units` — 0 when the existing
        allowance already covers it."""
        current = chain_ops.get_stable_allowance(w3, chain, stable_in, owner, cowswap.VAULT_RELAYER)
        return max(sell_units - current, 0)

    def build_approve_tx(self, w3: Web3, chain: Chain, stable_in: Stable, owner: str, amount_usd: float) -> dict:
        """Exact-amount approval (not unlimited): one more ~$0.01 tx per
        swap in exchange for never leaving a standing pull right on the
        operating wallet — same conservatism as the deposit connectors."""
        return chain_ops.build_erc20_approve_tx(w3, chain, stable_in, owner, cowswap.VAULT_RELAYER, amount_usd)

    # --- Order lifecycle ----------------------------------------------------

    def place_order(self, chain: Chain, quote: CowQuote, wallet: OperatingWallet, slippage_bps: int | None = None) -> PlacedOrder:
        slippage = config.SWAP_SLIPPAGE_BPS if slippage_bps is None else slippage_bps
        order = cowswap.build_order_from_quote(quote, slippage, receiver=wallet.address)
        signature = _hex(wallet.sign_typed_data(cowswap.order_typed_data(chain, order)))
        submitted_at = time.time()
        uid = self._connector.submit_order(chain, order, signature, wallet.address, quote_id=quote.quote_id)
        return PlacedOrder(uid=uid, order=order, quote=quote, submitted_at=submitted_at)

    def wait_for_settlement(self, chain: Chain, uid: str, timeout_s: float, interval_s: float) -> SwapOutcome:
        """Polls GET /orders/{uid} until it leaves `open` — fulfilled,
        cancelled or expired — or `timeout_s` elapses (status then still
        reads "open"; the caller decides what to do with the live order)."""
        deadline = time.time() + timeout_s
        last = self._connector.get_order(chain, uid)
        while last.get("status") in (cowswap.ORDER_STATUS_OPEN, cowswap.ORDER_STATUS_PRESIGNATURE_PENDING) and time.time() < deadline:
            time.sleep(interval_s)
            last = self._connector.get_order(chain, uid)
        return SwapOutcome(
            status=str(last.get("status")),
            executed_sell_units=int(last.get("executedSellAmount") or 0),
            executed_buy_units=int(last.get("executedBuyAmount") or 0),
            tx_hash=self._settlement_tx_hash(chain, uid) if last.get("status") == cowswap.ORDER_STATUS_FULFILLED else None,
            finished_at=time.time(),
        )

    def _settlement_tx_hash(self, chain: Chain, uid: str) -> str | None:
        try:
            trades = self._connector.get_trades(chain, uid)
        except Exception:  # noqa: BLE001 - informational only, the fill itself is already confirmed
            return None
        return next((t.get("txHash") for t in trades if t.get("txHash")), None)

    def cancel(self, chain: Chain, uid: str, wallet: OperatingWallet) -> None:
        signature = _hex(wallet.sign_typed_data(cowswap.cancellations_typed_data(chain, [uid])))
        self._connector.cancel_orders(chain, [uid], signature)

    def get_status(self, chain: Chain, uid: str) -> str:
        return str(self._connector.get_order(chain, uid).get("status"))
