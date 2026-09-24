import { fmt, fmtDuration } from "../utils/format";
import { getWalletBalances } from "../state/walletBalancesCache";
import { hopHtml } from "./routes";
import { walletDeficitFormHtml, bindWalletDeficitForm } from "../forms/walletDeficitForm";
import { bindTestEdgeButtons } from "../execution/testHop";
import { bindExecuteButtons } from "../execution/liveExecution";

// Wallet nodes — clicking one shows that chain's REAL on-chain USDC/USDT
// balance (GET /api/wallet-balances, same live data as the sidebar "Wallet
// (on-chain, live)" panel, see panels/walletBalancesSidebar.ts). Wallet
// balances cache (see state/walletBalancesCache.ts) — this panel and the
// sidebar panel both read from it instead of each hitting the endpoint on
// their own.
export async function renderWalletDetails(chain: string): Promise<void> {
  document.getElementById("details")!.innerHTML = `
    <h2>Details</h2>
    <div class="placeholder">Loading real on-chain balance…</div>`;

  let rows;
  try {
    rows = (await getWalletBalances()).filter(r => r.chain === chain);
  } catch {
    document.getElementById("details")!.innerHTML = `
      <h2>Details</h2>
      <dl><dt>Wallet</dt><dd>${chain}</dd></dl>
      <p class="results-empty">Not connected to the balance server. Serve this page with \`python -m visualization.server\`.</p>`;
    return;
  }

  const rowsHtml = rows.length ? rows.map(r => {
    const value = r.balanceUsd === null
      ? `<span class="wallet-value wallet-na" title="${r.error || "unavailable"}">n/a</span>`
      : `<span class="wallet-value">$${fmt(r.balanceUsd)}</span>`;
    return `<div class="wallet-row"><span class="wallet-label">${r.stable}</span>${value}</div>`;
  }).join("") : `<p class="results-empty">No live balance reader configured for ${chain} yet — set the withdraw amount below instead.</p>`;

  const walletJourneys = GRAPH_DATA.journeys.filter(j => j.fromWallet && j.from === `Wallet ${chain}/${j.stable}`);
  const journeyBlocks = walletJourneys.map(j => {
    const jHops = j.hops.map(h => hopHtml(h, true, j.amount)).join("");
    return `
      <div class="path-row">
        <div class="path-head expanded" style="cursor:default">
          <span style="flex:1">${fmt(j.amount)} ${j.stable} → ${j.to}${j.plausible ? `<span class="approx-tag">approximate</span>` : ""}</span>
          <span class="cost">${fmt(j.totalCost)} · ${fmtDuration(j.totalTime)}</span>
        </div>
        <div class="path-hops expanded">${jHops}</div>
      </div>`;
  }).join("");

  document.getElementById("details")!.innerHTML = `
    <h2>Details</h2>
    <dl>
      <dt>Wallet</dt><dd>${chain}</dd>
      <dt>Scope</dt><dd>${rows.length ? "USDC, USDT — real on-chain balance, live" : "no live balance reader for this chain yet"}</dd>
    </dl>
    <h2>Balance</h2>
    ${rowsHtml}
    <h2>Withdraw amount</h2>
    ${walletDeficitFormHtml()}
    ${walletJourneys.length ? `<h2>Chosen by the solver from this wallet</h2>${journeyBlocks}` : ""}`;

  bindWalletDeficitForm();
  bindTestEdgeButtons();
  bindExecuteButtons();
}
