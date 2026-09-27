// Chain -> block explorer name + tx-URL prefix. Deliberately small: only
// chains the user actually wants a button for. A chain not in here
// (Ethereum, Base, Optimism, Polygon, Avalanche, Solana) just gets no
// button — not an error, see explorerLinkHtml.
const EXPLORERS: Record<string, { name: string; base: string }> = {
  ARBITRUM: { name: "Arbiscan", base: "https://arbiscan.io/tx/" },
  BSC: { name: "BscScan", base: "https://bscscan.com/tx/" },
};

// A "See on <Explorer>" button for a confirmed on-chain tx, or null (render
// nothing) for a chain outside EXPLORERS above.
export function explorerLinkHtml(chain: string, txHash: string): string | null {
  const info = EXPLORERS[chain];
  if (!info) return null;
  return `<a class="exec-explorer-btn" href="${info.base}${txHash}" target="_blank" rel="noopener noreferrer">See on ${info.name} ↗</a>`;
}
