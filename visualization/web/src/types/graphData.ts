// Interfaces for the GRAPH_DATA payload injected into the page (see
// index.template.html's `const GRAPH_DATA = __GRAPH_DATA_JSON__;`).
// Source of truth is Python's web_view.py::graphToDict (plain
// `dict[str, Any]`, no TypedDict there — nothing auto-checks these two
// sides stay in sync, see also _dexNodeDict/_swapNodeDict/_journeyDict for
// the exact per-field construction). Update both sides together.

export interface MeasuredDelay {
  meanSeconds: number;
  n: number;
  stdSeconds: number;
  minSeconds: number;
  maxSeconds: number;
  lastMeasuredAt: number | null;
}

export interface DexOperationalParams {
  withdrawFeeUsd: number;
  withdrawDelaySeconds: number;
  minWithdrawUsd: number;
  depositFeeUsd: number;
  depositDelaySeconds: number;
  minDepositUsd: number;
}

export interface DexMeasuredDelays {
  withdrawDelaySeconds: MeasuredDelay | null;
  depositDelaySeconds: MeasuredDelay | null;
}

export interface DexNode {
  id: number;
  name: string;
  inbalance: number;
  stables: string[];
  chains: string[];
  withdrawBalances: Record<string, number>;
  withdrawChainByStable: Record<string, string>;
  requiresSameChainWithdraw: boolean;
  logo: string | null;
  brandColor: string | null;
  operationalParams: Record<string, DexOperationalParams>;
  measuredDelays: Record<string, DexMeasuredDelays>;
  // Set by graph/layout.ts::layoutNodes, not present in the raw payload.
  angle?: number;
  cx?: number;
  cy?: number;
  r?: number;
}

export interface SwapNode {
  name: string;
  chains: string[];
  logo: string | null;
  brandColor: string | null;
}

export interface TestableHopInfo {
  dex: string;
  chain: string;
  stable: string;
  toStable?: string;
  hopType: "Withdraw" | "Deposit" | "Swap";
}

export interface Hop {
  from: string;
  to: string;
  cost: number;
  time: number;
  type: string;
  protocol: string | null;
  testable: TestableHopInfo | null;
}

export interface RouteEstimate {
  totalCost: number;
  totalTime: number;
  hops: Hop[];
}

export interface PathEntry {
  cheapest: RouteEstimate;
  fastest: RouteEstimate;
}

export interface Operation {
  from: string;
  to: string;
  amount: number;
  cost: number;
  time: number;
  type: string;
  protocol: string | null;
  testable: TestableHopInfo | null;
}

export interface Journey {
  from: string;
  to: string;
  amount: number;
  stable: string;
  plausible: boolean;
  fromWallet: boolean;
  totalCost: number;
  totalTime: number;
  hops: Hop[];
}

export interface WalletNode {
  chain: string;
  stable: string;
  balance: number;
}

export interface TimeWeight {
  lambdaMin: number;
  lambdaMax: number;
  k: number;
  epsilon: number;
}

export interface GraphData {
  dexNodes: DexNode[];
  swapNode: SwapNode | null;
  paths: Record<string, Record<string, PathEntry>>;
  operations: Operation[];
  journeys: Journey[];
  timeWeight: TimeWeight | null;
  routeMode: "cheapest" | "fastest" | null;
  walletNodes: WalletNode[];
}

declare global {
  const GRAPH_DATA: GraphData;
}
