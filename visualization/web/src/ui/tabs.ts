import { initTestResults } from "../panels/testResults";

function switchTab(name: string, fitToScreen: () => void): void {
  document.getElementById("exploreView")!.classList.toggle("hidden", name !== "explore");
  document.getElementById("operationsView")!.classList.toggle("active", name === "operations");
  document.getElementById("configView")!.classList.toggle("active", name === "config");
  document.getElementById("testResultsView")!.classList.toggle("active", name === "testResults");
  document.getElementById("tabExplore")!.classList.toggle("active", name === "explore");
  document.getElementById("tabOperations")!.classList.toggle("active", name === "operations");
  document.getElementById("tabConfig")!.classList.toggle("active", name === "config");
  document.getElementById("tabTestResults")!.classList.toggle("active", name === "testResults");
  if (name === "explore") requestAnimationFrame(fitToScreen);
  if (name === "testResults") initTestResults();
}

export function bindTabs(fitToScreen: () => void): void {
  document.getElementById("tabExplore")!.addEventListener("click", () => switchTab("explore", fitToScreen));
  document.getElementById("tabOperations")!.addEventListener("click", () => switchTab("operations", fitToScreen));
  document.getElementById("tabConfig")!.addEventListener("click", () => switchTab("config", fitToScreen));
  document.getElementById("tabTestResults")!.addEventListener("click", () => switchTab("testResults", fitToScreen));
}
