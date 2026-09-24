import { fmt } from "../utils/format";
import { getWalletBalances } from "../state/walletBalancesCache";

// Panel "Wallet" : solde ON-CHAIN RÉEL de l'operating wallet, jamais un mock
// — voir compass_test/balances.py::list_wallet_balances / GET
// /api/wallet-balances (server.py). USDC/USDT sur Arbitrum/BSC uniquement
// pour l'instant.
const WALLET_CHAIN_DOT: Record<string, string> = { ARBITRUM: "#28a0f0", BSC: "#f0b90b" };

export async function renderWalletBalancesSidebar(): Promise<void> {
  const el = document.getElementById("walletBalances")!;
  let rows;
  try {
    rows = await getWalletBalances();
  } catch {
    el.innerHTML = `<p class="wallet-error">Not connected to the wallet balance server. Serve this page with \`python -m visualization.server\`.</p>`;
    return;
  }

  const rowsHtml = rows.map(r => {
    const dot = WALLET_CHAIN_DOT[r.chain] || "var(--muted)";
    const value = r.balanceUsd === null
      ? `<span class="wallet-value wallet-na" title="${r.error || "unavailable"}">n/a</span>`
      : `<span class="wallet-value">$${fmt(r.balanceUsd)}</span>`;
    return `<div class="wallet-row">
      <span class="wallet-label"><span class="wallet-chain-dot" style="background:${dot}"></span>${r.chain} / ${r.stable}</span>
      ${value}
    </div>`;
  }).join("");

  const total = rows.reduce((sum, r) => sum + (r.balanceUsd || 0), 0);
  const anyMissing = rows.some(r => r.balanceUsd === null);

  document.getElementById("walletBalances")!.innerHTML = `
    ${rowsHtml}
    <div class="wallet-total"><span>Total</span><span>$${fmt(total)}${anyMissing ? "+" : ""}</span></div>`;
}
