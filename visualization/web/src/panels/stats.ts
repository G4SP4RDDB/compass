import { fmt, fmtDuration } from "../utils/format";
import { pathEdges } from "../state/derivedGraphData";
import type { Operation } from "../types/graphData";

const reachablePairs = pathEdges.length;
const totalPairs = GRAPH_DATA.dexNodes.length * (GRAPH_DATA.dexNodes.length - 1);

export interface OperationsSummary {
  count: number;
  totalAmount: number;
  totalCost: number;
  totalTime: number;
}

// Résumé du plan réellement choisi par le solveur CP-SAT (edge.flow > 0),
// pas l'estimation Dijkstra par paire montrée dans l'onglet Explore. Partagé
// entre le header (visible sur tous les onglets) et l'onglet "Chosen
// Operations" (voir panels/operations.ts) pour ne calculer qu'une seule fois.
function operationsSummary(ops: Operation[]): OperationsSummary | null {
  if (ops.length === 0) return null;
  return {
    count: ops.length,
    totalAmount: ops.reduce((s, o) => s + o.amount, 0),
    totalCost: ops.reduce((s, o) => s + o.cost, 0),
    // Approximation volontaire : le plus long délai parmi tous les hops
    // choisis, pas la somme des hops le long d'une chaîne (un vrai chemin
    // critique demanderait de rejouer la décomposition en journeys par
    // chaîne — voir visualization/journeys.py — plutôt que ce max global).
    totalTime: Math.max(0, ...ops.map(o => o.time || 0)),
  };
}

export const opsSummaryData: OperationsSummary | null = operationsSummary(GRAPH_DATA.operations);

export function renderStats(): void {
  document.getElementById("stats")!.innerHTML = `
    <span><b>${GRAPH_DATA.dexNodes.length}</b> DEXes</span>
    <span><b>${reachablePairs}</b> / ${totalPairs} routes reachable</span>` + (opsSummaryData ? `
    <span><b>${opsSummaryData.count}</b> chosen operations</span>
    <span><b>$${fmt(opsSummaryData.totalAmount)}</b> moved</span>
    <span><b>$${fmt(opsSummaryData.totalCost)}</b> fees</span>
    <span title="Longest single hop among the chosen operations, not the sum along a chain"><b>${fmtDuration(opsSummaryData.totalTime)}</b> est. total time</span>` : "");
}
