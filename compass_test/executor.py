"""Runs one PlannedHop (see plan_loader.py) for real (--live) or as a dry run
(default), producing an ExecutedHop with the actual cost/time measured the
way described in README.md "What we measure":

  Withdraw: actualCostUsd = amountRequested - amountReceivedOnChain (the
            exchange's own gas is invisible to us, only its fee isn't).
            Live-only — an exchange's processing time/fee schedule can't be
            simulated, so a dry run reports status="dry_run" with no number.

  Deposit:  actualCostUsd = real gas paid (gasUsed * effectiveGasPrice, USD
            at confirmation time). A dry run still gets a real number here —
            eth_estimateGas simulates against live chain state without
            signing/sending anything.
"""

from __future__ import annotations

import time
from typing import Callable

from graph.structures.DEXes import Chain, Stable

from . import chain_ops, config
from .models import ExecutedHop, HopType, PlannedHop
from .runners.base import DexConnector
from .wallet import OperatingWallet


class SafetyCapError(RuntimeError):
    pass


def _check_caps(amount_usd: float, spent_so_far: float) -> None:
    if amount_usd > config.MAX_USD_PER_HOP:
        raise SafetyCapError(f"${amount_usd:.2f} exceeds COMPASS_TEST_MAX_USD_PER_HOP=${config.MAX_USD_PER_HOP:.2f}")
    if spent_so_far + amount_usd > config.MAX_USD_PER_RUN:
        raise SafetyCapError(
            f"cumulative ${spent_so_far + amount_usd:.2f} would exceed "
            f"COMPASS_TEST_MAX_USD_PER_RUN=${config.MAX_USD_PER_RUN:.2f}"
        )


def run_hop(
    planned: PlannedHop,
    connector: DexConnector,
    amount_usd: float,
    wallet: OperatingWallet,
    live: bool,
    spent_so_far: float = 0.0,
) -> ExecutedHop:
    if live and not config.ALLOW_LIVE:
        raise SafetyCapError(
            "live run requested (--live) but COMPASS_TEST_ALLOW_LIVE=1 is not set in the "
            "environment — both are required (see config.py)"
        )
    _check_caps(amount_usd, spent_so_far)

    chain = Chain[planned.chain]
    stable = Stable[planned.stable]
    if planned.hopType == HopType.WITHDRAW:
        return _run_withdraw(connector, chain, stable, amount_usd, wallet, live)
    return _run_deposit(connector, chain, stable, amount_usd, wallet, live)


def _poll_until(condition: Callable[[], bool], timeout_s: float, interval_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(interval_s)
    return False


def _run_withdraw(
    connector: DexConnector, chain: Chain, stable: Stable, amount_usd: float, wallet: OperatingWallet, live: bool
) -> ExecutedHop:
    startedAt = time.time()
    if not live:
        return ExecutedHop(
            live=False,
            startedAt=startedAt,
            finishedAt=time.time(),
            amountRequestedUsd=amount_usd,
            actualCostUsd=None,
            status="dry_run",
            notes="withdraw fee/time can only be measured live — the exchange, not us, controls both",
        )

    w3 = chain_ops.get_web3(chain)
    before_usd = chain_ops.get_stable_balance_usd(w3, chain, stable, wallet.address)
    result = connector.withdraw(chain, stable, amount_usd, wallet.address)

    received_usd: list[float] = []

    def _credited() -> bool:
        current = chain_ops.get_stable_balance_usd(w3, chain, stable, wallet.address)
        if current > before_usd + 1e-6:
            received_usd.append(current - before_usd)
            return True
        return False

    confirmed = _poll_until(_credited, config.POLL_TIMEOUT_SECONDS, config.POLL_INTERVAL_SECONDS)
    finishedAt = time.time()

    if not confirmed:
        return ExecutedHop(
            live=True,
            startedAt=startedAt,
            finishedAt=finishedAt,
            amountRequestedUsd=amount_usd,
            actualCostUsd=None,
            externalId=result.externalId,
            status="unconfirmed",
            notes=(
                f"withdraw accepted (id={result.externalId}) but {wallet.address} balance never increased "
                f"within {config.POLL_TIMEOUT_SECONDS:.0f}s — check the exchange/explorer manually"
            ),
        )

    actual_fee_usd = max(amount_usd - received_usd[0], 0.0)
    return ExecutedHop(
        live=True,
        startedAt=startedAt,
        finishedAt=finishedAt,
        amountRequestedUsd=amount_usd,
        actualCostUsd=actual_fee_usd,
        amountReceivedUsd=received_usd[0],
        externalId=result.externalId,
        status="ok",
    )


def _run_deposit(
    connector: DexConnector, chain: Chain, stable: Stable, amount_usd: float, wallet: OperatingWallet, live: bool
) -> ExecutedHop:
    startedAt = time.time()
    w3 = chain_ops.get_web3(chain)
    try:
        tx = connector.build_deposit_tx(w3, wallet.address, chain, stable, amount_usd)
    except Exception as exc:  # noqa: BLE001 - surfaced in the report, not swallowed
        return ExecutedHop(
            live=live,
            startedAt=startedAt,
            finishedAt=time.time(),
            amountRequestedUsd=amount_usd,
            actualCostUsd=None,
            status="error",
            notes=f"could not build deposit tx: {exc}",
        )

    if not live:
        cost_usd = chain_ops.estimate_dry_run_gas_cost_usd(w3, chain, tx)
        return ExecutedHop(
            live=False,
            startedAt=startedAt,
            finishedAt=time.time(),
            amountRequestedUsd=amount_usd,
            actualCostUsd=cost_usd,
            status="dry_run",
            notes="gas simulated live (eth_estimateGas against current chain state) — nothing signed or sent",
        )

    before_balance = connector.poll_balance_usd(stable)
    signed = wallet.sign_transaction(tx)
    onchain = chain_ops.send_and_wait(w3, chain, signed)

    def _credited() -> bool:
        return connector.poll_balance_usd(stable) > before_balance + 1e-6

    confirmed = _poll_until(_credited, config.POLL_TIMEOUT_SECONDS, config.POLL_INTERVAL_SECONDS)
    finishedAt = time.time()

    return ExecutedHop(
        live=True,
        startedAt=startedAt,
        finishedAt=finishedAt,
        amountRequestedUsd=amount_usd,
        actualCostUsd=onchain.gas_cost_usd,
        amountReceivedUsd=amount_usd if confirmed else None,
        txHash=onchain.tx_hash,
        status="ok" if confirmed else "unconfirmed",
        notes=(
            ""
            if confirmed
            else f"tx confirmed on-chain ({onchain.tx_hash}) but the DEX balance never reflected it "
            f"within {config.POLL_TIMEOUT_SECONDS:.0f}s"
        ),
    )


def run_journey_hops(
    planned_hops: list[PlannedHop],
    connector_for_dex: Callable[[str], DexConnector],
    amount_usd: float,
    wallet: OperatingWallet,
    live: bool,
    spent_so_far: float = 0.0,
) -> list[ExecutedHop]:
    """Runs a journey's hops back to back (Withdraw then Deposit). The
    deposit leg's amount is the withdraw leg's ACTUALLY received amount when
    that ran live (we can only deposit what we actually got), not the
    originally requested test amount."""
    executed: list[ExecutedHop] = []
    spent = spent_so_far
    carried_amount_usd: float | None = None

    for planned in planned_hops:
        amount = carried_amount_usd if carried_amount_usd is not None else amount_usd
        connector = connector_for_dex(planned.dex)
        result = run_hop(planned, connector, amount, wallet, live, spent_so_far=spent)
        executed.append(result)
        spent += amount
        if planned.hopType == HopType.WITHDRAW and result.amountReceivedUsd is not None:
            carried_amount_usd = result.amountReceivedUsd

    return executed
