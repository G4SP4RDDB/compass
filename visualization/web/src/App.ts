import { GraphRenderer } from "./graph/GraphRenderer";
import { SelectionController } from "./state/SelectionController";
import { bindTabs } from "./ui/tabs";
import { bindSolverControls } from "./ui/solverControls";
import { renderStats } from "./panels/stats";
import { renderOperations } from "./panels/operations";
import { initDexConfig, bindConfigSaveButton } from "./panels/config";
import { renderWalletBalancesSidebar } from "./panels/walletBalancesSidebar";
import { initImbalances, onImbalancesLoaded } from "./forms/imbalanceForm";
import { initWalletDeficits, onWalletDeficitsLoaded } from "./forms/walletDeficitForm";
import { onLiveRunComplete } from "./execution/liveHopRequest";
import { getWalletBalances } from "./state/walletBalancesCache";

// Composition root: wires graph/GraphRenderer.ts and state/
// SelectionController.ts together (they never import each other directly —
// see GraphRenderer's callback-based design), then the three cycle-breaking
// hooks described in the plan's "Breaking the cycles" (forms/
// imbalanceForm.ts, forms/walletDeficitForm.ts, execution/liveHopRequest.ts
// each expose a setter instead of importing panels directly), then runs the
// same init/render sequence the original script ran top-to-bottom at the
// bottom of the file, now explicit instead of implicit load-order.
export class App {
  start(): void {
    // GraphRenderer's click callbacks need SelectionController, but
    // SelectionController's constructor needs a GraphRenderer instance —
    // resolved with the standard "declare, construct the first with
    // closures over the second, then assign" pattern. The callbacks are
    // never invoked synchronously during construction (only later, on user
    // interaction), so `selectionController` is always assigned by the
    // time any of them actually run.
    let selectionController!: SelectionController;
    const graphRenderer = new GraphRenderer({
      onSelectDex: (name) => selectionController.selectDex(name),
      onSelectSwap: () => selectionController.selectSwap(),
      onSelectWallet: (chain) => selectionController.selectWallet(chain),
      onSelectEdge: (e, journeys) => selectionController.selectEdge(e, journeys),
      onClear: () => selectionController.clear(),
    });
    selectionController = new SelectionController(graphRenderer);

    bindTabs(() => graphRenderer.fitToScreen());
    bindSolverControls();
    bindConfigSaveButton();

    onImbalancesLoaded(() => selectionController.refreshCurrent());
    onWalletDeficitsLoaded(() => selectionController.refreshCurrent());
    // A live hop just moved real funds — the cached wallet balance is now
    // stale, unlike every other UI action (this is the only place that
    // actually triggers an on-chain operation). Refresh the cache and
    // whichever wallet/DEX Details panel is currently open.
    onLiveRunComplete(() => {
      getWalletBalances(true);
      renderWalletBalancesSidebar();
      selectionController.refreshCurrent();
    });

    initImbalances();
    initWalletDeficits();
    initDexConfig();

    renderStats();
    renderOperations();
    renderWalletBalancesSidebar();

    requestAnimationFrame(() => graphRenderer.fitToScreen());
    window.addEventListener("resize", () => graphRenderer.fitToScreen());
  }
}
