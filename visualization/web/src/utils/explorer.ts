// Chain -> block explorer tx-URL prefix. Deliberately small: only chains
// the user actually wants a link for. A chain not in here (Ethereum, Base,
// Optimism, Polygon, Avalanche, Solana) just gets no link — not an error,
// see explorerTxUrl.
const EXPLORER_BASE_URLS: Record<string, string> = {
  ARBITRUM: "https://arbiscan.io/tx/",
  BSC: "https://bscscan.com/tx/",
};

export function explorerTxUrl(chain: string, txHash: string): string | null {
  const base = EXPLORER_BASE_URLS[chain];
  return base ? `${base}${txHash}` : null;
}
