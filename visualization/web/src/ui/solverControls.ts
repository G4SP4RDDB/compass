import { postRecompute, postSolveMode } from "../api/client";

// Reconstruit tout le graphe from scratch côté serveur (nouveaux
// déséquilibres saisis à la main — voir main.buildAndSolveGraph et POST
// /api/recompute) : les balances changent, donc un patch DOM en place ne
// suffit pas — on recharge la page servie fraîchement re-rendue.
//
// Cheapest/Fastest toggle: all-or-nothing route priority (see
// graph.solver.RouteMode) — re-solves the graph already in memory (POST
// /api/solve, no rebuild) and reloads, same reload-the-page pattern: there's
// no in-place re-render entrypoint for operations/journeys, and the page is
// cheap to reload.
export function bindSolverControls(): void {
  const recomputeBtn = document.getElementById("recomputeBtn") as HTMLButtonElement;
  const recomputeStatusEl = document.getElementById("recomputeStatus")!;

  recomputeBtn.addEventListener("click", async () => {
    recomputeBtn.disabled = true;
    recomputeStatusEl.textContent = "Solving from the saved imbalances… (a few seconds)";
    recomputeStatusEl.className = "status-note saving";
    try {
      await postRecompute();
      window.location.reload();
    } catch (e) {
      const message = (e as Error).message;
      recomputeStatusEl.textContent = message.startsWith("Failed to fetch")
        ? "Not connected to the solve server — can't run the solver. Serve this page with `python -m visualization.server`."
        : `Can't solve: ${message}`;
      recomputeStatusEl.className = "status-note offline";
      recomputeBtn.disabled = false;
    }
  });

  const modeButtons = [document.getElementById("modeCheapestBtn"), document.getElementById("modeFastestBtn")] as HTMLButtonElement[];
  function setActiveModeButton(mode: string): void {
    for (const btn of modeButtons) btn.classList.toggle("active", btn.dataset.mode === mode);
  }
  setActiveModeButton(GRAPH_DATA.routeMode || "cheapest");
  for (const btn of modeButtons) {
    btn.addEventListener("click", async () => {
      if (btn.classList.contains("active")) return;
      for (const b of modeButtons) b.disabled = true;
      recomputeStatusEl.textContent = "Re-solving…";
      recomputeStatusEl.className = "status-note saving";
      try {
        await postSolveMode(btn.dataset.mode!);
        window.location.reload();
      } catch (e) {
        const message = (e as Error).message;
        recomputeStatusEl.textContent = message.startsWith("Failed to fetch")
          ? "Not connected to the solve server — can't switch route mode. Serve this page with `python -m visualization.server`."
          : `Can't switch mode: ${message}`;
        recomputeStatusEl.className = "status-note offline";
        for (const b of modeButtons) b.disabled = false;
      }
    });
  }
}
