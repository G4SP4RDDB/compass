"""Shared orchestration for testing ONE (dex, hopType, chain, stable) —
validate the request, resolve a test amount, run it (dry or live), compare,
and save a report. Used by both `cli.py`'s `run-hop` command and the
frontend's "Test This Edge" / "Run LIVE" buttons
(`visualization/server.py` POST `/api/test-hop`), so there is exactly one
place that does this, not two slightly-different copies.

Every actual safety decision still lives in executor.py (the $ caps, the
`live` + `COMPASS_TEST_ALLOW_LIVE` gate) — this module only handles
request-shaped validation (unknown DEX, unsupported connector, wrong
chain/stable, no usable amount) and orchestration, never re-implements or
loosens a safety check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from connectors.cowswap import COWSWAP_VENUE_NAME
from graph.structures.bridges import BridgeProtocol, availableBridgeProtocols
from graph.structures.DEXes import Chain, Stable

from . import comparator, config, executor, plan_loader, reporter
from .models import HopComparison, HopType, TestRunReport
from .runners.cowswap import CowSwapRunner
from .runners.registry import get_connector, is_supported
from .wallet import OperatingWallet


class HopValidationError(ValueError):
    """A request that never reaches executor.run_hop at all — unknown DEX,
    unsupported connector, unsupported chain/stable for that DEX, or no
    usable test amount. Distinct from executor.SafetyCapError (a request
    that WOULD have run but a safety gate refused it): this is "malformed
    ask", that is "safety said no"."""


class HopAborted(RuntimeError):
    """The caller's own confirm() hook (see run_single_hop) declined a live
    run — not an error, just "don't do this after all"."""


@dataclass
class HopRunResult:
    comparison: HopComparison
    report: TestRunReport
    reportPath: str
    resolvedAmountUsd: float
    usedConfiguredMinimum: bool


def _parse_chain_stable(chain_name: str, stable_name: str) -> tuple[Chain, Stable]:
    try:
        return Chain[chain_name], Stable[stable_name]
    except KeyError as exc:
        raise HopValidationError(f"invalid chain/stable: {exc}") from exc


def resolve_swap_hop(chain_name: str, stable_name: str, to_stable_name: str | None, amount_usd: float):
    """Swap counterpart of resolve_hop: a Swap has no DEX — its `dex` slot
    is the venue (COWSWAP_VENUE_NAME) — and its estimate depends on the
    amount (slippage), hence the extra argument. Returns (chain, stableIn,
    stableOut, PlannedHop)."""
    if not to_stable_name:
        raise HopValidationError("a Swap hop needs the stable to buy (toStable), e.g. stable=USDC toStable=USDT")
    chain, stable_in = _parse_chain_stable(chain_name, stable_name)
    _, stable_out = _parse_chain_stable(chain_name, to_stable_name)
    if stable_in == stable_out:
        raise HopValidationError(f"a Swap needs two different stables, got {stable_in.name} -> {stable_out.name}")
    if not CowSwapRunner.supports(chain, stable_in, stable_out):
        raise HopValidationError(
            f"{COWSWAP_VENUE_NAME} swap {stable_in.name}->{stable_out.name} on {chain.name} is not instrumented "
            f"(BSC and Arbitrum, USDC<->USDT only — see compass_test/runners/cowswap.py)"
        )
    return chain, stable_in, stable_out, plan_loader.build_swap_hop_estimate(chain, stable_in, stable_out, amount_usd)


def resolve_bridge_hop(from_chain_name: str, stable_name: str, to_chain_name: str):
    """Bridge counterpart of resolve_hop: Aden's internal bridge has no
    free-form `dex` (it's always "Aden", the only bridge protocol modeled —
    see graph.structures.bridges.BridgeProtocol) and needs two chains
    instead of one — same shape reason as resolve_swap_hop needing two
    stables. Returns (fromChain, toChain, stable, PlannedHop)."""
    registry = plan_loader.load_configured_dex_registry()
    dex = registry["Aden"]
    if not is_supported("Aden"):
        raise HopValidationError("Aden: not yet instrumented (see compass_test/README.md connector status table)")

    fromChain, stable = _parse_chain_stable(from_chain_name, stable_name)
    toChain, _ = _parse_chain_stable(to_chain_name, stable_name)
    if fromChain == toChain:
        raise HopValidationError(f"a Bridge needs two different chains, got {fromChain.name} -> {toChain.name}")
    if fromChain not in dex.chains or toChain not in dex.chains or stable not in dex.stables:
        raise HopValidationError(
            f"Aden's bridge does not support {stable.name} {fromChain.name}->{toChain.name} "
            f"(chains={[c.name for c in dex.chains]}, stables={[s.name for s in dex.stables]})"
        )

    planned = plan_loader.build_bridge_hop_estimate(fromChain, toChain, stable)
    # Reuse of minDepositUsd/minWithdrawUsd (Withdraw/Deposit's own fields) to
    # carry BOTH legs' floors on a Bridge hop: the amount must clear Aden's
    # minimum deposit on fromChain AND its minimum withdraw on toChain, same
    # convention cli.py already uses for a whole journey's default amount
    # (see PlannedHop.minDepositUsd's own docstring).
    planned.minDepositUsd = dex.minDepositUsdByChain[fromChain]
    planned.minWithdrawUsd = dex.minWithdrawUsdByChain[toChain]
    return fromChain, toChain, stable, planned


def resolve_cctp_bridge_hop(from_chain_name: str, stable_name: str, to_chain_name: str):
    """CCTP counterpart of resolve_bridge_hop — no DEX registry entry to
    validate against (see compass_test/cctp_runner.py: there's no DEX on
    either side of this bridge), so the check is directly against
    graph.structures.bridges.availableBridgeProtocols offering CCTP for
    this route, same source of truth plan_loader.build_bridge_hop_estimate
    uses to pick the protocol. Returns (fromChain, toChain, stable,
    PlannedHop) — no min deposit/withdraw floor to carry (unlike Aden's
    bridge, CCTP has no DEX-side minimum; see run_single_hop's
    config.DEFAULT_CCTP_TEST_USD fallback when no amount is given)."""
    fromChain, stable = _parse_chain_stable(from_chain_name, stable_name)
    toChain, _ = _parse_chain_stable(to_chain_name, stable_name)
    if fromChain == toChain:
        raise HopValidationError(f"a Bridge needs two different chains, got {fromChain.name} -> {toChain.name}")
    if BridgeProtocol.CCTP not in availableBridgeProtocols(fromChain, toChain, stable):
        raise HopValidationError(
            f"CCTP does not support {stable.name} {fromChain.name}->{toChain.name} — this project only wires "
            "ARBITRUM<->SOLANA USDC (see graph.structures.bridges._CCTP_CHAINS, compass_test/cctp_runner.py)"
        )
    planned = plan_loader.build_bridge_hop_estimate(fromChain, toChain, stable)
    return fromChain, toChain, stable, planned


def resolve_hop(dex_name: str, hop_type: HopType, chain_name: str, stable_name: str):
    """Validation + estimate only — no execution. Used by both
    run_single_hop below and by callers (e.g. the frontend, before it even
    shows a "Test This Edge" button) that want to know whether a hop is
    testable without running anything. Withdraw/Deposit only — a Swap goes
    through resolve_swap_hop (no DEX, amount-dependent estimate), a Bridge
    through resolve_bridge_hop (no DEX, two chains instead of one)."""
    if hop_type in (HopType.SWAP, HopType.BRIDGE):
        raise HopValidationError(
            f"resolve_hop handles Withdraw/Deposit only — use resolve_{hop_type.value.lower()}_hop for a {hop_type.value}"
        )
    registry = plan_loader.load_configured_dex_registry()
    dex = registry.get(dex_name)
    if dex is None:
        raise HopValidationError(f"Unknown DEX {dex_name!r}. Known DEXes: {', '.join(sorted(registry))}")
    if not is_supported(dex_name):
        raise HopValidationError(f"{dex_name}: not yet instrumented (see compass_test/README.md connector status table)")

    chain, stable = _parse_chain_stable(chain_name, stable_name)

    if chain not in dex.chains or stable not in dex.stables:
        raise HopValidationError(
            f"{dex_name} does not support {stable.name} on {chain.name} "
            f"(chains={[c.name for c in dex.chains]}, stables={[s.name for s in dex.stables]})"
        )

    return dex, chain, stable, plan_loader.build_hop_estimate(dex, hop_type, chain, stable)


def run_single_hop(
    dex_name: str,
    hop_type: HopType,
    chain_name: str,
    stable_name: str,
    amount_usd: float | None = None,
    live: bool = False,
    wallet: OperatingWallet | None = None,
    confirm: Callable[[object, float, str], bool] | None = None,
    to_stable_name: str | None = None,
    to_chain_name: str | None = None,
    on_stage: Callable[[str, str, str], None] | None = None,
) -> HopRunResult:
    """`confirm(planned, amount, wallet_address) -> bool`, called only when
    `live` is True, right before execution — the CLI passes an interactive
    typed-YES prompt here; the HTTP endpoint passes None and instead
    requires a confirmation token in the request body BEFORE ever calling
    this function (see server.py) — two different UIs, same underlying
    safety gate (executor.run_hop's own ALLOW_LIVE + $ cap checks) either
    way.

    `to_stable_name` is the bought stable of a Swap hop (`stable_name` is
    the one sold); ignored otherwise. `to_chain_name` is the withdraw chain
    of a Bridge hop (`chain_name` is the deposit chain); ignored otherwise.
    A Swap's `dex_name` is the venue, COWSWAP_VENUE_NAME. A Bridge's is
    "Aden" or "CCTP", picked automatically from `chain_name`/`to_chain_name`/
    `stable_name` via graph.structures.bridges.availableBridgeProtocols when
    left blank — passing the OTHER protocol's name for a given route is
    rejected rather than silently routed.

    `on_stage`, passed straight through to executor.run_hop, is how a LIVE
    run's progress (on-chain leg confirmed vs now waiting on the exchange's
    own side, etc.) reaches a caller that wants to show it before the whole
    hop finishes — see server.py's streamed POST /api/test-hop."""
    usedConfiguredMinimum = False
    amount = amount_usd
    if hop_type == HopType.SWAP:
        if dex_name not in (COWSWAP_VENUE_NAME, ""):
            raise HopValidationError(f"a Swap hop runs through {COWSWAP_VENUE_NAME!r}, not {dex_name!r}")
        dex_name = COWSWAP_VENUE_NAME
        if amount is None:
            amount = config.DEFAULT_SWAP_TEST_USD
            usedConfiguredMinimum = True
        chain, stable, _stable_out, planned = resolve_swap_hop(chain_name, stable_name, to_stable_name, amount)
    elif hop_type == HopType.BRIDGE:
        if not to_chain_name:
            raise HopValidationError("a Bridge hop needs the withdraw chain (toChain), e.g. chain=ARBITRUM toChain=BSC")
        # Protocol picked the same way plan_loader/the solver itself picks
        # it (availableBridgeProtocols), BEFORE requiring a specific `dex`
        # name — so a caller that leaves dex_name blank (the frontend/CLI
        # default) gets routed automatically instead of always landing on
        # Aden. The two protocols' chain sets never overlap today (Aden:
        # BSC<->ARBITRUM, CCTP: ARBITRUM<->SOLANA), so this is never
        # ambiguous in practice.
        fromChainCheck, stableCheck = _parse_chain_stable(chain_name, stable_name)
        toChainCheck, _ = _parse_chain_stable(to_chain_name, stable_name)
        isCctpRoute = BridgeProtocol.CCTP in availableBridgeProtocols(fromChainCheck, toChainCheck, stableCheck)
        if isCctpRoute:
            if dex_name not in ("CCTP", ""):
                raise HopValidationError(f"this Bridge route runs through CCTP, not {dex_name!r}")
            dex_name = "CCTP"
            chain, _to_chain, stable, planned = resolve_cctp_bridge_hop(chain_name, stable_name, to_chain_name)
            if amount is None:
                amount = config.DEFAULT_CCTP_TEST_USD
                usedConfiguredMinimum = True
        else:
            if dex_name not in ("Aden", ""):
                raise HopValidationError(f"a Bridge hop runs through Aden, not {dex_name!r}")
            dex_name = "Aden"
            chain, _to_chain, stable, planned = resolve_bridge_hop(chain_name, stable_name, to_chain_name)
            if amount is None:
                floor = max(planned.minDepositUsd, planned.minWithdrawUsd)
                if floor <= 0:
                    raise HopValidationError(
                        "No amount given and Aden's configured minimum deposit/withdraw is 0/unset. Set it in the "
                        'Config tab ("Min deposit"/"Min withdraw") or pass an amount explicitly.'
                    )
                amount = floor
                usedConfiguredMinimum = True
    else:
        dex, chain, stable, planned = resolve_hop(dex_name, hop_type, chain_name, stable_name)
        if amount is None:
            floor = planned.minWithdrawUsd if hop_type == HopType.WITHDRAW else planned.minDepositUsd
            if floor <= 0:
                kind = "withdraw" if hop_type == HopType.WITHDRAW else "deposit"
                raise HopValidationError(
                    f"No amount given and {dex_name}'s configured minimum {kind} is 0/unset. "
                    f'Set it in the Config tab ("Min {kind}") or pass an amount explicitly.'
                )
            amount = floor
            usedConfiguredMinimum = True

    wallet = wallet or OperatingWallet(known_address=config.OPERATING_WALLET_ADDRESS)

    if live and confirm is not None and not confirm(planned, amount, wallet.address):
        raise HopAborted("live run declined at confirmation")

    # Neither a Swap nor a CCTP bridge has a DexConnector — CCTP is
    # wallet-to-wallet (see compass_test/cctp_runner.py), same reason a
    # Swap has none (its venue is CoW Swap, not a DEX in the registry).
    connector = None if hop_type == HopType.SWAP or dex_name == "CCTP" else get_connector(dex_name)
    executed = executor.run_hop(planned, connector, amount, wallet, live, on_stage=on_stage)

    hop_comparison = comparator.compare_hop(planned, executed)
    journey_comparison = comparator.compare_journey(
        plan_loader.PlannedJourney(fromDex=dex_name, toDex=dex_name, stable=stable_name, hops=[planned]),
        [hop_comparison],
    )
    report = comparator.build_report(live=live, journeys=[journey_comparison], unsupported_dexes=[])
    path = reporter.save_report(report)

    return HopRunResult(
        comparison=hop_comparison,
        report=report,
        reportPath=str(path),
        resolvedAmountUsd=amount,
        usedConfiguredMinimum=usedConfiguredMinimum,
    )
