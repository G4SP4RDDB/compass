import { fmt, fmtDuration } from "../utils/format";
import type { Journey } from "../types/graphData";
import type { PathEdge } from "../state/derivedGraphData";
import { hopHtml, routesSectionHtml } from "./routes";
import { bindTestEdgeButtons } from "../execution/testHop";
import { bindExecuteButtons } from "../execution/liveExecution";

export function edgeTooltipHtml(e: PathEdge, journeys: Journey[] | null): string {
  let html = `<div><b>${e.from}</b> → <b>${e.to}</b></div>
    <div>cheapest: ${fmt(e.cheapest.totalCost)} · ${fmtDuration(e.cheapest.totalTime)}</div>
    <div>fastest: ${fmt(e.fastest.totalCost)} · ${fmtDuration(e.fastest.totalTime)}</div>`;
  if (journeys) {
    const total = journeys.reduce((s, j) => s + j.amount, 0);
    const approx = journeys.some(j => j.plausible);
    html += `<div style="margin-top:4px;color:var(--chosen)"><b>Chosen by solver:</b> ${fmt(total)} moved${approx ? " (approximate)" : ""}</div>`;
  }
  return html;
}

export function renderEdgeDetails(e: PathEdge, journeys: Journey[] | null): void {
  const routesSection = routesSectionHtml(e.cheapest, e.fastest);

  let journeySection = "";
  if (journeys) {
    const totalAmount = journeys.reduce((s, j) => s + j.amount, 0);
    const anyApprox = journeys.some(j => j.plausible);
    const journeyBlocks = journeys.map(j => {
      const jHops = j.hops.map(h => hopHtml(h, true, j.amount)).join("") || `<div class="hop">direct — no intermediate hop</div>`;
      return `
        <div class="path-row">
          <div class="path-head expanded" style="cursor:default">
            <span style="flex:1">${fmt(j.amount)} moved${j.plausible ? `<span class="approx-tag">approximate</span>` : ""}</span>
            <span class="cost">${fmt(j.totalCost)} · ${fmtDuration(j.totalTime)}</span>
          </div>
          <div class="path-hops expanded">${jHops}</div>
        </div>`;
    }).join("");
    journeySection = `
      <h2>Chosen by the solver</h2>
      <dl><dt>Total moved</dt><dd>${fmt(totalAmount)}</dd></dl>
      ${anyApprox ? `<div class="approx-note">Part of this route crosses a point where the solver's flow merges with money from other DEXes and later splits again — the hop-by-hop split shown below is one valid explanation of the solver's total, not necessarily the exact one.</div>` : ""}
      ${journeyBlocks}`;
  }

  document.getElementById("details")!.innerHTML = `
    <h2>Details</h2>
    <dl>
      <dt>Route</dt><dd>${e.from} → ${e.to}</dd>
    </dl>
    <h2>Estimated routes</h2>
    ${routesSection}
    ${journeySection}`;

  bindTestEdgeButtons();
  bindExecuteButtons();
}
