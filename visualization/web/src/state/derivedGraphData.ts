// Data derived once from GRAPH_DATA at module load — mirrors exactly what
// the original script computed at top-level scope before any function
// definitions. Pure (no DOM access); graph/GraphRenderer.ts is where any of
// this actually gets drawn.

import type { DexNode, Journey } from "../types/graphData";
import { layoutNodes, type LayoutTarget } from "../graph/layout";

export const dexByName = new Map<string, DexNode>(GRAPH_DATA.dexNodes.map(d => [d.name, d]));
export const dexById = new Map<number, DexNode>(GRAPH_DATA.dexNodes.map(d => [d.id, d]));

// LE node "swap" (voir GRAPH_DATA.swapNode / web_view.py::_swapNodeDict) :
// un seul node pour toutes les chains (contrairement à un DEX, un par node),
// mais placé SUR LE MÊME ANNEAU que les DEX, au même titre qu'eux — voir
// l'appel à layoutNodes juste en dessous, où il est passé dans le même
// tableau. `inbalance`/`withdrawBalances` à zéro (aucun concept de surplus/
// déficit pour un swap venue) uniquement pour satisfaire activity()/sign(),
// que layoutNodes appelle génériquement sur chaque entrée — `chains`
// contenant à la fois ARBITRUM et BSC, il rejoint naturellement le groupe
// "both" (voir layoutNodes) et se fait placer/fanned à côté d'Aden/MEXC au
// lieu d'un point fixe arbitraire. null si le graphe n'a aucune edge Swap.
export const swapNode: (LayoutTarget & { logo: string | null; brandColor: string | null; chains: string[] }) | null = GRAPH_DATA.swapNode
  ? { ...GRAPH_DATA.swapNode, inbalance: 0, withdrawBalances: {} }
  : null;

export const ringRadius = layoutNodes(swapNode ? [...GRAPH_DATA.dexNodes, swapNode] : GRAPH_DATA.dexNodes);

// Trois nœuds "Wallet" FIXES (pas des données du solveur — structurels) :
// BSC/ARBITRUM, dont le solde ON-CHAIN LIVE est lu (scope volontairement
// limité à USDC/USDT sur ces deux chains, comme le panel "Wallet (on-chain,
// live)" du sidebar, voir fetchWalletBalances), et SOLANA, le vault de sortie
// du withdraw pipeline CCTP (voir graph.structures.bridges/Graph.
// _linkWalletPayouts côté Python) — pas encore de lecteur de solde live pour
// celui-ci (voir renderWalletDetails), mais le même panel "Withdraw amount"
// EXISTE déjà dessus (walletDeficitFormHtml est partagé entre toutes les
// chains, voir son commentaire). Tout ce qui passe par un Withdraw ou un
// Deposit choisi par le solveur transite visuellement par l'un des trois
// (voir journeyWaypoints). BSC/ARBITRUM posés de part et d'autre du centre
// géométrique pour laisser le watermark "COMPASS" visible dans l'espace
// entre eux, à `{cx, cy, r}` — le même shape qu'un DEX (voir dexNodes/
// layoutNodes) — pour que edgeGeometry() marche dessus sans aucune
// modification. SOLANA délibérément posé EN DEHORS de l'anneau des DEX
// (cy = ringRadius + marge, pas un offset fixe comme les deux autres) : ce
// n'est pas un hub que les DEX traversent (aucun DEX du registre ne vit sur
// Solana), juste le point de sortie final du withdraw pipeline — le garder
// à l'écart du cercle évite de le laisser passer pour un troisième hub
// "comme les autres" au premier coup d'œil. fitToScreen (plus bas) inclut
// walletHubs dans son cadrage précisément pour que ce nœud, plus loin que
// tout DEX, ne sorte jamais du viewport.
const WALLET_HUB_OFFSET = 90, WALLET_HUB_R = 26;
export const walletHubs = [
  { name: "BSC", chain: "BSC", cx: -WALLET_HUB_OFFSET, cy: 0, r: WALLET_HUB_R },
  { name: "ARBITRUM", chain: "ARBITRUM", cx: WALLET_HUB_OFFSET, cy: 0, r: WALLET_HUB_R },
  { name: "SOLANA", chain: "SOLANA", cx: 0, cy: ringRadius + WALLET_HUB_R + 50, r: WALLET_HUB_R },
];
export const WALLET_HUB_ICON: Record<string, string> = { ARBITRUM: "ARB", BSC: "BSC", SOLANA: "SOL" };
export const walletHubByChain = new Map(walletHubs.map(w => [w.chain, w]));

export interface PathEdge {
  from: string;
  to: string;
  cost: number;
  time: number;
  hops: import("../types/graphData").Hop[];
  cheapest: import("../types/graphData").RouteEstimate;
  fastest: import("../types/graphData").RouteEstimate;
}

// Chaque paire (X, Y) atteignable devient une seule arête directe : le chemin
// réel (via bridges/swaps) reste calculé côté Python (voir web_view.py
// _computeDexPaths) et n'est affiché qu'au clic, dans le panneau latéral.
// GRAPH_DATA.paths[from][to] porte DEUX chemins estimés (cheapest/fastest,
// voir _routesFromSource) : l'arc dessiné sur l'anneau (largeur/couleur/
// label) reste piloté par "cheapest" comme avant, "fastest" n'est utilisé
// qu'au clic dans le panneau latéral (voir renderEdgeDetails/routeBlockHtml).
export const pathEdges: PathEdge[] = [];
for (const [fromName, targets] of Object.entries(GRAPH_DATA.paths)) {
  for (const [toName, path] of Object.entries(targets)) {
    pathEdges.push({
      from: fromName,
      to: toName,
      cost: path.cheapest.totalCost,
      time: path.cheapest.totalTime,
      hops: path.cheapest.hops,
      cheapest: path.cheapest,
      fastest: path.fastest,
    });
  }
}
export const maxCost = Math.max(1e-9, ...pathEdges.map(e => e.cost));

// Trajets DEX -> DEX réellement choisis par le solveur (edge.flow > 0),
// décomposés côté Python dans visualization/journeys.py — potentiellement
// plusieurs par paire si le solveur les a répartis sur des hops distincts.
// `plausible` marque un trajet dont l'attribution source/destination n'est
// qu'une explication parmi d'autres compatibles avec le même flot agrégé
// (voir Journey.plausible dans journeys.py).
export const journeysByPair = new Map<string, Journey[]>();
for (const j of GRAPH_DATA.journeys) {
  const key = `${j.from}→${j.to}`;
  if (!journeysByPair.has(key)) journeysByPair.set(key, []);
  journeysByPair.get(key)!.push(j);
}

export interface WalletJourneyEdge {
  // Every wallet hub chain the journey actually passes through, in order
  // (one entry per Bridge hop's destination, deduped against repeats) --
  // NOT just a single "fromChain"/intermediate "toChain" pair, since a
  // journey can hop TWO bridges before reaching its destination (e.g.
  // BSC -> Aden bridge -> ARBITRUM -> CCTP bridge -> SOLANA, see the
  // payout pipeline in graph.structures.bridges/Graph._linkWalletPayouts) :
  // collapsing that to first/last chain alone would draw a single straight
  // segment and silently skip the intermediate bridge leg.
  hubChains: string[];
  to: string;
  journeys: Journey[];
}

// Trajets qui partent de l'argent DÉJÀ dans un wallet (Journey.fromWallet,
// voir WalletNode.balance côté Python) : pas d'arête DEX -> DEX à laquelle
// les rattacher (journeysByPair ne matche que des paires de DEX), donc sans
// ce passage le graphe resterait vide alors que le header annonce des
// opérations choisies. Un segment "chosen" par hub, un par chain traversée
// -- via autant de hubs intermédiaires que de bridges empruntés -- avec les
// mêmes badges animés ; le clic ouvre le panel du wallet, où le trajet et
// son bouton Execute sont listés (voir renderWalletDetails). `to` peut être
// un DEX OU "User payouts" (voir web_view.py::_nodeLabel) -- un trajet
// WalletDeficit n'a pas de DEX de destination, GraphRenderer.
// drawWalletJourneyEdges gère ce cas en s'arrêtant au dernier hub plutôt
// qu'en cherchant un node DEX inexistant.
export const walletJourneyEdges = new Map<string, WalletJourneyEdge>();
for (const j of GRAPH_DATA.journeys) {
  if (!j.fromWallet) continue;
  const fromChain = j.from.replace(/^Wallet /, "").split("/")[0]!;
  const hubChains = [fromChain];
  for (const h of j.hops) {
    if (h.testable && h.testable.hopType === "Bridge" && h.testable.toChain && h.testable.toChain !== hubChains[hubChains.length - 1]) {
      hubChains.push(h.testable.toChain);
    }
  }
  const key = `${hubChains.join("→")}→${j.to}`;
  if (!walletJourneyEdges.has(key)) walletJourneyEdges.set(key, { hubChains, to: j.to, journeys: [] });
  walletJourneyEdges.get(key)!.journeys.push(j);
}
