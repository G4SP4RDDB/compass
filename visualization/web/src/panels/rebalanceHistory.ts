import { fetchLegStages, fetchRebalances } from "../api/client";
import { createStageTracker } from "../execution/StageTracker";
import type { LegStage, RebalanceSummary } from "../types/api";

// "Rebalance History" tab: read-only replay of what's durably landed in
// Sentinel's TimescaleDB (sentinel.rebalance / rebalance_leg_stage) — the
// stage-by-stage trail a LIVE run's progress popup (execution/testHop.ts,
// StageTracker.ts) used to only stream to the browser and discard. Distinct
// from panels/testResults.ts (the JSON-report-backed "Test Results" tab,
// which stays the source of truth): this is durable, queryable history, and
// can go dark (DB unreachable) the same way /metrics already can.
let rebalanceHistoryLoaded = false;

function fmtUsd(x: number | null): string {
  return x === null || x === undefined ? "n/a" : "$" + x.toFixed(2);
}

function fmtTs(x: string | null): string {
  return x ? new Date(x).toLocaleString() : "—";
}

async function showLegStages(rebalanceId: string, legIndex: number, container: HTMLElement): Promise<void> {
  container.innerHTML = `<p class="results-empty">Loading…</p>`;
  let stages: LegStage[];
  try {
    stages = await fetchLegStages(rebalanceId, legIndex);
  } catch (err) {
    container.innerHTML = `<p class="results-empty">Could not load stages: ${err instanceof Error ? err.message : String(err)}</p>`;
    return;
  }
  if (!stages.length) {
    container.innerHTML =
      `<p class="results-empty">No stages recorded for leg ${legIndex} — dry runs rarely emit any ` +
      `(see executor.py's on_stage docstring), or this leg index doesn't exist on this rebalance.</p>`;
    return;
  }
  // Replay: feed every persisted stage through the exact tracker a LIVE
  // run's popup uses, in the order it happened, then freeze the last one as
  // done — a finished leg's history has no pending/spinning stage.
  container.innerHTML = "";
  const tracker = createStageTracker(container, undefined);
  for (const s of stages) tracker.push(s.message, s.domain || undefined);
  tracker.finish("ok");
}

function rebalanceRowHtml(r: RebalanceSummary): string {
  const statusClass = r.status === "failed" ? "results-status-error" : "";
  return `<div class="results-journey">
    <div class="results-journey-head">
      <span>${r.from_dex} → ${r.to_dex}</span>
      <span class="muted">
        ${r.stable} · ${fmtUsd(r.planned_amount_usd)} · ${r.live ? "live" : "dry-run"}
        · <span class="${statusClass}">${r.status}</span>
        · started ${fmtTs(r.started_at)}
      </span>
    </div>
    <div class="rebalance-leg-controls">
      <label>Leg <input type="number" min="0" value="0" class="leg-index-input" /></label>
      <button type="button" class="rebalance-view-stages-btn">View stages</button>
    </div>
    <div class="rebalance-stages"></div>
  </div>`;
}

function bindRowHandlers(rowEl: Element, r: RebalanceSummary): void {
  const btn = rowEl.querySelector(".rebalance-view-stages-btn") as HTMLButtonElement;
  const input = rowEl.querySelector(".leg-index-input") as HTMLInputElement;
  const stagesEl = rowEl.querySelector(".rebalance-stages") as HTMLElement;
  btn.addEventListener("click", () => showLegStages(r.rebalance_id, Number(input.value) || 0, stagesEl));
}

async function loadRebalances(): Promise<void> {
  const listEl = document.getElementById("rebalanceHistoryList")!;
  let rebalances: RebalanceSummary[];
  try {
    rebalances = await fetchRebalances();
  } catch (err) {
    listEl.innerHTML =
      `<p class="results-empty">Sentinel's TimescaleDB is unavailable: ` +
      `${err instanceof Error ? err.message : String(err)}. Same DB the /metrics dashboard reads.</p>`;
    return;
  }
  if (!rebalances.length) {
    listEl.innerHTML =
      `<p class="results-empty">No rebalances recorded yet — run one ` +
      `(<code>python -m compass_test.cli run ...</code> or "Test This Edge") to see it here.</p>`;
    return;
  }
  listEl.innerHTML = rebalances.map(rebalanceRowHtml).join("");
  rebalances.forEach((r, i) => bindRowHandlers(listEl.children[i]!, r));
}

export async function initRebalanceHistory(): Promise<void> {
  if (rebalanceHistoryLoaded) return;
  rebalanceHistoryLoaded = true;
  await loadRebalances();
}

// A live hop just landed a new row in sentinel.rebalance -- the cached list is now stale the
// same way the wallet-balances cache is (see App.ts's onLiveRunComplete wiring). Reloads
// unconditionally: the target <div> is in the DOM whether or not this tab is the visible one,
// so it's ready with fresh data the next time it's switched to.
export async function refreshRebalanceHistory(): Promise<void> {
  rebalanceHistoryLoaded = true;
  await loadRebalances();
}
