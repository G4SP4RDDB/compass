import { fmt, fmtSigned, fmtDuration, sign } from "../utils/format";
import { fetchDexBalance } from "../api/client";
import type { DexNode } from "../types/graphData";
import { routesSectionHtml } from "./routes";
import { imbalanceFormHtml, bindImbalanceForm } from "../forms/imbalanceForm";

export function nodeTooltipHtml(dex: DexNode): string {
  return `<div><b>${dex.name}</b></div>
          <div>balance: ${fmtSigned(dex.inbalance)}</div>
          <div>stables: ${dex.stables.join(", ") || "–"}</div>
          <div>chains: ${dex.chains.length}</div>`;
}

// `onNavigate`: clicking another DEX's name in "Paths to other DEXes"
// selects and focuses it (graph/GraphRenderer.ts's focusOn) — passed in by
// state/SelectionController.ts rather than imported directly, so this
// module never has to depend on the selection/graph-rendering layer (see
// the plan's "Breaking the cycles").
export function renderDexDetails(dex: DexNode, onNavigate: (targetName: string) => void): void {
  const targets = GRAPH_DATA.paths[dex.name] || {};
  const stableChips = dex.stables.map(s => {
    const bal = dex.withdrawBalances[s] || 0;
    // dex.withdrawChainByStable (voir DEX.requiresSameChainWithdraw côté
    // Python, ex: Aster) : ce solde n'est retirable QUE vers cette chain,
    // pas fongible entre chains comme c'est le cas par défaut ailleurs.
    const restrictedChain = dex.withdrawChainByStable[s];
    const chainNote = restrictedChain ? ` (${restrictedChain} only)` : "";
    return `<span class="chip">${s}: ${fmt(bal)}${chainNote}</span>`;
  }).join("") || `<span class="chip">none</span>`;
  const chainChips = dex.chains.map(c => `<span class="chip">${c}</span>`).join("");

  const rows = Object.entries(targets).sort((a, b) => a[1].cheapest.totalCost - b[1].cheapest.totalCost).map(([toName, path]) => {
    // Chevron header stays keyed on the cheapest route (sort order, summary
    // stat) ; expanding it reveals BOTH cheapest and fastest, merged into one
    // block when they're the same route — see routesSectionHtml.
    return `
      <div class="path-row">
        <div class="path-head" data-target="${toName}">
          <span class="chevron">▶</span>
          <span class="name" data-nav="${toName}">${toName}</span>
          <span class="cost">${fmt(path.cheapest.totalCost)} · ${fmtDuration(path.cheapest.totalTime)}</span>
        </div>
        <div class="path-hops">${routesSectionHtml(path.cheapest, path.fastest)}</div>
      </div>`;
  }).join("") || `<div class="no-paths">No route found to any other DEX (no withdrawable stable configured).</div>`;

  document.getElementById("details")!.innerHTML = `
    <h2>Details</h2>
    <dl>
      <dt>DEX</dt><dd>${dex.name}</dd>
      <dt>Real balance (live)</dt><dd id="dexRealBalance"><div class="chip-row"><span class="chip dex-balance-na">Loading…</span></div></dd>
      <dt>Solved with</dt><dd class="${sign(dex)}" style="color:var(--${sign(dex)})">${fmtSigned(dex.inbalance)}${Object.keys(dex.withdrawBalances).length ? ` <span class="muted">(${stableChips.replace(/<[^>]+>/g, "")})</span>` : ""}</dd>
      <dt>Supported chains</dt><dd><div class="chip-row">${chainChips}</div></dd>
    </dl>
    <h2>Imbalance</h2>
    ${imbalanceFormHtml(dex)}
    <h2>Paths to other DEXes</h2>
    ${rows}`;

  loadDexRealBalance(dex.name, dex.stables);
  bindImbalanceForm(dex);

  document.querySelectorAll(".path-head").forEach(head => {
    head.addEventListener("click", (ev) => {
      if ((ev.target as HTMLElement).dataset.nav) return;
      head.classList.toggle("expanded");
      head.nextElementSibling!.classList.toggle("expanded");
    });
  });
  document.querySelectorAll<HTMLElement>("[data-nav]").forEach(el => {
    el.addEventListener("click", (ev) => {
      ev.stopPropagation();
      onNavigate(el.dataset.nav!);
    });
  });
}

let selectedDexName: string | null = null;
export function setSelectedDexNameForBalanceGuard(name: string | null): void {
  selectedDexName = name;
}

// Fetched on demand (not baked into GRAPH_DATA at page load, unlike the
// mock target) — GET /api/dex-balances/<name> (server.py), a live call to
// that DEX's own API every time, see compass_test/balances.py. Guarded
// against a stale response: if the user clicks a different DEX before this
// resolves, the currently-selected name has already moved on and the old
// response is dropped rather than overwriting the now-wrong panel.
// `data.balances` is `{USDT, USDC}` — most DEXes only ever hold one of the
// two (see each reader's docstring in balances.py), the other comes back as 0.
async function loadDexRealBalance(dexName: string, stables: string[]): Promise<void> {
  const data = await fetchDexBalance(dexName);
  if (selectedDexName !== dexName) return;
  const el = document.getElementById("dexRealBalance");
  if (!el) return;
  if (data.balances === null || data.balances === undefined) {
    el.innerHTML = `<div class="chip-row"><span class="chip dex-balance-na" title="${data.error || "unavailable"}">n/a</span></div>`;
  } else {
    const chips = (stables && stables.length ? stables : ["USDT", "USDC"]).map(s =>
      `<span class="chip dex-balance-real">${s}: $${fmt(data.balances![s] || 0)}</span>`
    ).join("");
    el.innerHTML = `<div class="chip-row">${chips}</div>`;
  }
}
