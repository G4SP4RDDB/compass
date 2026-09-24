import { fmt, fmtDuration } from "../utils/format";
import type { SwapNode } from "../types/graphData";

export function swapNodeTooltipHtml(swap: SwapNode): string {
  return `<div><b>${swap.name}</b></div>
          <div>on-chain stablecoin swap, same chain only</div>
          <div>chains: ${swap.chains.join(", ")}</div>`;
}

// LE node "swap" — pas de SourceNode/paths Dijkstra derrière (un swap n'est
// jamais qu'une edge WalletNode -> WalletNode, voir _swapNodeDict côté
// Python) : le détail affiché se limite à ce qu'on peut dire sans solveur —
// les chains supportées — plus les hops Swap du plan RÉELLEMENT choisi
// (GRAPH_DATA.operations, même source que le reste du panel "Chosen
// Operations").
export function renderSwapDetails(swap: SwapNode): void {
  const chainChips = swap.chains.map(c => `<span class="chip">${c}</span>`).join("");
  const swapOps = GRAPH_DATA.operations.filter(op => op.type === "Swap");
  const opsHtml = swapOps.length
    ? swapOps.map(op => `
        <div class="path-row">
          <div class="path-head expanded" style="cursor:default">
            <span style="flex:1">${op.from} → ${op.to}</span>
            <span class="cost">$${fmt(op.amount)} · ${fmt(op.cost)} · ${fmtDuration(op.time)}</span>
          </div>
        </div>`).join("")
    : `<div class="no-paths">No swap chosen by the solver in the current plan.</div>`;

  document.getElementById("details")!.innerHTML = `
    <h2>Details</h2>
    <dl>
      <dt>Venue</dt><dd>${swap.name}</dd>
      <dt>Scope</dt><dd>On-chain stablecoin swap, same chain only (no bridging)</dd>
      <dt>Supported chains</dt><dd><div class="chip-row">${chainChips}</div></dd>
    </dl>
    <h2>Chosen by the solver</h2>
    ${opsHtml}`;
}
