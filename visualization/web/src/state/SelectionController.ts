import type { Selection } from "../types/selection";
import type { GraphRenderer } from "../graph/GraphRenderer";
import { dexByName, swapNode, type PathEdge } from "./derivedGraphData";
import type { Journey } from "../types/graphData";
import { renderDexDetails, setSelectedDexNameForBalanceGuard } from "../panels/dexDetails";
import { renderSwapDetails } from "../panels/swapDetails";
import { renderWalletDetails } from "../panels/walletDetails";
import { renderEdgeDetails } from "../panels/edgeDetails";

// Replaces the original's four independent selectedName/selectedEdgeKey/
// selectedWalletChain/selectedSwap variables with one discriminated union
// (see types/selection.ts) plus the methods that used to be loose top-level
// functions (clearSelection/selectDex/selectSwap/selectWallet/selectEdge).
// Constructed with a GraphRenderer instance (for highlighting) but never
// imports panels' render functions' *callers* back — see graph/
// GraphRenderer.ts's callback-based design and the plan's "Breaking the
// cycles" for why this class and GraphRenderer never import each other.
export class SelectionController {
  current: Selection = { kind: "none" };
  private graphRenderer: GraphRenderer;

  constructor(graphRenderer: GraphRenderer) {
    this.graphRenderer = graphRenderer;
  }

  clear(): void {
    this.current = { kind: "none" };
    setSelectedDexNameForBalanceGuard(null);
    this.graphRenderer.clearHighlights();
    document.getElementById("details")!.innerHTML = "";
  }

  selectDex(name: string): void {
    const dex = dexByName.get(name);
    if (!dex) return;
    this.clear();
    this.current = { kind: "dex", name };
    setSelectedDexNameForBalanceGuard(name);
    this.graphRenderer.highlightForDexSelection(name);
    renderDexDetails(dex, (targetName) => this.selectDexAndFocus(targetName));
  }

  // Clicking a DEX's name inside another DEX's "Paths to other DEXes" list
  // both selects AND pans/zooms to it — unlike clicking the node itself
  // (already on screen, no pan needed). See panels/dexDetails.ts's
  // `onNavigate` callback.
  selectDexAndFocus(name: string): void {
    const dex = dexByName.get(name);
    this.selectDex(name);
    if (dex) this.graphRenderer.focusOn(dex.cx!, dex.cy!);
  }

  // LE node "swap" — pas de SourceNode/paths Dijkstra derrière.
  selectSwap(): void {
    if (!swapNode) return;
    this.clear();
    this.current = { kind: "swap" };
    this.graphRenderer.highlightForSwapSelection();
    renderSwapDetails(swapNode);
  }

  selectWallet(chain: string): void {
    this.clear();
    this.current = { kind: "wallet", chain };
    this.graphRenderer.highlightForWalletSelection(chain);
    renderWalletDetails(chain);
  }

  selectEdge(e: PathEdge, journeys: Journey[] | null): void {
    this.clear();
    this.current = { kind: "edge", key: `${e.from}→${e.to}` };
    this.graphRenderer.highlightForEdgeSelection(e);
    renderEdgeDetails(e, journeys);
  }

  // Re-renders whichever Details panel is currently open, without touching
  // selection/highlight state — used when data the open panel displays
  // changed underneath it (imbalances/wallet-deficits finished loading, or
  // a live hop just moved funds), not when the user picked something new.
  // Slightly more generic than the original (which only ever refreshed the
  // one panel kind each specific event could plausibly affect): here any
  // currently-open panel refreshes on any of these events. The other kinds
  // are cheap, harmless re-renders when the event turns out unrelated to
  // what's open — never a stale-data bug in the other direction.
  refreshCurrent(): void {
    switch (this.current.kind) {
      case "dex": {
        const dex = dexByName.get(this.current.name);
        if (dex) renderDexDetails(dex, (targetName) => this.selectDexAndFocus(targetName));
        break;
      }
      case "swap":
        if (swapNode) renderSwapDetails(swapNode);
        break;
      case "wallet":
        renderWalletDetails(this.current.chain);
        break;
      case "edge":
      case "none":
        break;
    }
  }
}
