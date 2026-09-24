import { fetchWalletDeficit, saveWalletDeficit } from "../api/client";
import type { WalletDeficitEntry } from "../types/api";

// Argent dû à un retrait utilisateur, par stable (connectors.wallet_deficits,
// GET/POST /api/wallet-deficits, voir graph.node.WalletDeficitNode) -- saisi
// à la main EXACTEMENT comme l'Imbalance d'un DEX (forms/imbalanceForm.ts),
// mais EN USD et PARTAGÉ entre TOUTES les chains ET TOUTES les stables (pas
// de "surplus" possible ici, ni de chain/stable à choisir -- régler un
// retrait en USDC ou en USDT ne fait aucune différence) : ce bloc s'affiche
// donc IDENTIQUEMENT dans le panel Details de chaque chain de wallet, et
// l'éditer depuis n'importe laquelle change la même donnée sur disque. UN
// SEUL montant pour tout le wallet, pas un par stable.
let walletDeficit: Partial<WalletDeficitEntry> = {};
let walletDeficitsServerAvailable = true;

// Called once GET /api/wallet-deficits resolves — App.ts wires this to
// re-render the currently-selected wallet's Details panel if one is open
// (same cycle-breaking pattern as forms/imbalanceForm.ts's onImbalancesLoaded).
let walletDeficitsOnLoadedCb: (() => void) | null = null;
export function onWalletDeficitsLoaded(cb: () => void): void {
  walletDeficitsOnLoadedCb = cb;
}

export async function initWalletDeficits(): Promise<void> {
  try {
    const data = await fetchWalletDeficit();
    walletDeficit = data.walletDeficit || {};
  } catch {
    walletDeficitsServerAvailable = false;
  }
  walletDeficitsOnLoadedCb?.();
}

export function walletDeficitFormHtml(): string {
  if (!walletDeficitsServerAvailable) {
    return `<div class="imb-status error">Not connected to the server — payouts can't be edited here. Serve this page with \`python -m visualization.server\`.</div>`;
  }
  const amount = walletDeficit.amountUsd !== undefined ? walletDeficit.amountUsd : "";
  return `
    <div class="wallet-deficit-form-block">
      <div class="imb-form">
        <label>Amount (USD)</label>
        <input type="number" class="imb-amount" min="0" step="0.01" value="${amount}">
      </div>
      <div class="imb-actions">
        <button class="imb-save-btn" type="button">Save</button>
        <span class="imb-status"></span>
      </div>
    </div>`;
}

export function bindWalletDeficitForm(): void {
  const block = document.querySelector(".wallet-deficit-form-block");
  if (!block) return;
  const amountEl = block.querySelector(".imb-amount") as HTMLInputElement | null;
  const saveBtn = block.querySelector(".imb-save-btn") as HTMLButtonElement | null;
  const statusEl = block.querySelector(".imb-status") as HTMLElement | null;
  if (!amountEl || !saveBtn || !statusEl) return;

  saveBtn.addEventListener("click", async () => {
    const amountUsd = parseFloat(amountEl.value);
    // Any amount > 0 is a deficit; 0, blank, or invalid clears it.
    const entry: WalletDeficitEntry | null = amountUsd > 0 ? { kind: "deficit", amountUsd } : null;
    saveBtn.disabled = true;
    statusEl.textContent = "Saving…";
    statusEl.className = "imb-status";
    try {
      const data = await saveWalletDeficit(entry);
      walletDeficit = data.walletDeficit;
      statusEl.textContent = entry ? `Saved — $${entry.amountUsd.toFixed(2)}. Press Run solver to re-plan.` : "Saved — 0. Press Run solver to re-plan.";
      statusEl.className = "imb-status ok";
    } catch (e) {
      statusEl.textContent = (e as Error).message;
      statusEl.className = "imb-status error";
    } finally {
      saveBtn.disabled = false;
    }
  });
}
