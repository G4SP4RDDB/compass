"""DexConnector: what each DEX-specific runner (mexc.py, aster.py, ...)
implements. executor.py drives these; nothing here talks to a chain or an
exchange directly."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from web3 import Web3

from graph.structures.DEXes import Chain, Stable


@dataclass
class WithdrawResult:
    externalId: str
    requestedAt: float
    acceptedAt: float
    amountRequestedUsd: float
    # Fee/amount the exchange itself reports synchronously, when it does
    # (e.g. MEXC's withdraw-history amount vs. requested amount). None means
    # "not known yet" — the executor falls back to diffing the destination
    # wallet's on-chain balance before/after instead.
    quotedFeeUsd: float | None = None


class DexConnector(ABC):
    name: str
    supported_chains: frozenset[Chain]
    supported_stables: frozenset[Stable]

    def supports(self, chain: Chain, stable: Stable) -> bool:
        return chain in self.supported_chains and stable in self.supported_stables

    @abstractmethod
    def withdraw(self, chain: Chain, stable: Stable, amount_usd: float, to_address: str) -> WithdrawResult:
        """Request a withdrawal of `amount_usd` of `stable` on `chain` to
        `to_address` (the operating wallet). Must return once the exchange
        has ACCEPTED the request (not once funds have arrived — the executor
        polls the destination wallet separately for that)."""

    @abstractmethod
    def build_deposit_tx(self, w3: Web3, from_address: str, chain: Chain, stable: Stable, amount_usd: float) -> dict:
        """Unsigned tx dict crediting `amount_usd` of `stable` on `chain`
        into this DEX's account, sent from `from_address` (the operating
        wallet) — a plain ERC-20 transfer to a deposit address, or a DEX-
        specific contract call. See chain_ops.build_erc20_transfer_tx /
        build_contract_call_tx."""

    @abstractmethod
    def poll_balance_usd(self, stable: Stable) -> float:
        """This DEX's current available balance in `stable`, used by the
        executor to detect when a deposit has been credited (poll until it
        increases) — an authenticated account/balance call, not on-chain."""
