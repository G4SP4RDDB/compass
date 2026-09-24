import { fmt, fmtDuration, typeSlug, typeTag } from "../utils/format";
import { opsSummaryData } from "./stats";
import { executeButtonHtml, bindExecuteButtons } from "../execution/liveExecution";
import { hopStablesLabel } from "../execution/testHop";

// Column header row — same op-type/op-route/op-stable/op-amount/op-cost/
// op-delay span classes as the data rows below it, so the flex column
// widths line up automatically. Only shown when there's a list to label
// (skipped in the empty-state branch below) — someone unfamiliar with the
// interface otherwise has to infer what each bare number in a row means
// from column position alone.
const OPS_HEADER_HTML = `
  <div class="op-row op-row-header">
    <span class="op-type">Type</span>
    <span class="op-route">Route</span>
    <span class="op-stable">Stablecoin</span>
    <span class="op-amount">Amount</span>
    <span class="op-cost">Cost</span>
    <span class="op-delay">Est. Time</span>
    <span class="op-exec"></span>
  </div>`;

export function renderOperations(): void {
  const ops = GRAPH_DATA.operations;
  const opsSummary = document.getElementById("opsSummary")!;
  const opsList = document.getElementById("opsList")!;

  if (!opsSummaryData) {
    opsSummary.innerHTML = "";
    opsList.innerHTML = `<div class="ops-empty">No rebalancing operations — no DEX has a deficit to fill. Click a DEX, set a surplus or a deficit, then press "Run solver".</div>`;
    return;
  }

  opsSummary.innerHTML = `
    <div>Operations<b>${opsSummaryData.count}</b></div>
    <div>Total moved<b>$${fmt(opsSummaryData.totalAmount)}</b></div>
    <div>Total fees<b>$${fmt(opsSummaryData.totalCost)}</b></div>
    <div title="Longest single hop among the chosen operations, not the sum along a chain">Est. total time<b>${fmtDuration(opsSummaryData.totalTime)}</b></div>`;

  const rows = ops.map(o => `
    <div class="op-row type-${typeSlug(o.type)}">
      <span class="op-type">${typeTag(o.type)}</span>
      <span class="op-route"><b>${o.from}</b> → ${o.to}${o.protocol ? ` <span class="hop-protocol">via ${o.protocol}</span>` : ""}</span>
      <span class="op-stable">${o.testable ? hopStablesLabel(o.testable) : "–"}</span>
      <span class="op-amount">${fmt(o.amount)}</span>
      <span class="op-cost">${fmt(o.cost)}</span>
      <span class="op-delay">${o.time ? o.time.toFixed(0) + "s" : "–"}</span>
      <span class="op-exec">${o.testable ? executeButtonHtml(o.testable, o.amount, o.from, o.to) : ""}</span>
      ${o.testable ? `<div class="op-exec-result"><div class="hop-test-result"></div></div>` : ""}
    </div>`).join("");
  opsList.innerHTML = OPS_HEADER_HTML + rows;
  bindExecuteButtons();
}
