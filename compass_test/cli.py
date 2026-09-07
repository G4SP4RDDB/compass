"""CLI entry point. Run from the compass/ repo root:

    python -m compass_test.cli list-hops
    python -m compass_test.cli check-auth --dex MEXC --stable USDT
    python -m compass_test.cli run --from Aster --to MEXC --amount 5
    python -m compass_test.cli run --from Aster --to MEXC --amount 5 --live --yes

    # Test ONE DEX's Withdraw or Deposit directly — independent of whatever
    # journey the solver's demo-imbalance seed happens to produce (see
    # plan_loader.build_hop_estimate). Useful for proving out a single
    # connector (e.g. MEXC) before a second DEX's connector is ready:
    python -m compass_test.cli run-hop --dex MEXC --hop withdraw --chain ARBITRUM --stable USDT --amount 1
    python -m compass_test.cli run-hop --dex MEXC --hop deposit --chain ARBITRUM --stable USDT --amount 1 --live

`run` defaults to a dry run (no keys required beyond COMPASS_TEST_WALLET_ADDRESS,
no funds moved). A real transfer needs --live on the command line AND
COMPASS_TEST_ALLOW_LIVE=1 in the environment (see config.py) — and, unless
--yes is also given, an interactive typed confirmation showing exactly what
is about to move where.

Live execution from the frontend also exists now (the graph UI's "Test This
Edge" → "Run LIVE" button, see visualization/server.py POST /api/test-hop)
— gated the same way underneath (executor.run_hop's $ caps and
COMPASS_TEST_ALLOW_LIVE check) plus its own required confirmation token, see
that route's docstring. This CLI remains the simplest, most auditable way to
run one: no browser, no server process required.
"""

from __future__ import annotations

import argparse
import sys

from graph.structures.DEXes import Chain, Stable

from . import comparator, config, executor, plan_loader, reporter
from .hop_runner import HopAborted, HopValidationError, run_single_hop
from .models import HopType
from .runners.registry import get_connector
from .wallet import OperatingWallet


def _cmd_list_hops(_args: argparse.Namespace) -> int:
    graph = plan_loader.build_solved_graph()
    journeys = plan_loader.list_planned_journeys(graph)
    if not journeys:
        print("No Withdraw/Deposit journeys in the current solved graph.")
        return 0

    for j in journeys:
        scope = "IN SCOPE" if j.inScope else f"OUT OF SCOPE ({j.outOfScopeReason})"
        print(f"\n{j.fromDex} -> {j.toDex}  [{j.stable}]  — {scope}")
        for h in j.hops:
            if h.hopType == HopType.WITHDRAW:
                minLabel = f"  min withdraw=${h.minWithdrawUsd:.2f}"
            elif h.hopType == HopType.DEPOSIT:
                minLabel = f"  min deposit=${h.minDepositUsd:.2f}"
            else:
                minLabel = ""
            print(
                f"    {h.hopType.value:8s} {h.dex:16s} {h.chain:10s} "
                f"est.cost=${h.estimatedCostUsd:.4f}  est.time={h.estimatedTimeSeconds:.0f}s  "
                f"solved flow=${h.solvedFlowUsd:.2f}{minLabel}"
            )
    return 0


def _cmd_check_auth(args: argparse.Namespace) -> int:
    connector = get_connector(args.dex)
    stable = Stable[args.stable]
    balance = connector.poll_balance_usd(stable)
    print(f"{args.dex} {args.stable} available balance: ${balance:.4f} — auth OK")
    return 0


def _find_journey(journeys, from_dex: str, to_dex: str):
    for j in journeys:
        if j.fromDex == from_dex and j.toDex == to_dex:
            return j
    return None


def _confirm_live_run(planned_journey, amount_usd: float, wallet_address: str) -> bool:
    print("\n" + "=" * 70)
    print("LIVE RUN — real funds will move")
    print("=" * 70)
    print(f"Journey        : {planned_journey.fromDex} -> {planned_journey.toDex} ({planned_journey.stable})")
    print(f"Test amount    : ${amount_usd:.2f} per hop (caps: hop=${config.MAX_USD_PER_HOP:.2f}, "
          f"run=${config.MAX_USD_PER_RUN:.2f})")
    print(f"Operating wallet: {wallet_address}")
    for h in planned_journey.hops:
        print(f"  {h.hopType.value:8s} {h.dex:16s} on {h.chain}")
    print("=" * 70)
    typed = input("Type YES (all caps) to proceed: ")
    return typed == "YES"


def _cmd_run(args: argparse.Namespace) -> int:
    graph = plan_loader.build_solved_graph()
    journeys = plan_loader.list_planned_journeys(graph)
    journey = _find_journey(journeys, args.from_dex, args.to_dex)
    if journey is None:
        print(f"No journey {args.from_dex} -> {args.to_dex} in the current solved graph.", file=sys.stderr)
        print("Run `python -m compass_test.cli list-hops` to see what's available.", file=sys.stderr)
        return 1
    if not journey.inScope:
        print(f"{args.from_dex} -> {args.to_dex} is out of scope: {journey.outOfScopeReason}", file=sys.stderr)
        return 1

    amount = args.amount
    if amount is None:
        withdraw_hop = next((h for h in journey.hops if h.hopType == HopType.WITHDRAW), None)
        deposit_hop = next((h for h in journey.hops if h.hopType == HopType.DEPOSIT), None)
        min_withdraw = withdraw_hop.minWithdrawUsd if withdraw_hop else 0.0
        min_deposit = deposit_hop.minDepositUsd if deposit_hop else 0.0
        if min_withdraw <= 0 and min_deposit <= 0:
            print(
                f"No --amount given and neither {args.from_dex}'s minimum withdraw nor "
                f"{args.to_dex}'s minimum deposit is configured (0 or unset). Set them in the "
                f'Config tab ("Min withdraw" / "Min deposit") or pass --amount explicitly.',
                file=sys.stderr,
            )
            return 1
        # The smallest amount that clears BOTH ends of the route: below the
        # source's withdraw floor, the withdrawal itself would be rejected;
        # below the destination's deposit floor, the deposit wouldn't be
        # credited even if the withdrawal succeeded.
        amount = max(min_withdraw, min_deposit)
        print(
            f"No --amount given — using max(min withdraw {args.from_dex}=${min_withdraw:.2f}, "
            f"min deposit {args.to_dex}=${min_deposit:.2f}) = ${amount:.2f}"
        )

    wallet = OperatingWallet(known_address=config.OPERATING_WALLET_ADDRESS)

    if args.live:
        if not args.yes and not _confirm_live_run(journey, amount, wallet.address):
            print("Aborted.")
            return 1

    executed = executor.run_journey_hops(
        planned_hops=journey.hops,
        connector_for_dex=get_connector,
        amount_usd=amount,
        wallet=wallet,
        live=args.live,
    )

    hop_comparisons = [comparator.compare_hop(p, e) for p, e in zip(journey.hops, executed)]
    journey_comparison = comparator.compare_journey(journey, hop_comparisons)
    report = comparator.build_report(live=args.live, journeys=[journey_comparison], unsupported_dexes=[])
    path = reporter.save_report(report)

    print(f"\n{journey.fromDex} -> {journey.toDex} ({'LIVE' if args.live else 'dry-run'})")
    for hc in hop_comparisons:
        cost_err = f"{hc.costErrorPct:+.1f}%" if hc.costErrorPct is not None else "n/a"
        print(
            f"  {hc.planned.hopType.value:8s} {hc.planned.dex:16s} "
            f"est=${hc.planned.estimatedCostUsd:.4f}/{hc.planned.estimatedTimeSeconds:.0f}s  "
            f"actual={'n/a' if hc.executed.actualCostUsd is None else f'${hc.executed.actualCostUsd:.4f}'}"
            f"/{hc.executed.actualTimeSeconds:.0f}s  cost_err={cost_err}  status={hc.executed.status}"
        )
    print(f"\nReport written to {path}")
    return 0


def _confirm_live_hop(planned, amount_usd: float, wallet_address: str) -> bool:
    print("\n" + "=" * 70)
    print("LIVE RUN — real funds will move")
    print("=" * 70)
    print(f"Hop            : {planned.hopType.value} {planned.dex} on {planned.chain} ({planned.stable})")
    print(f"Test amount    : ${amount_usd:.2f} (caps: hop=${config.MAX_USD_PER_HOP:.2f}, "
          f"run=${config.MAX_USD_PER_RUN:.2f})")
    print(f"Operating wallet: {wallet_address}")
    print("=" * 70)
    typed = input("Type YES (all caps) to proceed: ")
    return typed == "YES"


def _cmd_run_hop(args: argparse.Namespace) -> int:
    hopType = HopType.WITHDRAW if args.hop == "withdraw" else HopType.DEPOSIT
    confirm = None if args.yes else _confirm_live_hop

    try:
        result = run_single_hop(
            args.dex, hopType, args.chain, args.stable,
            amount_usd=args.amount, live=args.live, confirm=confirm,
        )
    except HopValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except HopAborted:
        print("Aborted.")
        return 1
    except executor.SafetyCapError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if result.usedConfiguredMinimum:
        print(f"No --amount given — using configured minimum: ${result.resolvedAmountUsd:.2f}")

    hc = result.comparison
    cost_err = f"{hc.costErrorPct:+.1f}%" if hc.costErrorPct is not None else "n/a"
    print(f"\n{args.hop} {args.dex} ({'LIVE' if args.live else 'dry-run'})")
    print(
        f"  est=${hc.planned.estimatedCostUsd:.4f}/{hc.planned.estimatedTimeSeconds:.0f}s  "
        f"actual={'n/a' if hc.executed.actualCostUsd is None else f'${hc.executed.actualCostUsd:.4f}'}"
        f"/{hc.executed.actualTimeSeconds:.0f}s  cost_err={cost_err}  status={hc.executed.status}"
    )
    if hc.executed.notes:
        print(f"  notes: {hc.executed.notes}")
    print(f"\nReport written to {result.reportPath}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="compass_test")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-hops", help="List Withdraw/Deposit hops on the solver's chosen journeys").set_defaults(
        func=_cmd_list_hops
    )

    auth = sub.add_parser("check-auth", help="Smoke-test a DEX connector's auth with a read-only balance call")
    auth.add_argument("--dex", required=True)
    auth.add_argument("--stable", required=True, choices=[s.name for s in Stable])
    auth.set_defaults(func=_cmd_check_auth)

    run = sub.add_parser("run", help="Run (or dry-run) one solver-chosen journey")
    run.add_argument("--from", dest="from_dex", required=True)
    run.add_argument("--to", dest="to_dex", required=True)
    run.add_argument(
        "--amount",
        type=float,
        default=None,
        help="Test amount in USD per hop. Omit to use the source DEX's configured minimum "
        "withdraw amount (Config tab \"Min withdraw\")",
    )
    run.add_argument("--live", action="store_true", help="Actually move funds (see README.md Safety model)")
    run.add_argument("--yes", action="store_true", help="Skip the interactive live-run confirmation prompt")
    run.set_defaults(func=_cmd_run)

    runHop = sub.add_parser(
        "run-hop", help="Run (or dry-run) ONE DEX's Withdraw or Deposit directly, independent of any journey"
    )
    runHop.add_argument("--dex", required=True)
    runHop.add_argument("--hop", required=True, choices=["withdraw", "deposit"])
    runHop.add_argument("--chain", required=True, choices=[c.name for c in Chain])
    runHop.add_argument("--stable", required=True, choices=[s.name for s in Stable])
    runHop.add_argument(
        "--amount",
        type=float,
        default=None,
        help="Test amount in USD. Omit to use this DEX/chain's configured minimum "
        '("Min withdraw" / "Min deposit" in the Config tab)',
    )
    runHop.add_argument("--live", action="store_true", help="Actually move funds (see README.md Safety model)")
    runHop.add_argument("--yes", action="store_true", help="Skip the interactive live-run confirmation prompt")
    runHop.set_defaults(func=_cmd_run_hop)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
