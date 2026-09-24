import { fmt, fmtDuration, typeSlug, typeTag } from "../utils/format";
import { opsSummaryData } from "./stats";
import { executeButtonHtml, bindExecuteButtons } from "../execution/liveExecution";

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

  opsList.innerHTML = ops.map(o => `
    <div class="op-row type-${typeSlug(o.type)}">
      <span class="op-type">${typeTag(o.type)}</span>
      <span class="op-route"><b>${o.from}</b> → ${o.to}${o.protocol ? ` <span class="hop-protocol">via ${o.protocol}</span>` : ""}</span>
      <span class="op-amount">${fmt(o.amount)}</span>
      <span class="op-cost">${fmt(o.cost)}</span>
      <span class="op-delay">${o.time ? o.time.toFixed(0) + "s" : "–"}</span>
      <span class="op-exec">${o.testable ? executeButtonHtml(o.testable, o.amount, o.from, o.to) : ""}</span>
      ${o.testable ? `<div class="op-exec-result"><div class="hop-test-result"></div></div>` : ""}
    </div>`).join("");
  bindExecuteButtons();
}
