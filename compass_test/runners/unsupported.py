"""Explicit stand-in for DEXes the graph's registry knows about but that
compass_test does not yet instrument — so a gap shows up in every report
(TestRunReport.unsupportedDexes, and the frontend's Test Results tab) instead
of the DEX silently vanishing from the list.

Reasons, so the gap is legible rather than mysterious:
  - Gate (Perp DEX): excluded by explicit request for this v1.
  - Aden: no withdraw/deposit/bridge REST endpoint exists in its public API
    docs (https://aden-perp-api-docs.aden.io/, checked 2026-09-04) — only
    market-data endpoints. Fund movement would need reverse-engineering an
    undocumented flow, not attempted here.
  - dYdX: a real withdraw is THREE hops, not one (checked against
    docs.dydx.xyz and the actual `dydx-v4-client` PyPI package, 2026-09-06).
    Hop 1 (subaccount -> your own dYdX main account) is solid — NodeClient.
    withdraw() really does sign+broadcast a MsgWithdrawFromSubaccount. Hop 2
    (dYdX main account -> Noble, IBC) has NO working vendor implementation:
    docs.dydx.xyz's own NobleClient.send_token_ibc() is unfinished in the
    published package — its body computes `coin = token.coin()` and ends,
    no message construction, no return. Hop 3 (Noble -> Arbitrum, Circle's
    CCTP MsgDepositForBurn) isn't in dydx-v4-client at all. Hand-rolling
    hops 2/3 means writing an unverified CCTP burn call with no reference
    implementation — a wrong destination-domain/recipient-padding there
    burns the USDC with nothing minting on the other end, irreversibly.
    The one real alternative is Skip Go (dYdX's own documented bridge
    partner, real public API at api.skip.build, confirmed reachable) which
    returns pre-built messages for the whole route instead of requiring a
    hand-rolled CCTP message — but that's a materially bigger build (a new
    Cosmos mnemonic credential, real protobuf tx signing against two
    chains, a new third-party dependency), decided against for now.
Hyperliquid, Lighter, Extended and Ondo Perps moved OUT of this table — see
runners/hyperliquid.py, runners/lighter.py, runners/extended.py and
runners/ondo.py (Lighter and Extended: withdraw only — deposit still lands
here implicitly via NotImplementedError raised directly in each connector,
not via this module). Ondo Perps turned out to be documented after all —
the earlier "no Bearer JWT handshake" reason was based on the "Builder
Integration Guide" page alone; the separate "API Reference" pages
(docs.ondoperps.xyz/api-reference/wallet/*) document both deposit and
withdraw against the SAME API-key auth balances.py::_ondo already uses, see
runners/ondo.py's own docstring.
"""

from __future__ import annotations

from web3 import Web3

from graph.structures.DEXes import Chain, Stable

from .base import DexConnector, WithdrawResult

UNSUPPORTED_REASONS: dict[str, str] = {
    "Gate (Perp DEX)": "excluded for this v1 by explicit request",
    "dYdX": "3-hop withdraw (subaccount->main is solid, but Noble IBC + Noble->Arbitrum CCTP have no working vendor SDK support) — not implemented",
}


class UnsupportedConnector(DexConnector):
    supported_chains = frozenset()
    supported_stables = frozenset()

    def __init__(self, name: str, reason: str | None = None):
        self.name = name
        self.reason = reason or UNSUPPORTED_REASONS.get(name, "not instrumented yet")

    def _raise(self):
        raise NotImplementedError(f"{self.name}: {self.reason}")

    def withdraw(self, chain: Chain, stable: Stable, amount_usd: float, to_address: str) -> WithdrawResult:
        self._raise()

    def build_deposit_tx(self, w3: Web3, from_address: str, chain: Chain, stable: Stable, amount_usd: float) -> dict:
        self._raise()

    def poll_balance_usd(self, stable: Stable) -> float:
        self._raise()
