import { fmt, fmtDuration, typeSlug, typeTag } from "../utils/format";
import type { Hop, RouteEstimate } from "../types/graphData";
import type { TestableHopInfo } from "../types/api";
import { executeButtonHtml } from "../execution/liveExecution";

// `testable`: only true for hops in the solver's ACTUAL chosen journey (see
// panels/edgeDetails.ts) — never for the "Estimated routes" cheapest/fastest
// Dijkstra panel, which is informative only, not something the solver
// picked (see web_view.py's two-views comment). h.testable itself (from
// web_view.py::_testableHopInfo) is null for Bridge hops regardless —
// compass_test tests Withdraw/Deposit, and Swap (via CoW Swap, BSC/Arbitrum
// only — `toStable` then names the stable bought).
export function hopHtml(h: Hop, testable?: boolean, executeAmount?: number): string {
  // A cross-chain move is one hop here (entry/cross/exit already merged
  // server-side, see web_view.py:_buildHopList) — h.protocol names which
  // bridge protocol the solver actually picked for it (GENERIC / CCTP V1 /
  // CCTP V2), null for any non-Bridge hop.
  const protocol = h.protocol ? ` <span class="hop-protocol">via ${h.protocol}</span>` : "";
  const testSection = (testable && h.testable) ? testEdgeSectionHtml(h.testable, executeAmount) : "";
  return `<div class="hop type-${typeSlug(h.type)}">${typeTag(h.type)} <span class="hop-cost">${fmt(h.cost)} · ${fmtDuration(h.time)}</span><b>${h.from}</b> → ${h.to}${protocol}${testSection}</div>`;
}

export function testEdgeSectionHtml(t: TestableHopInfo, executeAmount?: number): string {
  // executeAmount: the solver's amount for this journey, so the hop can be
  // executed for real at that amount (see execution/liveExecution.ts's
  // executeButtonHtml). Absent for the informational "Estimated routes" panel.
  const exec = executeAmount ? ` ${executeButtonHtml(t, executeAmount)}` : "";
  return `<div class="hop-test">
    <button class="test-edge-btn" data-dex="${t.dex}" data-chain="${t.chain}" data-stable="${t.stable}" data-to-stable="${t.toStable || ""}" data-hop-type="${t.hopType}">Test This Edge</button>${exec}
    <div class="hop-test-result"></div>
  </div>`;
}

// Deux estimations Dijkstra indépendantes (voir web_view.py:_computeDexPaths)
// coïncident souvent (même route la moins chère et la plus rapide) : plutôt
// que d'afficher deux blocs identiques, on les fusionne quand les hops sont
// exactement la même séquence.
export function routesEqual(a: RouteEstimate, b: RouteEstimate): boolean {
  return (
    a.hops.length === b.hops.length &&
    a.hops.every((h, i) => h.from === b.hops[i]!.from && h.to === b.hops[i]!.to && h.type === b.hops[i]!.type && h.protocol === b.hops[i]!.protocol)
  );
}

export function routeBlockHtml(label: string, route: RouteEstimate): string {
  // Explicit single-arg wrapper, NOT route.hops.map(hopHtml) directly:
  // Array.prototype.map calls its callback as (item, index, array) — passed
  // bare, hopHtml's second param (`testable`) would silently receive the
  // INDEX instead (truthy for every hop after the first), which is exactly
  // how "Test This Edge" first ended up leaking into this informational
  // routes panel during testing.
  const hops = route.hops.map(h => hopHtml(h)).join("") || `<div class="hop">direct — no intermediate hop</div>`;
  return `
    <div class="path-row">
      <div class="route-head">
        <span style="flex:1">${label}</span>
        <span class="cost">${fmt(route.totalCost)} · ${fmtDuration(route.totalTime)}</span>
      </div>
      <div class="path-hops expanded">${hops}</div>
    </div>`;
}

export function routesSectionHtml(cheapest: RouteEstimate, fastest: RouteEstimate): string {
  return routesEqual(cheapest, fastest)
    ? routeBlockHtml("Cheapest & fastest", cheapest)
    : routeBlockHtml("Cheapest", cheapest) + routeBlockHtml("Fastest", fastest);
}
