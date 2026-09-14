"""Turns the live runs already on disk (compass_test/reports/*.json, see
reporter.py) into the measured delays the solver uses in place of the
hand-typed "withdraw in 5 min" claims — connectors/dex_measured_delays.json,
read back by connectors.dex_measured_delays.apply_measured_delays.

What counts as one sample: a hop with executed.live == True and
executed.status == "ok" — a real transfer whose credit was actually observed
(wallet balance increased for a withdraw, DEX balance increased for a
deposit). Its value is ExecutedHop.actualTimeSeconds = finishedAt -
startedAt, the full "we pressed go" -> "money is there" delta. "unconfirmed"
(timed out after POLL_TIMEOUT_SECONDS), "error" and "dry_run" hops are
excluded outright: they carry no completed delay to average.

Grouping key: (DEX, hop type, chain). Stable is deliberately NOT part of the
key — every DEX in the registry supports exactly one stable today. A Swap
hop's "DEX" is the venue (connectors.cowswap.COWSWAP_VENUE_NAME, the JSON's
SWAP_VENUE_KEY) and its direction (USDC->USDT vs the reverse) isn't part of
the key either: a CoW batch auction takes the same time either way.

Statistic: arithmetic mean of the MAX_SAMPLES (10) most recent samples,
ordered by when the hop finished — one sample already overrides the config,
two are averaged, and so on (see connectors.dex_measured_delays for the
rationale). A (DEX, chain, direction) with no successful live run gets NO
entry and keeps its configured/default delay.

Rebuilt from scratch on every call rather than incrementally appended, so
deleting a bad report from reports/ is enough to drop its sample.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from connectors.dex_measured_delays import (
    DEFAULT_MEASURED_DELAYS_PATH,
    DEPOSIT_FIELD,
    SWAP_FIELD,
    WITHDRAW_FIELD,
    MeasuredDelaysDict,
    measured_delay_from_samples,
    measured_delay_to_dict,
    save_measured_delays,
)

from .models import HopType, TestRunReport


@dataclass(frozen=True)
class DelaySample:
    dex: str
    hopType: HopType
    chain: str
    seconds: float
    runId: str
    measuredAt: float  # executed.finishedAt, unix seconds


def collect_delay_samples(reports: list[TestRunReport]) -> list[DelaySample]:
    """Every usable sample across `reports`, oldest first (by the hop's own
    finishedAt, not the report's createdAt: a journey report holds several
    hops that finished at different times)."""
    samples: list[DelaySample] = []
    for report in reports:
        for journey in report.journeys:
            for hc in journey.hops:
                executed = hc.executed
                if not executed.live or executed.status != "ok":
                    continue
                samples.append(
                    DelaySample(
                        dex=hc.planned.dex,
                        hopType=hc.planned.hopType,
                        chain=hc.planned.chain,
                        seconds=executed.actualTimeSeconds,
                        runId=report.runId,
                        measuredAt=executed.finishedAt,
                    )
                )
    samples.sort(key=lambda s: s.measuredAt)
    return samples


_FIELD_BY_HOP_TYPE = {HopType.WITHDRAW: WITHDRAW_FIELD, HopType.DEPOSIT: DEPOSIT_FIELD, HopType.SWAP: SWAP_FIELD}


def compute_measured_delays(reports: list[TestRunReport]) -> MeasuredDelaysDict:
    """{dexName: {chainName: {withdrawDelaySeconds: entry, depositDelaySeconds: entry}}}
    in the exact shape connectors/dex_measured_delays.json holds. A key is
    only present when at least one sample exists for it."""
    grouped: dict[tuple[str, str, HopType], list[DelaySample]] = {}
    for sample in collect_delay_samples(reports):
        grouped.setdefault((sample.dex, sample.chain, sample.hopType), []).append(sample)

    measured: MeasuredDelaysDict = {}
    for (dex, chain, hopType), group in grouped.items():
        entry = measured_delay_from_samples(
            samplesSeconds=[s.seconds for s in group],
            runIds=[s.runId for s in group],
            measuredAts=[s.measuredAt for s in group],
        )
        if entry is None:
            continue
        field = _FIELD_BY_HOP_TYPE[hopType]
        measured.setdefault(dex, {}).setdefault(chain, {})[field] = measured_delay_to_dict(entry)
    return measured


def rebuild_measured_delays(path: Path | str = DEFAULT_MEASURED_DELAYS_PATH) -> MeasuredDelaysDict:
    """Recompute from every report on disk and persist. Called by
    reporter.save_report after each live run and by the CLI's
    `calibrate-delays` command."""
    from . import reporter  # local: reporter imports this module lazily too

    measured = compute_measured_delays(reporter.list_reports())
    save_measured_delays(measured, path)
    return measured


def format_measured_delays_table(measured: MeasuredDelaysDict, configured: dict[str, dict[str, dict[str, float]]]) -> str:
    """Human-readable side-by-side of what the config says vs. what was
    measured, one line per (DEX, chain, direction) that has a measurement."""
    lines = [f"{'DEX':16s} {'chain':10s} {'hop':9s} {'configured':>11s} {'measured':>9s} {'n':>3s} {'std':>7s} {'min':>7s} {'max':>7s}"]
    for dex in sorted(measured):
        for chain in sorted(measured[dex]):
            for field, label in ((WITHDRAW_FIELD, "Withdraw"), (DEPOSIT_FIELD, "Deposit"), (SWAP_FIELD, "Swap")):
                entry = measured[dex][chain].get(field)
                if not entry:
                    continue
                configuredValue = configured.get(dex, {}).get(chain, {}).get(field)
                configuredText = f"{configuredValue:.0f}s" if configuredValue is not None else "default"
                lines.append(
                    f"{dex:16s} {chain:10s} {label:9s} {configuredText:>11s} {entry['meanSeconds']:>8.1f}s "
                    f"{entry['n']:>3d} {entry['stdSeconds']:>6.1f}s {entry['minSeconds']:>6.1f}s {entry['maxSeconds']:>6.1f}s"
                )
    if len(lines) == 1:
        lines.append("(no successful live run on disk yet — every delay stays on its configured/default value)")
    return "\n".join(lines)
