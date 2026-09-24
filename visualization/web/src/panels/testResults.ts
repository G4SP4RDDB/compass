import { estTimeTitle } from "../utils/format";
import { fetchTestRuns, fetchTestRun } from "../api/client";
import type { ResultHop, ResultJourney, TestRun } from "../types/api";

// "Test Results" tab : lecture seule des rapports compass_test/reports/*.json
// via GET /api/test-runs (liste) / GET /api/test-runs/<runId> (détail), voir
// visualization/server.py. Aucun contrôle ici ne peut déclencher un run —
// voir compass_test/README.md "Safety model" : seul le CLI le peut, en
// particulier un run --live qui déplace de vrais fonds.
let testResultsLoaded = false;

function fmtUsd4(x: number | null | undefined): string {
  return x === null || x === undefined ? "n/a" : "$" + x.toFixed(4);
}

function fmtPct(x: number | null | undefined): string {
  if (x === null || x === undefined) return "n/a";
  return `${x > 0 ? "+" : ""}${x.toFixed(1)}%`;
}

function resultsErrClass(x: number | null | undefined): string {
  if (x === null || x === undefined) return "results-err-na";
  return x > 0 ? "results-err-pos" : x < 0 ? "results-err-neg" : "";
}

function hopRowHtml(hc: ResultHop): string {
  const p = hc.planned, e = hc.executed;
  const statusClass = e.status !== "ok" ? `results-status-${e.status}` : "";
  return `<tr>
    <td>${p.hopType}${p.toStable ? ` <span class="muted">${p.stable}→${p.toStable}</span>` : ""}</td>
    <td>${p.dex}</td>
    <td>${p.chain}</td>
    <td>${fmtUsd4(p.estimatedCostUsd)}</td>
    <td>${fmtUsd4(e.actualCostUsd)}</td>
    <td class="${resultsErrClass(hc.costErrorPct)}">${fmtPct(hc.costErrorPct)}</td>
    <td title="${estTimeTitle(p)}">${p.estimatedTimeSeconds.toFixed(0)}s${p.timeSource === "measured" ? " <span class=\"muted\">(m)</span>" : ""}</td>
    <td>${e.actualTimeSeconds.toFixed(0)}s</td>
    <td class="${resultsErrClass(hc.timeErrorPct)}">${fmtPct(hc.timeErrorPct)}</td>
    <td class="${statusClass}">${e.status}</td>
  </tr>`;
}

function journeyHtml(j: ResultJourney): string {
  return `<div class="results-journey">
    <div class="results-journey-head">
      <span>${j.fromDex} → ${j.toDex}</span>
      <span class="muted">
        ${j.stable} · total est ${fmtUsd4(j.totalEstimatedCostUsd)}/${j.totalEstimatedTimeSeconds.toFixed(0)}s
        · total actual ${fmtUsd4(j.totalActualCostUsd)}/${j.totalActualTimeSeconds.toFixed(0)}s
        · cost err ${fmtPct(j.costErrorPct)} · time err ${fmtPct(j.timeErrorPct)}
      </span>
    </div>
    <table class="results-table">
      <thead><tr>
        <th>Hop</th><th>DEX</th><th>Chain</th>
        <th>Est. cost</th><th>Actual cost</th><th>Cost err</th>
        <th>Est. time</th><th>Actual time</th><th>Time err</th><th>Status</th>
      </tr></thead>
      <tbody>${j.hops.map(hopRowHtml).join("")}</tbody>
    </table>
  </div>`;
}

function renderTestRun(run: TestRun): void {
  document.getElementById("resultsRunBadge")!.innerHTML =
    `<span class="results-badge ${run.live ? "results-badge-live" : "results-badge-dry"}">${run.live ? "LIVE" : "DRY RUN"}</span>`;

  document.getElementById("resultsUnsupported")!.textContent =
    run.unsupportedDexes && run.unsupportedDexes.length
      ? `Not yet instrumented: ${run.unsupportedDexes.join(", ")}`
      : "";

  const journeysEl = document.getElementById("resultsJourneys")!;
  journeysEl.innerHTML = run.journeys && run.journeys.length
    ? run.journeys.map(journeyHtml).join("")
    : `<p class="results-empty">This run recorded no journeys.</p>`;
}

async function loadSelectedRun(runId: string): Promise<void> {
  try {
    renderTestRun(await fetchTestRun(runId));
  } catch {
    document.getElementById("resultsJourneys")!.innerHTML = `<p class="results-empty">Could not load run ${runId}.</p>`;
  }
}

export async function initTestResults(): Promise<void> {
  if (testResultsLoaded) return;
  testResultsLoaded = true;
  const select = document.getElementById("resultsRunSelect") as HTMLSelectElement;
  let runs;
  try {
    runs = await fetchTestRuns();
  } catch {
    document.getElementById("resultsJourneys")!.innerHTML =
      "<p class=\"results-empty\">Not connected to the results server. Serve this page with `python -m visualization.server`.</p>";
    return;
  }
  if (!runs.length) {
    document.getElementById("resultsJourneys")!.innerHTML =
      "<p class=\"results-empty\">No test runs yet — see compass_test/README.md for how to run one (`python -m compass_test.cli run ...`).</p>";
    return;
  }
  select.innerHTML = runs.map(r =>
    `<option value="${r.runId}">${new Date(r.createdAt * 1000).toLocaleString()} — ${r.live ? "live" : "dry-run"}</option>`
  ).join("");
  select.addEventListener("change", () => loadSelectedRun(select.value));
  await loadSelectedRun(runs[0]!.runId);
}
