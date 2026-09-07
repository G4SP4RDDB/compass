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

from graph.structures.DEXes import Chain, Stable

from . import comparator, config, executor, plan_loader, reporter
from .models import HopComparison, HopType, TestRunReport
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


def resolve_hop(dex_name: str, hop_type: HopType, chain_name: str, stable_name: str):
    """Validation + estimate only — no execution. Used by both
    run_single_hop below and by callers (e.g. the frontend, before it even
    shows a "Test This Edge" button) that want to know whether a hop is
    testable without running anything."""
    registry = plan_loader.load_configured_dex_registry()
    dex = registry.get(dex_name)
    if dex is None:
        raise HopValidationError(f"Unknown DEX {dex_name!r}. Known DEXes: {', '.join(sorted(registry))}")
    if not is_supported(dex_name):
        raise HopValidationError(f"{dex_name}: not yet instrumented (see compass_test/README.md connector status table)")

    try:
        chain = Chain[chain_name]
        stable = Stable[stable_name]
    except KeyError as exc:
        raise HopValidationError(f"invalid chain/stable: {exc}") from exc

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
) -> HopRunResult:
    """`confirm(planned, amount, wallet_address) -> bool`, called only when
    `live` is True, right before execution — the CLI passes an interactive
    typed-YES prompt here; the HTTP endpoint passes None and instead
    requires a confirmation token in the request body BEFORE ever calling
    this function (see server.py) — two different UIs, same underlying
    safety gate (executor.run_hop's own ALLOW_LIVE + $ cap checks) either
    way."""
    dex, chain, stable, planned = resolve_hop(dex_name, hop_type, chain_name, stable_name)

    usedConfiguredMinimum = False
    amount = amount_usd
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

    connector = get_connector(dex_name)
    executed = executor.run_hop(planned, connector, amount, wallet, live)

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
