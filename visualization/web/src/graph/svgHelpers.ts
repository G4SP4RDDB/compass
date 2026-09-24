import { createFlowDots } from "./flowDots";
import type { EdgeGeometry } from "./layout";
import { SVG_NS } from "./svgNamespace";

export function createSvgElement<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attrs?: Record<string, string>
): SVGElementTagNameMap[K] {
  const el = document.createElementNS(SVG_NS, tag) as SVGElementTagNameMap[K];
  if (attrs) for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

// Applique/retire les classes stable-usdc/stable-usdt sur une edge "chosen"
// (voir .edge.chosen.stable-* dans :root).
export function setEdgeStableClass(path: SVGPathElement, stable: string | null): void {
  path.classList.toggle("stable-usdc", stable === "USDC");
  path.classList.toggle("stable-usdt", stable === "USDT");
}

export interface EdgeSegmentElements {
  path: SVGPathElement;
  dots: SVGGElement;
  hitPath: SVGPathElement;
  speedFactor: number;
}

// The path/dots/hitPath triple every edge segment needs, factored out of
// three near-identical ~25-30 line blocks in the original: the main
// pathEdges loop, the walletJourneyEdges loop, and the swap node's
// locally-scoped `drawSwapStub` helper (which had already half-recognized
// this pattern on its own — same create-path/create-dots/create-hitPath/
// append sequence, three separate times). Each call site still sets its own
// extra classes/dataset/marker/event-listeners beyond what's common here.
export function drawEdgeSegment(
  edgesLayer: Element,
  geo: EdgeGeometry,
  pathId: string,
  stable: string | null
): EdgeSegmentElements {
  const d = `M ${geo.sx} ${geo.sy} Q ${geo.mx} ${geo.my} ${geo.ex} ${geo.ey}`;
  const path = createSvgElement("path", { id: pathId, d, class: "edge", "marker-end": "url(#arrow)" });
  edgesLayer.appendChild(path);

  const pathLength = Math.hypot(geo.ex - geo.sx, geo.ey - geo.sy);
  const speedFactor = Math.max(0.6, pathLength / 260);
  const dots = createFlowDots(pathId, speedFactor, stable);
  edgesLayer.appendChild(dots);

  const hitPath = createSvgElement("path", { d, class: "edge-hit" });
  edgesLayer.appendChild(hitPath);

  return { path, dots, hitPath, speedFactor };
}
