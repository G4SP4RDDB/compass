import type { Hop } from "../types/graphData";

const MIN_R = 16, MAX_R = 34, MIN_ARC_SPACING = 70;

// Structural shape layoutNodes needs — satisfied by both a real DexNode and
// the synthetic swap-node object built in state/derivedGraphData.ts
// ({ ...GRAPH_DATA.swapNode, inbalance: 0, withdrawBalances: {} }), which
// isn't a DexNode itself.
export interface LayoutTarget {
  name: string;
  chains: string[];
  inbalance: number;
  withdrawBalances: Record<string, number>;
  angle?: number;
  cx?: number;
  cy?: number;
  r?: number;
}

type Positioned = LayoutTarget;

// Ordered by chain affinity, NOT alphabetical — so each Wallet hub's
// spokes stay on its own side of the ring and never have to sweep across
// the OTHER hub to reach it (BSC hub sits at cx<0, ARBITRUM hub at cx>0,
// see walletHubs). An alphabetical ordering scattered ARBITRUM-only DEXes
// onto the BSC side and vice versa, which is exactly what made an edge
// like "Ondo Perps -> Wallet ARBITRUM" visually cut across Wallet BSC on
// its way there even though it never touched it. A DEX supporting only one
// chain is grouped with that chain; one supporting both (MEXC, Aden, ...)
// sits at the top/bottom transition between the two groups.
//
// Every node then gets the SAME angular slot (360°/n) walking the ring in
// that group order, starting centered on the top (-90°) seam — one single
// uniform step for the whole ring, not each group spaced independently of
// the others (which used to squeeze dual-chain venues into a much
// narrower fan than single-chain ones got, an uneven-looking ring).
export function layoutNodes(dexNodes: LayoutTarget[]): number {
  const byName = (a: Positioned, b: Positioned) => a.name.localeCompare(b.name);
  const arbOnly = dexNodes.filter(d => d.chains.includes("ARBITRUM") && !d.chains.includes("BSC")).sort(byName);
  const bscOnly = dexNodes.filter(d => d.chains.includes("BSC") && !d.chains.includes("ARBITRUM")).sort(byName);
  const both = dexNodes.filter(d => d.chains.includes("ARBITRUM") && d.chains.includes("BSC")).sort(byName);
  // Neither chain (shouldn't happen with today's registry, kept so a future
  // 3rd chain never silently drops a node instead of just landing oddly).
  const other = dexNodes.filter(d => !d.chains.includes("ARBITRUM") && !d.chains.includes("BSC")).sort(byName);
  const bothTop = both.slice(0, Math.ceil(both.length / 2));
  const bothBottom = both.slice(Math.ceil(both.length / 2));
  const ordered = [...bothTop, ...arbOnly, ...bothBottom, ...bscOnly, ...other];

  const n = ordered.length;
  if (n === 0) return 220;
  // Radius sized so a FULL circle of n evenly-spaced nodes still meets
  // MIN_ARC_SPACING (arc length = 2πr/n).
  const radius = Math.max(220, (n * MIN_ARC_SPACING) / (2 * Math.PI));
  const maxActivity = Math.max(1, ...ordered.map(activityOf));
  const step = 360 / n;
  // Centers bothTop's group on -90° (its average index is (bothTop.length-1)/2)
  // and every subsequent item follows at a constant `step`, so the whole
  // ring — including the arbOnly/bothBottom/bscOnly/other groups that come
  // after it in `ordered` — ends up spaced by exactly the same angle.
  ordered.forEach((dex, i) => {
    const angleDeg = -90 + (i - (bothTop.length - 1) / 2) * step;
    const angle = (angleDeg * Math.PI) / 180;
    dex.angle = angle;
    dex.cx = Math.cos(angle) * radius;
    dex.cy = Math.sin(angle) * radius;
    dex.r = MIN_R + Math.sqrt(activityOf(dex) / maxActivity) * (MAX_R - MIN_R);
  });

  return radius;
}

// Same activity() formula as utils/format.ts, duplicated locally rather
// than imported: layoutNodes also runs on the synthetic swapNode object
// (inbalance: 0, withdrawBalances: {}) which isn't a real DexNode, and
// utils/format.ts's activity() is typed against DexNode specifically.
function activityOf(dex: { inbalance: number; withdrawBalances: Record<string, number> }): number {
  const withdrawTotal = Object.values(dex.withdrawBalances).reduce((s, v) => s + v, 0);
  return Math.max(Math.abs(dex.inbalance), withdrawTotal);
}

export interface WalletHub {
  name: string;
  chain: string;
  cx: number;
  cy: number;
  r: number;
}

export type Waypoint = Positioned | WalletHub;

// Décompose une pathEdge (DEX -> DEX) en points de passage réels
// [dexFrom, hub(s)..., dexTo] à partir de SES PROPRES hops Dijkstra
// (e.hops, déjà calculés une fois pour toutes — voir web_view.py
// _buildHopList/_testableHopInfo pour `.testable`), PAS des hops du trajet
// réellement choisi par le solveur : ça permet de construire tous les
// segments UNE SEULE FOIS au chargement de la page. Dans de
// rares cas, la route affichée peut donc différer légèrement du choix
// conjoint réel du solveur — même famille d'approximation que
// Journey.plausible ailleurs dans ce code. Repli sur [dexFrom, dexTo]
// (le chord direct d'avant) si un hop Withdraw/Deposit manque ou que sa
// chain ne correspond à aucun hub connu : rien ne disparaît jamais.
export function journeyWaypoints(
  e: { from: string; to: string; hops?: Hop[] },
  dexByName: Map<string, Positioned>,
  walletHubByChain: Map<string, WalletHub>
): Waypoint[] {
  const fromNode = dexByName.get(e.from), toNode = dexByName.get(e.to);
  const direct = [fromNode as Waypoint, toNode as Waypoint];
  const hops = e.hops || [];
  const withdrawHop = hops.find(h => h.testable && h.testable.hopType === "Withdraw");
  const depositHop = [...hops].reverse().find(h => h.testable && h.testable.hopType === "Deposit");
  if (!withdrawHop || !depositHop) return direct;

  const hubA = walletHubByChain.get(withdrawHop.testable!.chain);
  const hubB = walletHubByChain.get(depositHop.testable!.chain);
  if (!hubA || !hubB) return direct;
  return hubA.chain === hubB.chain ? [fromNode as Waypoint, hubA, toNode as Waypoint] : [fromNode as Waypoint, hubA, hubB, toNode as Waypoint];
}

// La stable ENVOYÉE d'un trajet (voir Journey.stable côté Python) : jamais
// ambiguë par trajet individuel (un WithdrawNode = une stable précise), mais
// un même edge visuel (paire de DEX) peut agréger PLUSIEURS trajets si le
// solveur y a fait transiter plusieurs commodités. On prend celle du premier
// trajet — sous le registre actuel (une seule stable par DEX), tous les
// trajets d'un même edge partagent de toute façon la même stable de départ.
export function journeyStable(journeys: { stable: string }[] | null): string | null {
  return journeys && journeys.length ? journeys[0]!.stable : null;
}

export interface EdgeGeometry {
  sx: number;
  sy: number;
  ex: number;
  ey: number;
  mx: number;
  my: number;
}

export function edgeGeometry(a: { cx: number; cy: number; r: number }, b: { cx: number; cy: number; r: number }): EdgeGeometry {
  const dx = b.cx - a.cx, dy = b.cy - a.cy;
  const dist = Math.hypot(dx, dy) || 1;
  const ux = dx / dist, uy = dy / dist;
  const nx = -dy / dist, ny = dx / dist;
  const sx = a.cx + ux * a.r, sy = a.cy + uy * a.r;
  const ex = b.cx - ux * b.r, ey = b.cy - uy * b.r;
  const curvature = 14;
  const mx = (sx + ex) / 2 + nx * curvature;
  const my = (sy + ey) / 2 + ny * curvature;
  return { sx, sy, ex, ey, mx, my };
}
