// Request/response shapes for the /api/* endpoints exposed by
// visualization/server.py. Field names mirror exactly what the original JS
// read off each response (no server-side TypedDict/schema to check against
// — see the same caveat in types/graphData.ts).

import type { DexOperationalParams, MeasuredDelay, TestableHopInfo } from "./graphData";

export interface ExecutionStatus {
  liveAllowed: boolean;
  walletAddress: string | null;
  uiMode?: "test" | "prod";
  maxUsdPerHop?: number;
  maxUsdPerRun?: number;
  offline?: boolean;
}

export interface PlannedHop {
  dex: string;
  chain: string;
  stable: string;
  toStable?: string;
  // Bridge only: the destination chain (`chain` is the source chain).
  toChain?: string;
  hopType: string;
  estimatedCostUsd: number;
  estimatedTimeSeconds: number;
  timeSource: "measured" | "configured";
  configuredTimeSeconds: number | null;
}

export interface ExecutedHop {
  status: "ok" | "unconfirmed" | "error";
  actualCostUsd: number | null;
  actualTimeSeconds: number;
  notes?: string;
}

export interface TestHopResult {
  ok: boolean;
  error?: string;
  planned: PlannedHop;
  executed: ExecutedHop;
  resolvedAmountUsd: number;
  usedConfiguredMinimum: boolean;
  costErrorPct: number | null;
  live: boolean;
  liveAllowed?: boolean;
  walletAddress?: string;
}

export interface TestHopStageEvent {
  type: "stage";
  message: string;
  domain?: "onchain" | "exchange";
}

export type TestHopStreamEvent = TestHopStageEvent | TestHopResult;

export interface TestHopRequestBody {
  dex: string;
  chain: string;
  stable: string;
  toStable?: string;
  toChain?: string;
  hopType: string;
  live: boolean;
  confirm?: "YES";
  amount?: number;
}

export interface ImbalanceEntry {
  kind: "surplus" | "deficit";
  amountUsd: number;
  stable: string;
  chain: string | null;
}

export interface ImbalanceSummary {
  totalSurplusUsd: number;
  surplusDexes: string[];
  totalDeficitUsd: number;
  deficitDexes: string[];
  problem?: string;
}

export interface ImbalancesResponse {
  imbalances: Record<string, ImbalanceEntry | null>;
  summary: ImbalanceSummary | null;
  startupProblem?: string;
  error?: string;
}

export interface WalletDeficitEntry {
  kind: "deficit";
  amountUsd: number;
}

export interface WalletDeficitResponse {
  walletDeficit: WalletDeficitEntry | Record<string, never>;
  error?: string;
}

export interface WalletBalanceRow {
  chain: string;
  stable: string;
  balanceUsd: number | null;
  error?: string;
}

export interface DexBalancesResponse {
  balances: Record<string, number> | null;
  error?: string;
}

export type DexConfigResponse = Record<string, Record<string, Partial<DexOperationalParams>>>;

export type MeasuredDelaysResponse = Record<
  string,
  Record<string, { withdrawDelaySeconds: MeasuredDelay | null; depositDelaySeconds: MeasuredDelay | null }>
>;

export interface TestRunSummary {
  runId: string;
  createdAt: number;
  live: boolean;
}

export interface ResultHop {
  planned: PlannedHop;
  executed: ExecutedHop;
  costErrorPct: number | null;
  timeErrorPct: number | null;
}

export interface ResultJourney {
  fromDex: string;
  toDex: string;
  stable: string;
  totalEstimatedCostUsd: number;
  totalEstimatedTimeSeconds: number;
  totalActualCostUsd: number | null;
  totalActualTimeSeconds: number;
  costErrorPct: number | null;
  timeErrorPct: number | null;
  hops: ResultHop[];
}

export interface TestRun {
  live: boolean;
  unsupportedDexes: string[];
  journeys: ResultJourney[];
}

export interface ApiErrorBody {
  error?: string;
}

// Re-exported so api/client.ts callers don't need a second import for it.
export type { TestableHopInfo };
