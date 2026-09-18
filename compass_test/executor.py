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

  Swap:     (CoW Swap, same chain, stable -> stable, see runners/cowswap.py)
            actualCostUsd = amountSold - amountBought (CoW's network fee and
            the price impact, all-in — the wallet pays no gas for the trade
            itself) + the gas of the ERC-20 approve tx when one was needed.
            A dry run gets a real number too: a live CoW quote prices
            exactly that (fee + impact) without signing anything, plus the
            simulated approve gas if the allowance is missing.
"""

from __future__ import annotations

import time
from typing import Callable

from graph.structures.DEXes import Chain, Stable

from connectors import cowswap

from . import chain_ops, config
from .models import ExecutedHop, HopType, PlannedHop
from .runners.base import DexConnector
from .runners.cowswap import CowSwapRunner
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


_NOOP_STAGE: Callable[[str, str, str], None] = lambda stage, message, domain: None  # noqa: E731


def _fmt_duration(seconds: float) -> str:
    """Mirrors the frontend's own fmtDuration (graph_template.html) so a
    stage message quoting an ETA (e.g. "usually ~2m30s") reads the same
    unit style as the rest of the page."""
    s = round(seconds)
    m, s = divmod(s, 60)
    return f"{m}m{s:02d}s" if m > 0 else f"{s}s"


def run_hop(
    planned: PlannedHop,
    connector: DexConnector | None,
    amount_usd: float,
    wallet: OperatingWallet,
    live: bool,
    spent_so_far: float = 0.0,
    swap_runner: CowSwapRunner | None = None,
    on_stage: Callable[[str, str, str], None] | None = None,
) -> ExecutedHop:
    """`connector` is the DEX behind a Withdraw/Deposit hop; a Swap hop has
    no DEX and ignores it (its venue is `swap_runner`, CoW Swap by default).
    Every hop type goes through the same live gate and $ caps first.

    `on_stage(stage_code, message, domain)`, when given, is called at each
    notable transition of a LIVE run only (dry runs are fast enough
    end-to-end that staged progress doesn't matter). `domain` is always
    either "onchain" (this wallet's own signed transaction) or "exchange"
    (the DEX/protocol's own internal processing, off-chain from our point
    of view) — the frontend's execution animation needs that distinction
    front and center (a deposit landing on-chain is NOT the same event as
    the DEX crediting it internally, and conflating them is exactly the
    confusion this callback exists to avoid), and a single before/after
    ExecutedHop can't carry it. See visualization/server.py POST
    /api/test-hop, which streams these as they happen instead of buffering
    them until the whole hop finishes."""
    on_stage = on_stage or _NOOP_STAGE
    if live and not config.ALLOW_LIVE:
        raise SafetyCapError(
            "live run requested (--live) but COMPASS_TEST_ALLOW_LIVE=1 is not set in the "
            "environment — both are required (see config.py)"
        )
    _check_caps(amount_usd, spent_so_far)

    chain = Chain[planned.chain]
    stable = Stable[planned.stable]
    if planned.hopType == HopType.WITHDRAW:
        return _run_withdraw(connector, chain, stable, amount_usd, wallet, live, on_stage, planned.estimatedTimeSeconds)
    if planned.hopType == HopType.SWAP:
        return _run_swap(
            swap_runner or CowSwapRunner(),
            chain,
            stable,
            Stable[planned.toStable],
            amount_usd,
            wallet,
            live,
            on_stage,
            planned.estimatedTimeSeconds,
        )
    if planned.hopType == HopType.BRIDGE:
        return _run_bridge(
            connector,
            chain,
            Chain[planned.toChain],
            stable,
            amount_usd,
            wallet,
            live,
            on_stage,
            planned.estimatedTimeSeconds,
        )
    return _run_deposit(connector, chain, stable, amount_usd, wallet, live, on_stage, planned.estimatedTimeSeconds)


def _poll_until(condition: Callable[[], bool], timeout_s: float, interval_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(interval_s)
    return False


def _run_withdraw(
    connector: DexConnector,
    chain: Chain,
    stable: Stable,
    amount_usd: float,
    wallet: OperatingWallet,
    live: bool,
    on_stage: Callable[[str, str, str], None] = _NOOP_STAGE,
    estimated_time_s: float = 0.0,
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

    on_stage("requesting", f"Requesting the withdrawal from {connector.name}…", "exchange")
    w3 = chain_ops.get_web3(chain)
    before_usd = chain_ops.get_stable_balance_usd(w3, chain, stable, wallet.address)
    result = connector.withdraw(chain, stable, amount_usd, wallet.address)

    on_stage("dex_accepted", f"{connector.name} accepted the withdrawal (id {result.externalId}).", "exchange")
    eta_note = f" (usually ~{_fmt_duration(estimated_time_s)})" if estimated_time_s > 0 else ""
    on_stage("waiting_onchain", f"Waiting for the funds to land on {chain.name}{eta_note}…", "onchain")

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
    connector: DexConnector,
    chain: Chain,
    stable: Stable,
    amount_usd: float,
    wallet: OperatingWallet,
    live: bool,
    on_stage: Callable[[str, str, str], None] = _NOOP_STAGE,
    estimated_time_s: float = 0.0,
) -> ExecutedHop:
    startedAt = time.time()
    w3 = chain_ops.get_web3(chain)
    if live:
        # build_deposit_tx can itself be a real API call to the DEX (e.g.
        # Ondo's connector provisions/looks up a deposit address here, see
        # runners/ondo.py) — without this, that lookup used to happen
        # silently between the frontend's generic "Connecting to the
        # server…" placeholder and the first real stage, which read as the
        # popup being stuck if the DEX's API was slow to answer.
        on_stage("preparing", f"Preparing the deposit with {connector.name}…", "exchange")
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
    on_stage("signing", "Submitting the on-chain transaction…", "onchain")
    signed = wallet.sign_transaction(tx)
    onchain = chain_ops.send_and_wait(w3, chain, signed)
    txConfirmedAt = time.time()

    on_stage("onchain_confirmed", f"Transaction validated on-chain (tx {onchain.tx_hash}).", "onchain")
    eta_note = f" (usually ~{_fmt_duration(estimated_time_s)})" if estimated_time_s > 0 else ""
    on_stage("waiting_dex", f"Waiting for {connector.name} to process the deposit{eta_note}…", "exchange")

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
        txConfirmedAt=txConfirmedAt,
        status="ok" if confirmed else "unconfirmed",
        notes=(
            ""
            if confirmed
            else f"tx confirmed on-chain ({onchain.tx_hash}) but the DEX balance never reflected it "
            f"within {config.POLL_TIMEOUT_SECONDS:.0f}s"
        ),
    )


def _run_bridge(
    connector: DexConnector,
    fromChain: Chain,
    toChain: Chain,
    stable: Stable,
    amount_usd: float,
    wallet: OperatingWallet,
    live: bool,
    on_stage: Callable[[str, str, str], None] = _NOOP_STAGE,
    estimated_time_s: float = 0.0,
) -> ExecutedHop:
    """Aden's internal bridge: a deposit into Aden on `fromChain` followed by
    a withdraw from Aden on `toChain` — literally what the graph model
    already treats as a single combined-cost/time edge (costing.py's
    EdgeType.Bridge branch). Reuses _run_deposit/_run_withdraw verbatim
    (same mechanics, already exercised standalone) rather than
    reimplementing either leg, and merges their two ExecutedHops into the
    ONE the caller sees."""
    startedAt = time.time()
    deposit_result = _run_deposit(connector, fromChain, stable, amount_usd, wallet, live, on_stage, estimated_time_s)
    if deposit_result.status not in ("ok", "dry_run"):
        # The deposit leg itself failed or never got credited — funds may
        # already be sitting at Aden on fromChain, uncredited toward a
        # toChain withdraw. Surface that distinctly rather than attempting
        # to withdraw an amount that was never actually deposited.
        return ExecutedHop(
            live=live,
            startedAt=startedAt,
            finishedAt=deposit_result.finishedAt,
            amountRequestedUsd=amount_usd,
            actualCostUsd=deposit_result.actualCostUsd,
            txHash=deposit_result.txHash,
            status=deposit_result.status,
            notes=f"bridge deposit leg ({fromChain.name}) did not complete, withdraw leg not attempted: {deposit_result.notes}",
        )

    if not live:
        # Withdraw fee/time can only be measured live (see _run_withdraw) —
        # a dry run's bridge cost is just the deposit leg's simulated gas,
        # same convention _run_withdraw itself uses for a standalone dry run.
        return ExecutedHop(
            live=False,
            startedAt=startedAt,
            finishedAt=deposit_result.finishedAt,
            amountRequestedUsd=amount_usd,
            actualCostUsd=deposit_result.actualCostUsd,
            status="dry_run",
            notes=(
                f"bridge deposit leg ({fromChain.name}) simulated gas ${deposit_result.actualCostUsd or 0.0:.4f}; "
                f"the withdraw leg ({toChain.name}) fee/time can only be measured live"
            ),
        )

    deposit_received_usd = deposit_result.amountReceivedUsd if deposit_result.amountReceivedUsd is not None else amount_usd
    withdraw_result = _run_withdraw(
        connector, toChain, stable, deposit_received_usd, wallet, live, on_stage, estimated_time_s
    )
    combined_cost = (
        (deposit_result.actualCostUsd or 0.0) + (withdraw_result.actualCostUsd or 0.0)
        if withdraw_result.status == "ok"
        else None
    )
    notes = f"deposit leg ({fromChain.name}) cost ${deposit_result.actualCostUsd or 0.0:.4f}"
    if withdraw_result.notes:
        notes += f"; withdraw leg ({toChain.name}): {withdraw_result.notes}"
    return ExecutedHop(
        live=True,
        startedAt=startedAt,
        finishedAt=withdraw_result.finishedAt,
        amountRequestedUsd=amount_usd,
        actualCostUsd=combined_cost,
        amountReceivedUsd=withdraw_result.amountReceivedUsd,
        txHash=deposit_result.txHash,
        externalId=withdraw_result.externalId,
        status=withdraw_result.status,
        notes=notes,
    )


def _run_swap(
    runner: CowSwapRunner,
    chain: Chain,
    stable_in: Stable,
    stable_out: Stable,
    amount_usd: float,
    wallet: OperatingWallet,
    live: bool,
    on_stage: Callable[[str, str, str], None] = _NOOP_STAGE,
    estimated_time_s: float = 0.0,
) -> ExecutedHop:
    """Sell `amount_usd` of stable_in for stable_out through CoW Swap. The
    order's sell amount is exactly `amount_usd` (network fee included, see
    connectors.cowswap.build_order_from_quote) so the $ caps checked above
    bound what can actually leave the wallet."""
    startedAt = time.time()
    if not runner.supports(chain, stable_in, stable_out):
        return ExecutedHop(
            live=live,
            startedAt=startedAt,
            finishedAt=time.time(),
            amountRequestedUsd=amount_usd,
            actualCostUsd=None,
            status="error",
            notes=f"{runner.name} does not support {stable_in.name}->{stable_out.name} on {chain.name}",
        )

    w3 = chain_ops.get_web3(chain)
    try:
        quote = runner.quote(chain, stable_in, stable_out, amount_usd, wallet.address)
    except Exception as exc:  # noqa: BLE001 - surfaced in the report, not swallowed
        return ExecutedHop(
            live=live,
            startedAt=startedAt,
            finishedAt=time.time(),
            amountRequestedUsd=amount_usd,
            actualCostUsd=None,
            status="error",
            notes=f"could not get a {runner.name} quote: {exc}",
        )

    sold_usd = chain_ops.token_units_to_usd(chain, stable_in, quote.sell_amount_before_fee)
    quoted_buy_usd = chain_ops.token_units_to_usd(chain, stable_out, quote.buy_amount)
    fee_usd = chain_ops.token_units_to_usd(chain, stable_in, quote.fee_amount)
    shortfall = runner.allowance_shortfall_units(w3, chain, stable_in, wallet.address, quote.sell_amount_before_fee)

    approve_tx: dict | None = None
    if shortfall > 0:
        try:
            approve_tx = runner.build_approve_tx(w3, chain, stable_in, wallet.address, amount_usd)
        except Exception as exc:  # noqa: BLE001
            return ExecutedHop(
                live=live,
                startedAt=startedAt,
                finishedAt=time.time(),
                amountRequestedUsd=amount_usd,
                actualCostUsd=None,
                status="error",
                notes=f"could not build the approve tx for {cowswap.VAULT_RELAYER}: {exc}",
            )

    if not live:
        approve_gas_usd = chain_ops.estimate_dry_run_gas_cost_usd(w3, chain, approve_tx) if approve_tx else 0.0
        approve_note = (
            f"; an ERC-20 approve to CoW's vault relayer would be needed first (simulated gas ${approve_gas_usd:.4f}, included)"
            if approve_tx
            else "; existing allowance covers it, no approve tx needed"
        )
        return ExecutedHop(
            live=False,
            startedAt=startedAt,
            finishedAt=time.time(),
            amountRequestedUsd=amount_usd,
            actualCostUsd=max(sold_usd - quoted_buy_usd, 0.0) + approve_gas_usd,
            amountReceivedUsd=quoted_buy_usd,
            externalId=f"quote:{quote.quote_id}" if quote.quote_id is not None else None,
            status="dry_run",
            notes=(
                f"live {runner.name} quote (id {quote.quote_id}, verified={quote.verified}): sell ${sold_usd:.4f} {stable_in.name} "
                f"-> buy ${quoted_buy_usd:.4f} {stable_out.name}, network fee ${fee_usd:.4f}{approve_note} — nothing signed or sent"
            ),
        )

    approve_gas_usd = 0.0
    approve_hash: str | None = None
    if approve_tx is not None:
        on_stage("approving", f"Approving {runner.name}'s vault relayer to spend {stable_in.name}…", "onchain")
        signed = wallet.sign_transaction(approve_tx)
        onchain = chain_ops.send_and_wait(w3, chain, signed)
        approve_gas_usd = onchain.gas_cost_usd
        approve_hash = onchain.tx_hash
        on_stage("approved", f"Approval confirmed on-chain (tx {approve_hash}).", "onchain")

    before_out_usd = chain_ops.get_stable_balance_usd(w3, chain, stable_out, wallet.address)
    placed = runner.place_order(chain, quote, wallet)
    on_stage("order_placed", f"Order placed on {runner.name} (uid {placed.uid}).", "exchange")
    eta_note = f" (usually ~{_fmt_duration(estimated_time_s)})" if estimated_time_s > 0 else ""
    on_stage("waiting_settlement", f"Waiting for {runner.name} to fill the order{eta_note}…", "exchange")
    outcome = runner.wait_for_settlement(chain, placed.uid, config.POLL_TIMEOUT_SECONDS, config.POLL_INTERVAL_SECONDS)
    approve_note = f" (approve tx {approve_hash}, gas ${approve_gas_usd:.4f} included)" if approve_hash else ""

    if outcome.status == cowswap.ORDER_STATUS_OPEN:
        # Never leave a live order behind after giving up on it: cancel, so
        # it can't settle later unobserved. Best effort — the UID is in the
        # report either way for a manual check.
        try:
            runner.cancel(chain, placed.uid, wallet)
            cancel_note = "order cancelled"
        except Exception as exc:  # noqa: BLE001
            cancel_note = f"CANCELLATION FAILED ({exc}) — cancel it manually on explorer.cow.fi"
        return ExecutedHop(
            live=True,
            startedAt=startedAt,
            finishedAt=outcome.finished_at,
            amountRequestedUsd=amount_usd,
            actualCostUsd=None,
            txHash=approve_hash,
            externalId=placed.uid,
            status="unconfirmed",
            notes=f"{runner.name} order {placed.uid} still open after {config.POLL_TIMEOUT_SECONDS:.0f}s — {cancel_note}{approve_note}",
        )

    if outcome.status != cowswap.ORDER_STATUS_FULFILLED:
        return ExecutedHop(
            live=True,
            startedAt=startedAt,
            finishedAt=outcome.finished_at,
            amountRequestedUsd=amount_usd,
            actualCostUsd=None,
            txHash=approve_hash,
            externalId=placed.uid,
            status="error",
            notes=(
                f"{runner.name} order {placed.uid} ended {outcome.status} without filling (min buy was "
                f"${chain_ops.token_units_to_usd(chain, stable_out, placed.order.buy_amount):.4f} {stable_out.name}, "
                f"slippage {config.SWAP_SLIPPAGE_BPS} bps){approve_note}"
            ),
        )

    # Authoritative numbers from the orderbook's executed amounts, cross-checked
    # against the wallet's real on-chain balance of the bought stable.
    bought_usd = chain_ops.token_units_to_usd(chain, stable_out, outcome.executed_buy_units)
    sold_actual_usd = chain_ops.token_units_to_usd(chain, stable_in, outcome.executed_sell_units) or sold_usd
    after_out_usd = chain_ops.get_stable_balance_usd(w3, chain, stable_out, wallet.address)
    onchain_delta_usd = after_out_usd - before_out_usd
    mismatch_note = (
        ""
        if abs(onchain_delta_usd - bought_usd) < 1e-4
        else f"; on-chain {stable_out.name} balance moved ${onchain_delta_usd:.4f} vs executedBuyAmount ${bought_usd:.4f}"
    )
    return ExecutedHop(
        live=True,
        startedAt=startedAt,
        finishedAt=outcome.finished_at,
        amountRequestedUsd=amount_usd,
        actualCostUsd=max(sold_actual_usd - bought_usd, 0.0) + approve_gas_usd,
        amountReceivedUsd=bought_usd,
        txHash=outcome.tx_hash or approve_hash,
        externalId=placed.uid,
        status="ok",
        notes=(
            f"{runner.name} order filled: sold ${sold_actual_usd:.4f} {stable_in.name} -> bought ${bought_usd:.4f} {stable_out.name} "
            f"(quoted ${quoted_buy_usd:.4f}, settlement tx {outcome.tx_hash}){approve_note}{mismatch_note}"
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
    """Runs a journey's hops back to back (Withdraw, optionally Swap, then
    Deposit). Each next leg's amount is the previous leg's ACTUALLY received
    amount when that ran live (we can only swap/deposit what we actually
    got), not the originally requested test amount."""
    executed: list[ExecutedHop] = []
    spent = spent_so_far
    carried_amount_usd: float | None = None

    for planned in planned_hops:
        amount = carried_amount_usd if carried_amount_usd is not None else amount_usd
        connector = None if planned.hopType == HopType.SWAP else connector_for_dex(planned.dex)
        result = run_hop(planned, connector, amount, wallet, live, spent_so_far=spent)
        executed.append(result)
        spent += amount
        if planned.hopType in (HopType.WITHDRAW, HopType.SWAP, HopType.BRIDGE) and live and result.amountReceivedUsd is not None:
            carried_amount_usd = result.amountReceivedUsd

    return executed
