// Typed fetch wrappers for every /api/* endpoint (visualization/server.py).
// Two of these were found, by reading the original file closely, to be
// copy-pasted inline at two separate call sites rather than factored out:
// fetchDexBalance (loadDexRealBalance / addRecheckBalanceButton) and the
// getExecutionStatus response-cache pattern (mirrors getWalletBalances'
// cache in state/walletBalancesCache.ts, kept separate since they cache
// different endpoints).

import type {
  ApiErrorBody,
  DexBalancesResponse,
  DexConfigResponse,
  ExecutionStatus,
  ImbalanceEntry,
  ImbalancesResponse,
  MeasuredDelaysResponse,
  TestHopRequestBody,
  TestHopResult,
  TestRun,
  TestRunSummary,
  WalletBalanceRow,
  WalletDeficitEntry,
  WalletDeficitResponse,
} from "../types/api";

async function parseErrorBody(res: Response, fallback: string): Promise<string> {
  try {
    const data = (await res.json()) as ApiErrorBody;
    return data.error || fallback;
  } catch {
    return fallback;
  }
}

export async function fetchImbalances(): Promise<ImbalancesResponse> {
  const res = await fetch("/api/imbalances");
  if (!res.ok) throw new Error("bad response");
  return res.json();
}

export async function saveImbalances(next: Record<string, ImbalanceEntry | null>): Promise<ImbalancesResponse> {
  const res = await fetch("/api/imbalances", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(next),
  });
  const data: ImbalancesResponse = await res.json();
  if (!res.ok) throw new Error(data.error || `save failed (${res.status})`);
  return data;
}

export async function fetchWalletDeficit(): Promise<WalletDeficitResponse> {
  const res = await fetch("/api/wallet-deficits");
  if (!res.ok) throw new Error("bad response");
  return res.json();
}

export async function saveWalletDeficit(entry: WalletDeficitEntry | null): Promise<WalletDeficitResponse> {
  const res = await fetch("/api/wallet-deficits", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(entry),
  });
  const data: WalletDeficitResponse = await res.json();
  if (!res.ok) throw new Error(data.error || `save failed (${res.status})`);
  return data;
}

let executionStatusCache: ExecutionStatus | null = null;

export async function getExecutionStatus(): Promise<ExecutionStatus> {
  if (executionStatusCache) return executionStatusCache;
  let status: ExecutionStatus;
  try {
    const res = await fetch("/api/execution-status");
    status = res.ok ? await res.json() : { liveAllowed: false, walletAddress: null };
  } catch {
    status = { liveAllowed: false, walletAddress: null, offline: true };
  }
  executionStatusCache = status;
  return status;
}

export async function fetchWalletBalanceRows(): Promise<WalletBalanceRow[]> {
  const res = await fetch("/api/wallet-balances");
  if (!res.ok) throw new Error("bad response");
  return res.json();
}

// Duplicated inline in the original at two call sites (a DEX's Details
// panel and the "recheck balance" button after an unconfirmed live hop) —
// factored out here.
export async function fetchDexBalance(dexName: string): Promise<DexBalancesResponse> {
  try {
    const res = await fetch(`/api/dex-balances/${encodeURIComponent(dexName)}`);
    return await res.json();
  } catch {
    return { balances: null, error: "not connected to the balance server" };
  }
}

export async function postTestHop(body: TestHopRequestBody): Promise<TestHopResult> {
  const res = await fetch("/api/test-hop", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data: TestHopResult = await res.json();
  if (!res.ok || !data.ok) throw new Error(data.error || `test-hop failed (${res.status})`);
  return data;
}

// Live runs stream NDJSON progress (see execution/NdjsonStreamReader.ts) —
// this only issues the request and hands back the raw Response, since
// reading the stream is execution/'s concern, not api/'s.
export async function postTestHopLive(body: TestHopRequestBody): Promise<Response> {
  return fetch("/api/test-hop", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function fetchMeasuredDelays(): Promise<MeasuredDelaysResponse | null> {
  const res = await fetch("/api/measured-delays");
  if (!res.ok) return null;
  return res.json();
}

export async function fetchDexConfig(): Promise<DexConfigResponse> {
  const res = await fetch("/api/config");
  if (!res.ok) throw new Error("bad response");
  return res.json();
}

export async function saveDexConfig(config: DexConfigResponse): Promise<void> {
  const res = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
  if (!res.ok) throw new Error("save failed");
}

export async function postRecompute(): Promise<void> {
  const res = await fetch("/api/recompute", { method: "POST" });
  if (!res.ok) throw new Error(await parseErrorBody(res, `solve failed (${res.status})`));
}

export async function postSolveMode(mode: string): Promise<void> {
  const res = await fetch("/api/solve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
  if (!res.ok) throw new Error(await parseErrorBody(res, `solve failed (${res.status})`));
}

export async function fetchTestRuns(): Promise<TestRunSummary[]> {
  const res = await fetch("/api/test-runs");
  if (!res.ok) throw new Error("bad response");
  return res.json();
}

export async function fetchTestRun(runId: string): Promise<TestRun> {
  const res = await fetch(`/api/test-runs/${encodeURIComponent(runId)}`);
  if (!res.ok) throw new Error("bad response");
  return res.json();
}
