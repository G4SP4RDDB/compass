"""Data model shared by executor.py, comparator.py, reporter.py and the
frontend endpoints (visualization/server.py). Deliberately plain dataclasses
with hand-written to_dict/from_dict (not a JSON library dependency) — the
schema is small and is the exact contract the frontend's Test Results tab
reads."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class HopType(str, Enum):
    WITHDRAW = "Withdraw"
    DEPOSIT = "Deposit"
    # Same-chain stable -> stable conversion (WalletNode -> WalletNode,
    # EdgeType.Swap), executed through CoW Swap — see runners/cowswap.py.
    # `dex` on such a hop is the venue name (connectors.cowswap.
    # COWSWAP_VENUE_NAME), `stable` the sold stable, `toStable` the bought one.
    SWAP = "Swap"


@dataclass
class PlannedHop:
    """One Withdraw or Deposit edge from the solved graph (src/graph), i.e.
    Edge.cost/Edge.time as computed by costing.computeCost/computeDelay for
    that exact edge — the estimate we're checking."""

    hopType: HopType
    dex: str
    chain: str  # graph.structures.DEXes.Chain member name, e.g. "BSC"
    stable: str  # graph.structures.DEXes.Stable member name, e.g. "USDT"
    estimatedCostUsd: float
    estimatedTimeSeconds: float
    # SWAP hops only: the stable BOUGHT (`stable` above is the one sold).
    # "" for Withdraw/Deposit — one stable in, same stable out.
    toStable: str = ""
    # What the hand-typed config (Config tab / DEFAULT_*) says for this
    # edge, regardless of any measurement — costing.computeConfiguredDelay.
    # estimatedTimeSeconds above is what the solver actually uses: equal to
    # this when timeSource == "configured", or the mean of the last live
    # runs when timeSource == "measured" (see costing.computeDelay and
    # connectors/dex_measured_delays.py). Kept side by side so the Test
    # Results tab can still show how wrong the DEX frontend's claim was
    # even after a measurement has replaced it. Default None only for
    # reports written before this field existed.
    configuredTimeSeconds: float | None = None
    timeSource: str = "configured"  # "configured" | "measured"
    # Solver-chosen amount on this edge (edge.flow), kept only for context —
    # the actually-tested amount is amountRequestedUsd on ExecutedHop below,
    # deliberately a small capped test amount, never the solver's amount.
    # Imbalances are hand-set per DEX (connectors/dex_imbalances.json, see
    # connectors.dex_imbalances) — solvedFlowUsd is bounded by what the user
    # typed, not by a real balance, so it can still exceed what an account
    # holds. The "Execute" button in the graph UI runs a hop at exactly
    # this amount (POST /api/test-hop live), subject to executor's $ caps.
    solvedFlowUsd: float = 0.0
    # WITHDRAW hops only: this DEX/chain's configured minimum withdrawal
    # amount (DEX.minWithdrawUsdByChain, edited in the Config tab — "Min
    # withdraw"). 0.0 for a Deposit hop.
    minWithdrawUsd: float = 0.0
    # DEPOSIT hops only, mirror of the above (DEX.minDepositUsdByChain,
    # "Min deposit"). 0.0 for a Withdraw hop. cli.py defaults a journey's
    # test amount to max(destination's minDepositUsd, source's
    # minWithdrawUsd) when --amount is omitted — the smallest amount that
    # clears BOTH ends of the route, not just the source.
    minDepositUsd: float = 0.0

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["hopType"] = self.hopType.value
        return d

    @staticmethod
    def from_dict(d: dict) -> "PlannedHop":
        return PlannedHop(
            hopType=HopType(d["hopType"]),
            dex=d["dex"],
            chain=d["chain"],
            stable=d["stable"],
            estimatedCostUsd=d["estimatedCostUsd"],
            estimatedTimeSeconds=d["estimatedTimeSeconds"],
            toStable=d.get("toStable", ""),
            configuredTimeSeconds=d.get("configuredTimeSeconds"),
            timeSource=d.get("timeSource", "configured"),
            solvedFlowUsd=d.get("solvedFlowUsd", 0.0),
            minWithdrawUsd=d.get("minWithdrawUsd", 0.0),
            minDepositUsd=d.get("minDepositUsd", 0.0),
        )


@dataclass
class ExecutedHop:
    """What actually happened when we ran (or dry-ran) a PlannedHop."""

    live: bool
    startedAt: float  # unix seconds
    finishedAt: float
    amountRequestedUsd: float
    actualCostUsd: float | None  # None only if a live measurement failed/timed out
    amountReceivedUsd: float | None = None
    txHash: str | None = None
    # Deposit hops only: when the on-chain transfer's receipt came back, so
    # actualTimeSeconds (= finishedAt - startedAt, the full delta the
    # calibration averages) can later be split into chain confirmation
    # (txConfirmedAt - startedAt) vs. DEX crediting (finishedAt -
    # txConfirmedAt). None for withdraws and for older reports.
    txConfirmedAt: float | None = None
    externalId: str | None = None  # exchange-side withdraw id / CoW order UID, when applicable
    status: str = "ok"  # "ok" | "unconfirmed" | "error" | "dry_run"
    notes: str = ""

    @property
    def actualTimeSeconds(self) -> float:
        return self.finishedAt - self.startedAt

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["actualTimeSeconds"] = self.actualTimeSeconds
        return d

    @staticmethod
    def from_dict(d: dict) -> "ExecutedHop":
        return ExecutedHop(
            live=d["live"],
            startedAt=d["startedAt"],
            finishedAt=d["finishedAt"],
            amountRequestedUsd=d["amountRequestedUsd"],
            actualCostUsd=d.get("actualCostUsd"),
            amountReceivedUsd=d.get("amountReceivedUsd"),
            txHash=d.get("txHash"),
            txConfirmedAt=d.get("txConfirmedAt"),
            externalId=d.get("externalId"),
            status=d.get("status", "ok"),
            notes=d.get("notes", ""),
        )


@dataclass
class HopComparison:
    planned: PlannedHop
    executed: ExecutedHop

    @property
    def costErrorUsd(self) -> float | None:
        if self.executed.actualCostUsd is None:
            return None
        return self.executed.actualCostUsd - self.planned.estimatedCostUsd

    @property
    def costErrorPct(self) -> float | None:
        err = self.costErrorUsd
        if err is None or self.planned.estimatedCostUsd <= 1e-9:
            return None
        return err / self.planned.estimatedCostUsd * 100.0

    @property
    def timeErrorSeconds(self) -> float:
        return self.executed.actualTimeSeconds - self.planned.estimatedTimeSeconds

    @property
    def timeErrorPct(self) -> float | None:
        if self.planned.estimatedTimeSeconds <= 1e-9:
            return None
        return self.timeErrorSeconds / self.planned.estimatedTimeSeconds * 100.0

    def to_dict(self) -> dict:
        return {
            "planned": self.planned.to_dict(),
            "executed": self.executed.to_dict(),
            "costErrorUsd": self.costErrorUsd,
            "costErrorPct": self.costErrorPct,
            "timeErrorSeconds": self.timeErrorSeconds,
            "timeErrorPct": self.timeErrorPct,
        }

    @staticmethod
    def from_dict(d: dict) -> "HopComparison":
        return HopComparison(planned=PlannedHop.from_dict(d["planned"]), executed=ExecutedHop.from_dict(d["executed"]))


@dataclass
class JourneyComparison:
    """Roll-up of a DEX->DEX journey (mirrors visualization/journeys.Journey)
    — the unit the user actually reasons about: 'Aster -> MEXC', not
    individual edges."""

    fromDex: str
    toDex: str
    stable: str
    hops: list[HopComparison] = field(default_factory=list)

    @property
    def totalEstimatedCostUsd(self) -> float:
        return sum(h.planned.estimatedCostUsd for h in self.hops)

    @property
    def totalActualCostUsd(self) -> float | None:
        values = [h.executed.actualCostUsd for h in self.hops]
        return None if any(v is None for v in values) else sum(values)

    @property
    def totalEstimatedTimeSeconds(self) -> float:
        return sum(h.planned.estimatedTimeSeconds for h in self.hops)

    @property
    def totalActualTimeSeconds(self) -> float:
        return sum(h.executed.actualTimeSeconds for h in self.hops)

    @property
    def costErrorPct(self) -> float | None:
        actual = self.totalActualCostUsd
        if actual is None or self.totalEstimatedCostUsd <= 1e-9:
            return None
        return (actual - self.totalEstimatedCostUsd) / self.totalEstimatedCostUsd * 100.0

    @property
    def timeErrorPct(self) -> float | None:
        if self.totalEstimatedTimeSeconds <= 1e-9:
            return None
        return (self.totalActualTimeSeconds - self.totalEstimatedTimeSeconds) / self.totalEstimatedTimeSeconds * 100.0

    def to_dict(self) -> dict:
        return {
            "fromDex": self.fromDex,
            "toDex": self.toDex,
            "stable": self.stable,
            "hops": [h.to_dict() for h in self.hops],
            "totalEstimatedCostUsd": self.totalEstimatedCostUsd,
            "totalActualCostUsd": self.totalActualCostUsd,
            "totalEstimatedTimeSeconds": self.totalEstimatedTimeSeconds,
            "totalActualTimeSeconds": self.totalActualTimeSeconds,
            "costErrorPct": self.costErrorPct,
            "timeErrorPct": self.timeErrorPct,
        }

    @staticmethod
    def from_dict(d: dict) -> "JourneyComparison":
        return JourneyComparison(
            fromDex=d["fromDex"],
            toDex=d["toDex"],
            stable=d["stable"],
            hops=[HopComparison.from_dict(h) for h in d.get("hops", [])],
        )


@dataclass
class TestRunReport:
    runId: str
    createdAt: float
    live: bool
    journeys: list[JourneyComparison] = field(default_factory=list)
    # DEX names present in the solved graph's chosen operations but with no
    # connector yet (see runners/registry.py) — surfaced explicitly rather
    # than silently missing from the report.
    unsupportedDexes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "runId": self.runId,
            "createdAt": self.createdAt,
            "live": self.live,
            "journeys": [j.to_dict() for j in self.journeys],
            "unsupportedDexes": self.unsupportedDexes,
        }

    @staticmethod
    def from_dict(d: dict) -> "TestRunReport":
        return TestRunReport(
            runId=d["runId"],
            createdAt=d["createdAt"],
            live=d["live"],
            journeys=[JourneyComparison.from_dict(j) for j in d.get("journeys", [])],
            unsupportedDexes=d.get("unsupportedDexes", []),
        )


def new_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
