import { fmt } from "../utils/format";
import { fetchImbalances, saveImbalances } from "../api/client";
import type { DexNode } from "../types/graphData";
import type { ImbalanceEntry, ImbalanceSummary } from "../types/api";

// Déséquilibres saisis à la main, un par DEX (voir connectors.dex_imbalances
// pour le format) — chargés depuis GET /api/imbalances, sauvegardés en bloc
// via POST /api/imbalances à chaque "Save" du formulaire ci-dessous. Le
// graphe/plan affiché ne change qu'au prochain "Run solver" (POST
// /api/recompute) : `summary` (totaux, faisabilité) est ce que ce prochain
// run verra, pas ce que le plan courant a utilisé.
let imbalances: Record<string, ImbalanceEntry | null> = {};
let imbalanceSummary: ImbalanceSummary | null = null;
let imbalanceServerAvailable = true;

function renderImbalanceSummary(): void {
  const el = document.getElementById("imbalanceSummary");
  if (!el) return;
  if (!imbalanceServerAvailable) { el.textContent = ""; return; }
  const sm = imbalanceSummary;
  if (!sm) { el.textContent = ""; return; }
  const parts = [`surplus <b>$${fmt(sm.totalSurplusUsd)}</b> (${sm.surplusDexes.length})`, `deficit <b>$${fmt(sm.totalDeficitUsd)}</b> (${sm.deficitDexes.length})`];
  el.className = "imb-summary" + (sm.problem ? " infeasible" : "");
  el.innerHTML = parts.join(" · ") + (sm.problem ? ` — ${sm.problem}` : "");
}

// Called once GET /api/imbalances resolves — App.ts wires this to
// re-render the currently-selected DEX's Details panel if one is open (see
// the plan's "Breaking the cycles": this module can't import
// panels/dexDetails.ts directly, since dexDetails.ts already imports this
// module for imbalanceFormHtml/bindImbalanceForm).
let imbalancesOnLoadedCb: (() => void) | null = null;
export function onImbalancesLoaded(cb: () => void): void {
  imbalancesOnLoadedCb = cb;
}

export async function initImbalances(): Promise<void> {
  try {
    const data = await fetchImbalances();
    imbalances = data.imbalances || {};
    imbalanceSummary = data.summary || null;
    if (data.startupProblem) {
      const st = document.getElementById("recomputeStatus")!;
      st.textContent = `Imbalances on disk were not solvable at startup (${data.startupProblem}) — the plan shown is empty. Fix them and press Run solver.`;
      st.className = "status-note offline";
    }
  } catch {
    imbalanceServerAvailable = false;
  }
  renderImbalanceSummary();
  imbalancesOnLoadedCb?.();
}

export function imbalanceFormHtml(dex: DexNode): string {
  if (!imbalanceServerAvailable) {
    return `<div class="imb-status error">Not connected to the server — imbalances can't be edited here. Serve this page with \`python -m visualization.server\`.</div>`;
  }
  const cur = imbalances[dex.name] || ({} as Partial<ImbalanceEntry>);
  const kind = cur.kind || "";
  const signedAmount = kind === "deficit" ? -(cur.amountUsd ?? 0) : kind === "surplus" ? cur.amountUsd : "";
  const hasStableChoice = dex.stables.length > 1;
  const stableOpts = dex.stables.map(s => `<option value="${s}" ${cur.stable === s ? "selected" : ""}>${s}</option>`).join("");
  const hasChainChoice = dex.chains.length > 1;
  const chainOpts = (hasChainChoice ? [`<option value="">any chain</option>`] : [])
    .concat(dex.chains.map(c => `<option value="${c}" ${cur.chain === c ? "selected" : ""}>${c}</option>`)).join("");
  const isSurplus = kind === "surplus";
  return `
    <div class="imb-form" data-dex="${dex.name}">
      <label>Amount (USD)</label>
      <input type="number" class="imb-amount" step="0.01" value="${signedAmount}" placeholder="0 = balanced, + = surplus, − = deficit">
      ${hasStableChoice ? `
      <label class="imb-surplus-only" ${isSurplus ? "" : "hidden"}>Stable</label>
      <select class="imb-stable imb-surplus-only" ${isSurplus ? "" : "hidden"}>${stableOpts}</select>` : ""}
      ${hasChainChoice ? `
      <label class="imb-surplus-only" ${isSurplus ? "" : "hidden"}>Chain${dex.requiresSameChainWithdraw ? " (required)" : ""}</label>
      <select class="imb-chain imb-surplus-only" ${isSurplus ? "" : "hidden"}>${chainOpts}</select>` : ""}
    </div>
    <div class="imb-actions">
      <button class="imb-save-btn" type="button">Save imbalance</button>
      <span class="imb-status"></span>
    </div>`;
}

export function bindImbalanceForm(dex: DexNode): void {
  const form = document.querySelector(`.imb-form[data-dex="${CSS.escape(dex.name)}"]`) as HTMLElement | null;
  if (!form) return;
  const amountEl = form.querySelector(".imb-amount") as HTMLInputElement;
  const saveBtn = form.parentElement!.querySelector(".imb-save-btn") as HTMLButtonElement;
  const statusEl = form.parentElement!.querySelector(".imb-status") as HTMLElement;

  const syncVisibility = () => {
    const isSurplus = parseFloat(amountEl.value) > 0;
    form.querySelectorAll(".imb-surplus-only").forEach(el => { (el as HTMLElement).hidden = !isSurplus; });
    const chainEl = form.querySelector(".imb-chain") as HTMLSelectElement | null;
    if (dex.requiresSameChainWithdraw && isSurplus && chainEl && !chainEl.value) {
      chainEl.value = dex.chains[0]!;
    }
  };
  amountEl.addEventListener("input", syncVisibility);
  syncVisibility();

  saveBtn.addEventListener("click", async () => {
    const raw = amountEl.value.trim();
    const value = raw === "" ? 0 : parseFloat(raw);
    if (Number.isNaN(value)) {
      statusEl.textContent = "enter a valid number";
      statusEl.className = "imb-status error";
      return;
    }
    const kind = value === 0 ? "" : value > 0 ? "surplus" : "deficit";
    const stableEl = form.querySelector(".imb-stable") as HTMLSelectElement | null;
    const chainEl = form.querySelector(".imb-chain") as HTMLSelectElement | null;
    const entry: ImbalanceEntry | null = kind === "" ? null : {
      kind: kind as "surplus" | "deficit",
      amountUsd: Math.abs(value),
      stable: stableEl ? stableEl.value : dex.stables[0]!,
      chain: chainEl ? (chainEl.value || null) : (dex.chains[0] ?? null),
    };
    const next = { ...imbalances, [dex.name]: entry };
    saveBtn.disabled = true;
    statusEl.textContent = "Saving…";
    statusEl.className = "imb-status";
    try {
      const data = await saveImbalances(next);
      imbalances = data.imbalances;
      imbalanceSummary = data.summary;
      renderImbalanceSummary();
      statusEl.textContent = entry ? `Saved — ${kind} $${entry.amountUsd.toFixed(2)}. Press Run solver to re-plan.` : "Saved — balanced. Press Run solver to re-plan.";
      statusEl.className = "imb-status ok";
    } catch (e) {
      statusEl.textContent = (e as Error).message;
      statusEl.className = "imb-status error";
    } finally {
      saveBtn.disabled = false;
    }
  });
}
