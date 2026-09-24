import { fetchWalletBalanceRows } from "../api/client";
import type { WalletBalanceRow } from "../types/api";

// Cached in memory: this balance only actually moves when a live hop
// executes (see execution/liveHopRequest.ts's callers, the only place that
// forces a refresh) — every other read (page load, re-opening a wallet's
// Details panel) reuses the same cached rows instead of re-hitting GET
// /api/wallet-balances. `inFlight` dedupes concurrent callers into a single
// request.
let cache: WalletBalanceRow[] | null = null;
let inFlight: Promise<WalletBalanceRow[]> | null = null;

export async function getWalletBalances(forceRefresh = false): Promise<WalletBalanceRow[]> {
  if (forceRefresh) cache = null;
  if (cache) return cache;
  if (!inFlight) {
    inFlight = fetchWalletBalanceRows()
      .then(rows => { cache = rows; return rows; })
      .finally(() => { inFlight = null; });
  }
  return inFlight;
}
