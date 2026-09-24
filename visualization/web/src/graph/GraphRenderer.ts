import { fmt, fmtSigned, sign } from "../utils/format";
import { journeyWaypoints, edgeGeometry, journeyStable, type WalletHub } from "./layout";
import { setEdgeStableClass, drawEdgeSegment } from "./svgHelpers";
import { SVG_NS, XLINK_NS } from "./svgNamespace";
import { dexByName, swapNode, ringRadius, walletHubs, WALLET_HUB_ICON, walletHubByChain, pathEdges, maxCost, journeysByPair, walletJourneyEdges, type PathEdge } from "../state/derivedGraphData";
import type { Journey } from "../types/graphData";
import { showTooltip, positionTooltip, hideTooltip } from "../ui/tooltip";
import { nodeTooltipHtml } from "../panels/dexDetails";
import { edgeTooltipHtml } from "../panels/edgeDetails";
import { swapNodeTooltipHtml } from "../panels/swapDetails";

interface Positioned {
  cx: number;
  cy: number;
  r: number;
}

// dex/swap-node cx/cy/r are typed optional in types/graphData.ts (they
// don't exist in the raw GRAPH_DATA payload — graph/layout.ts::layoutNodes
// fills them in). By the time GraphRenderer is ever constructed,
// state/derivedGraphData.ts's module-load-time layoutNodes() call has
// already run on every node passed here, so they're always populated in
// practice; this cast documents that construction-time invariant instead of
// threading `!` through every call site.
function positioned(w: { cx?: number; cy?: number; r?: number }): Positioned {
  return w as Positioned;
}

export interface GraphRendererCallbacks {
  onSelectDex: (name: string) => void;
  onSelectSwap: () => void;
  onSelectWallet: (chain: string) => void;
  onSelectEdge: (e: PathEdge, journeys: Journey[] | null) => void;
  onClear: () => void;
}

interface EdgeElementRef {
  e: { from: string; to: string };
  path: SVGPathElement;
  label: SVGTextElement;
  dots: SVGGElement;
  hitPath: SVGPathElement;
  state: { journeys: Journey[] | null; dotsStable: string | null };
  pathId: string;
  speedFactor: number;
}

// Owns the whole SVG scene (nodes, edges, wallet hubs) and the pan/zoom
// `view` transform entirely privately — nothing outside this class touches
// `nodeElements`/`edgeElements`/`walletHubElements`/`swapNodeElement`/`view`
// directly. The original code built all of this as one long sequence of
// top-level script statements; the constructor here runs that exact same
// sequence explicitly instead of relying on script-load order. Click
// callbacks are passed in (rather than importing state/SelectionController.ts
// directly) so this class and SelectionController never import each other —
// see the plan's "Breaking the cycles".
export class GraphRenderer {
  private svg: HTMLElement;
  private viewport: HTMLElement;
  private edgesLayer: HTMLElement;
  private nodesLayer: HTMLElement;
  private canvasWrap: HTMLElement;

  private nodeElements = new Map<string, SVGGElement>();
  private edgeElements: EdgeElementRef[] = [];
  private walletHubElements = new Map<string, SVGGElement>();
  private swapNodeElement: SVGGElement | null = null;
  private edgeCounter = 0;

  private view = { x: 0, y: 0, k: 1 };
  private callbacks: GraphRendererCallbacks;

  constructor(callbacks: GraphRendererCallbacks) {
    this.callbacks = callbacks;
    this.svg = document.getElementById("graphSvg")!;
    this.viewport = document.getElementById("viewport")!;
    this.edgesLayer = document.getElementById("edgesLayer")!;
    this.nodesLayer = document.getElementById("nodesLayer")!;
    this.canvasWrap = document.getElementById("canvasWrap")!;

    document.getElementById("centerMark")!.setAttribute("font-size", String(Math.max(18, ringRadius * 0.09)));

    this.drawPathEdges();
    this.drawWalletJourneyEdges();
    this.drawDexNodes();
    this.drawSwapNode();
    this.drawWalletHubs();
    this.wireBackgroundAndControls();
  }

  // ---- scene construction ----

  // A chosen route no longer draws as one direct chord: it bends through
  // the relevant Wallet hub(s) (see journeyWaypoints) — 2 waypoints = the
  // old direct chord (fallback), 3 = one hub (same-chain rebalancing), 4 =
  // two hubs (a bridge crosses chains). `state`/`e` are shared across every
  // segment of this pathEdge, so highlightForEdgeSelection (which iterates
  // edgeElements keyed on `el.e.from`/`el.e.to`) toggles all of a route's
  // segments together for free.
  private drawPathEdges(): void {
    for (const e of pathEdges) {
      const waypoints = journeyWaypoints(e, dexByName, walletHubByChain);
      const weight = 1 - e.cost / maxCost;

      const journeys = journeysByPair.get(`${e.from}→${e.to}`) || null;
      const dotsStable = journeyStable(journeys);
      const state = { journeys, dotsStable };

      // Un seul label de coût pour toute la route (posé sur le premier segment),
      // pas un par segment — répéter le même montant à chaque coude serait
      // redondant. Partagé (même élément) entre toutes les entrées edgeElements
      // de cette pathEdge.
      const label = document.createElementNS(SVG_NS, "text") as SVGTextElement;
      label.setAttribute("class", "edge-label hidden");
      label.setAttribute("text-anchor", "middle");
      label.textContent = fmt(e.cost);
      this.edgesLayer.appendChild(label);
      let labelPositioned = false;

      for (let i = 0; i < waypoints.length - 1; i++) {
        const a = waypoints[i]!, b = waypoints[i + 1]!;
        const geo = edgeGeometry(positioned(a), positioned(b));

        const pathId = `edge-path-${this.edgeCounter++}`;
        const { path, dots, hitPath, speedFactor } = drawEdgeSegment(this.edgesLayer, geo, pathId, state.dotsStable);
        path.style.strokeWidth = (0.8 + weight * 2.2).toFixed(2);
        path.style.opacity = (0.12 + weight * 0.35).toFixed(2);
        path.dataset.from = e.from;
        path.dataset.to = e.to;

        // Route jamais empruntée par le solveur : entièrement masquée (voir
        // .edge.hidden) plutôt qu'affichée en filigrane par coût — seules
        // les routes réellement choisies restent visibles dans le graphe.
        path.classList.toggle("hidden", !state.journeys);
        if (state.journeys) {
          path.classList.add("chosen");
          path.classList.toggle("approximate", state.journeys.some(j => j.plausible));
          setEdgeStableClass(path, state.dotsStable);
        }

        if (!labelPositioned) {
          label.setAttribute("x", String(geo.mx));
          label.setAttribute("y", String(geo.my));
          labelPositioned = true;
        }

        dots.classList.toggle("hidden", !state.journeys);

        // Hitbox invisible bien plus large que le trait visible (voir
        // .edge-hit) : porte tous les listeners, ajoutée en dernier pour
        // rester au-dessus. Masquée avec l'edge elle-même (route non
        // choisie) : pas de zone cliquable fantôme là où rien n'est dessiné.
        hitPath.classList.add("clickable");
        hitPath.classList.toggle("hidden", !state.journeys);

        this.edgeElements.push({ e, path, label, dots, hitPath, state, pathId, speedFactor });

        hitPath.addEventListener("mouseenter", (ev) => { path.classList.add("hover"); showTooltip(ev, edgeTooltipHtml(e, state.journeys)); });
        hitPath.addEventListener("mousemove", positionTooltip);
        hitPath.addEventListener("mouseleave", () => { path.classList.remove("hover"); hideTooltip(); });
        hitPath.addEventListener("click", (ev) => { ev.stopPropagation(); this.callbacks.onSelectEdge(e, state.journeys); });
      }
    }
  }

  // Trajets qui partent de l'argent DÉJÀ dans un wallet (Journey.fromWallet,
  // voir WalletNode.balance côté Python) : pas d'arête DEX -> DEX à laquelle
  // les rattacher (journeysByPair ne matche que des paires de DEX), donc sans
  // ce passage le graphe resterait vide alors que le header annonce des
  // opérations choisies. Un segment "chosen" par (hub de départ, DEX
  // d'arrivée) -- via le second hub si le trajet bridge -- avec les mêmes
  // badges animés ; le clic ouvre le panel du wallet, où le trajet et son
  // bouton Execute sont listés (voir panels/walletDetails.ts).
  private drawWalletJourneyEdges(): void {
    for (const we of walletJourneyEdges.values()) {
      const hubA = walletHubByChain.get(we.fromChain), hubB = walletHubByChain.get(we.toChain), toNode = dexByName.get(we.to);
      if (!hubA || !toNode) continue;
      const waypoints = hubB && hubB !== hubA ? [hubA, hubB, toNode] : [hubA, toNode];
      const total = we.journeys.reduce((s, j) => s + j.amount, 0);
      const approx = we.journeys.some(j => j.plausible);
      const stable = we.journeys[0]!.stable;
      const e = { from: `Wallet ${we.fromChain}`, to: we.to };
      const cost = we.journeys.reduce((s, j) => s + j.totalCost, 0);
      const tooltip = `<div><b>Wallet ${we.fromChain}</b> → <b>${we.to}</b></div>
        <div style="margin-top:4px;color:var(--chosen)"><b>Chosen by solver:</b> ${fmt(total)} ${stable} from the wallet's own balance${approx ? " (approximate)" : ""}</div>`;

      for (let i = 0; i < waypoints.length - 1; i++) {
        const a = waypoints[i]!, b = waypoints[i + 1]!;
        const geo = edgeGeometry(positioned(a), positioned(b));
        const pathId = `edge-path-${this.edgeCounter++}`;
        const { path, dots, hitPath, speedFactor } = drawEdgeSegment(this.edgesLayer, geo, pathId, stable);
        path.classList.add("chosen");
        path.classList.toggle("approximate", approx);
        setEdgeStableClass(path, stable);
        path.style.strokeWidth = "2.4";
        path.style.opacity = "0.9";
        path.dataset.from = e.from;
        path.dataset.to = e.to;
        hitPath.classList.add("clickable");

        const label = document.createElementNS(SVG_NS, "text") as SVGTextElement;
        label.setAttribute("class", "edge-label hidden");
        label.setAttribute("text-anchor", "middle");
        label.setAttribute("x", String(geo.mx));
        label.setAttribute("y", String(geo.my));
        label.textContent = fmt(cost);
        this.edgesLayer.appendChild(label);

        this.edgeElements.push({ e, path, label, dots, hitPath, state: { journeys: we.journeys, dotsStable: stable }, pathId, speedFactor });

        hitPath.addEventListener("mouseenter", (ev) => { path.classList.add("hover"); showTooltip(ev, tooltip); });
        hitPath.addEventListener("mousemove", positionTooltip);
        hitPath.addEventListener("mouseleave", () => { path.classList.remove("hover"); hideTooltip(); });
        hitPath.addEventListener("click", (ev) => { ev.stopPropagation(); this.callbacks.onSelectWallet(we.fromChain); path.classList.add("highlight"); });
      }
    }
  }

  private drawDexNodes(): void {
    for (const dex of GRAPH_DATA.dexNodes) {
      const g = document.createElementNS(SVG_NS, "g") as SVGGElement;
      g.setAttribute("class", "node");
      g.setAttribute("transform", `translate(${dex.cx}, ${dex.cy})`);
      g.dataset.name = dex.name;

      // Fond = couleur de marque du DEX (teinte dominante extraite de son
      // logo, voir visualization/dex_branding.py) ; l'anneau reste la
      // couleur de solde (surplus/déficit/équilibré) pour ne pas perdre ce
      // signal quand on y superpose l'identité visuelle — inspiré du rendu
      // des icônes L2Beat.
      const brandColor = dex.brandColor || `var(--${sign(dex)})`;
      const circle = document.createElementNS(SVG_NS, "circle");
      circle.setAttribute("r", String(dex.r));
      circle.setAttribute("style", `fill:${brandColor};fill-opacity:${dex.logo ? "0.18" : "0.28"};stroke:var(--${sign(dex)});stroke-width:1.6`);
      g.appendChild(circle);

      if (dex.logo) {
        const clipId = `logo-clip-${dex.id}`;
        const clipPath = document.createElementNS(SVG_NS, "clipPath");
        clipPath.setAttribute("id", clipId);
        const clipCircle = document.createElementNS(SVG_NS, "circle");
        clipCircle.setAttribute("r", String(dex.r! - 1.5));
        clipPath.appendChild(clipCircle);
        g.appendChild(clipPath);

        const logoSize = dex.r! * 1.55;
        const image = document.createElementNS(SVG_NS, "image");
        image.setAttributeNS(XLINK_NS, "xlink:href", dex.logo);
        image.setAttribute("href", dex.logo);
        image.setAttribute("x", String(-logoSize / 2));
        image.setAttribute("y", String(-logoSize / 2));
        image.setAttribute("width", String(logoSize));
        image.setAttribute("height", String(logoSize));
        image.setAttribute("preserveAspectRatio", "xMidYMid slice");
        image.setAttribute("clip-path", `url(#${clipId})`);
        (image as unknown as SVGElement).style.pointerEvents = "none";
        g.appendChild(image);
      } else {
        // Pas de branding pour ce DEX (voir _dexNodeDict côté Python) :
        // repli sur un monogramme plutôt qu'un cercle vide.
        const monogram = document.createElementNS(SVG_NS, "text");
        monogram.setAttribute("class", "node-monogram");
        monogram.setAttribute("y", "4");
        monogram.setAttribute("text-anchor", "middle");
        monogram.textContent = (dex.name.match(/[A-Za-z]/g) || ["?"]).slice(0, 2).join("").toUpperCase();
        g.appendChild(monogram);
      }

      const label = document.createElementNS(SVG_NS, "text");
      label.setAttribute("y", String(-dex.r! - 8));
      label.setAttribute("text-anchor", "middle");
      label.textContent = dex.name;
      g.appendChild(label);

      const sub = document.createElementNS(SVG_NS, "text");
      sub.setAttribute("class", `sublabel ${sign(dex)}`);
      sub.setAttribute("y", String(dex.r! + 16));
      sub.setAttribute("text-anchor", "middle");
      sub.textContent = fmtSigned(dex.inbalance);
      g.appendChild(sub);

      this.nodesLayer.appendChild(g);
      this.nodeElements.set(dex.name, g);

      g.addEventListener("click", (ev) => { ev.stopPropagation(); this.callbacks.onSelectDex(dex.name); });
      g.addEventListener("mouseenter", (ev) => showTooltip(ev, nodeTooltipHtml(dex)));
      g.addEventListener("mousemove", positionTooltip);
      g.addEventListener("mouseleave", hideTooltip);
    }
  }

  // LE node "swap" — même dessin (cercle + logo/monogramme + label) qu'un
  // node DEX ci-dessus, PAS le <rect> des walletHubs : visuellement un
  // venue comme un DEX. Pas d'anneau de solde (aucun concept de
  // surplus/déficit pour un swap venue) : anneau neutre `--border` au lieu
  // de la couleur de signe.
  private drawSwapNode(): void {
    if (!swapNode) return;
    const g = document.createElementNS(SVG_NS, "g") as SVGGElement;
    g.setAttribute("class", "node");
    g.setAttribute("transform", `translate(${swapNode.cx}, ${swapNode.cy})`);
    g.dataset.name = swapNode.name;

    const brandColor = swapNode.brandColor || "var(--border)";
    const circle = document.createElementNS(SVG_NS, "circle");
    circle.setAttribute("r", String(swapNode.r));
    circle.setAttribute("style", `fill:${brandColor};fill-opacity:${swapNode.logo ? "0.18" : "0.28"};stroke:var(--border);stroke-width:1.6`);
    g.appendChild(circle);

    if (swapNode.logo) {
      const clipId = "logo-clip-swap";
      const clipPath = document.createElementNS(SVG_NS, "clipPath");
      clipPath.setAttribute("id", clipId);
      const clipCircle = document.createElementNS(SVG_NS, "circle");
      clipCircle.setAttribute("r", String(swapNode.r! - 1.5));
      clipPath.appendChild(clipCircle);
      g.appendChild(clipPath);

      const logoSize = swapNode.r! * 1.55;
      const image = document.createElementNS(SVG_NS, "image");
      image.setAttributeNS(XLINK_NS, "xlink:href", swapNode.logo);
      image.setAttribute("href", swapNode.logo);
      image.setAttribute("x", String(-logoSize / 2));
      image.setAttribute("y", String(-logoSize / 2));
      image.setAttribute("width", String(logoSize));
      image.setAttribute("height", String(logoSize));
      image.setAttribute("preserveAspectRatio", "xMidYMid slice");
      image.setAttribute("clip-path", `url(#${clipId})`);
      (image as unknown as SVGElement).style.pointerEvents = "none";
      g.appendChild(image);
    } else {
      const monogram = document.createElementNS(SVG_NS, "text");
      monogram.setAttribute("class", "node-monogram");
      monogram.setAttribute("y", "4");
      monogram.setAttribute("text-anchor", "middle");
      monogram.textContent = (swapNode.name.match(/[A-Za-z]/g) || ["?"]).slice(0, 2).join("").toUpperCase();
      g.appendChild(monogram);
    }

    const label = document.createElementNS(SVG_NS, "text");
    label.setAttribute("y", String(-swapNode.r! - 8));
    label.setAttribute("text-anchor", "middle");
    label.textContent = swapNode.name;
    g.appendChild(label);

    const sub = document.createElementNS(SVG_NS, "text");
    sub.setAttribute("class", "sublabel zero");
    sub.setAttribute("y", String(swapNode.r! + 16));
    sub.setAttribute("text-anchor", "middle");
    sub.textContent = `${swapNode.chains.length} chains`;
    g.appendChild(sub);

    this.nodesLayer.appendChild(g);
    this.swapNodeElement = g;

    g.addEventListener("click", (ev) => { ev.stopPropagation(); this.callbacks.onSelectSwap(); });
    g.addEventListener("mouseenter", (ev) => showTooltip(ev, swapNodeTooltipHtml(swapNode!)));
    g.addEventListener("mousemove", positionTooltip);
    g.addEventListener("mouseleave", hideTooltip);

    this.drawSwapStubs();
  }

  // Deux arêtes par Wallet hub, mais SEULEMENT si le solveur a réellement
  // choisi un Swap sur cette chain (edge.flow > 0, voir
  // computeChosenOperations côté Python) — jamais affichées juste parce que
  // CoW Swap existe dans le registre : si aucun swap n'est utilisé dans le
  // plan courant, aucune arête ici, seul le node reste visible (venue
  // disponible mais inactive). Le "venue d'où ça vient" et le "venue où ça
  // ressort" sont le MÊME Wallet hub (voir EdgeType.Swap/Graph._linkSwaps :
  // jamais de changement de chain, contrairement à un Bridge) ; la stable
  // entrante/sortante vient de `testable.stable`/`testable.toStable` (voir
  // web_view.py::_testableHopInfo), pas un sens canonique arbitraire.
  private drawSwapStubs(): void {
    const swap = swapNode!;
    const swapPos = positioned(swap);
    const swapHubs = walletHubs.filter(hub => swap.chains.includes(hub.chain));
    const chosenSwapOps = GRAPH_DATA.operations.filter(op => op.type === "Swap" && op.testable);

    const drawSwapStub = (a: { cx: number; cy: number; r: number }, b: { cx: number; cy: number; r: number }, stable: string) => {
      const geo = edgeGeometry(a, b);
      const pathId = `edge-path-${this.edgeCounter++}`;
      const { path, hitPath } = drawEdgeSegment(this.edgesLayer, geo, pathId, stable);
      path.classList.add("chosen");
      setEdgeStableClass(path, stable);
      path.style.strokeWidth = "2.4";
      path.style.opacity = "0.9";
      // No "clickable" class here (unlike the other two edge-drawing
      // sites): a swap stub is hover-only (tooltip), never itself the click
      // target for a selection.
      return { path, hitPath };
    };

    for (const hub of swapHubs) {
      for (const op of chosenSwapOps.filter(o => o.testable!.chain === hub.chain)) {
        const { stable, toStable } = op.testable!;

        const { path: inPath, hitPath: inHit } = drawSwapStub(hub, swapPos, stable);
        const inTip = `<div><b>Wallet ${hub.chain}</b> → <b>CoW Swap</b></div><div>${stable} in · $${fmt(op.amount)}</div>`;
        inHit.addEventListener("mouseenter", (ev) => { inPath.classList.add("hover"); showTooltip(ev, inTip); });
        inHit.addEventListener("mousemove", positionTooltip);
        inHit.addEventListener("mouseleave", () => { inPath.classList.remove("hover"); hideTooltip(); });

        const { path: outPath, hitPath: outHit } = drawSwapStub(swapPos, hub, toStable!);
        const outTip = `<div><b>CoW Swap</b> → <b>Wallet ${hub.chain}</b></div><div>${toStable} out · $${fmt(op.amount)}</div>`;
        outHit.addEventListener("mouseenter", (ev) => { outPath.classList.add("hover"); showTooltip(ev, outTip); });
        outHit.addEventListener("mousemove", positionTooltip);
        outHit.addEventListener("mouseleave", () => { outPath.classList.remove("hover"); hideTooltip(); });
      }
    }
  }

  // Nœuds "Wallet" — même <g class="node ..."> que les DEX (réutilise
  // .selected/.dimmed/.hidden génériques, voir CSS) mais un <rect> arrondi
  // plutôt qu'un <circle> : jamais confondu avec un DEX au premier coup
  // d'œil, et sans anneau de solde (pas de concept de surplus/déficit pour
  // un wallet). Cliquer dessus affiche le solde ON-CHAIN RÉEL de cette
  // chain quand il y en a un lecteur live (BSC/ARBITRUM) et, pour toutes
  // les chains y compris SOLANA, le panel "Withdraw amount".
  private drawWalletHubs(): void {
    for (const hub of walletHubs) {
      const g = document.createElementNS(SVG_NS, "g") as SVGGElement;
      g.setAttribute("class", "node node-wallet");
      g.setAttribute("transform", `translate(${hub.cx}, ${hub.cy})`);
      g.dataset.name = hub.name;

      const rect = document.createElementNS(SVG_NS, "rect");
      rect.setAttribute("x", String(-hub.r));
      rect.setAttribute("y", String(-hub.r));
      rect.setAttribute("width", String(hub.r * 2));
      rect.setAttribute("height", String(hub.r * 2));
      rect.setAttribute("rx", "8");
      g.appendChild(rect);

      const icon = document.createElementNS(SVG_NS, "text");
      icon.setAttribute("class", "wallet-icon");
      icon.setAttribute("y", "5");
      icon.textContent = WALLET_HUB_ICON[hub.chain] || hub.chain.slice(0, 3);
      g.appendChild(icon);

      const label = document.createElementNS(SVG_NS, "text");
      label.setAttribute("y", String(-hub.r - 8));
      label.setAttribute("text-anchor", "middle");
      label.textContent = `Wallet · ${hub.chain}`;
      g.appendChild(label);

      this.nodesLayer.appendChild(g);
      this.walletHubElements.set(hub.chain, g);

      g.addEventListener("click", (ev) => { ev.stopPropagation(); this.callbacks.onSelectWallet(hub.chain); });
      g.addEventListener("mouseenter", (ev) => showTooltip(ev,
        `<div><b>Wallet · ${hub.chain}</b></div><div>Click for the wallet balance and withdrawal amount</div>`));
      g.addEventListener("mousemove", positionTooltip);
      g.addEventListener("mouseleave", hideTooltip);
    }
  }

  // ---- background click, showCosts checkbox, pan/zoom ----

  private wireBackgroundAndControls(): void {
    this.svg.addEventListener("click", () => this.callbacks.onClear());

    const showCosts = document.getElementById("showCosts") as HTMLInputElement;
    showCosts.addEventListener("change", () => {
      // Un label ne réapparaît jamais pour une route non choisie (voir
      // .edge.hidden) : la case ne fait que révéler les coûts des routes
      // déjà visibles.
      for (const { label, state } of this.edgeElements) label.classList.toggle("hidden", !showCosts.checked || !state.journeys);
    });

    this.canvasWrap.addEventListener("wheel", (ev) => {
      ev.preventDefault();
      const rect = this.canvasWrap.getBoundingClientRect();
      const mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
      const worldX = (mx - this.view.x) / this.view.k, worldY = (my - this.view.y) / this.view.k;
      const factor = Math.exp(-ev.deltaY * 0.001);
      this.view.k = Math.min(4, Math.max(0.15, this.view.k * factor));
      this.view.x = mx - worldX * this.view.k;
      this.view.y = my - worldY * this.view.k;
      this.applyView();
    }, { passive: false });

    let dragging = false;
    let dragStart = { x: 0, y: 0 };
    this.canvasWrap.addEventListener("pointerdown", (ev) => {
      dragging = true;
      dragStart = { x: ev.clientX - this.view.x, y: ev.clientY - this.view.y };
      this.canvasWrap.classList.add("grabbing");
    });
    window.addEventListener("pointermove", (ev) => {
      if (!dragging) return;
      this.view.x = ev.clientX - dragStart.x;
      this.view.y = ev.clientY - dragStart.y;
      this.applyView();
    });
    window.addEventListener("pointerup", () => { dragging = false; this.canvasWrap.classList.remove("grabbing"); });

    document.getElementById("zoomIn")!.addEventListener("click", () => { this.view.k = Math.min(4, this.view.k * 1.25); this.applyView(); });
    document.getElementById("zoomOut")!.addEventListener("click", () => { this.view.k = Math.max(0.15, this.view.k / 1.25); this.applyView(); });
    document.getElementById("zoomFit")!.addEventListener("click", () => this.fitToScreen());
  }

  private applyView(): void {
    this.viewport.setAttribute("transform", `translate(${this.view.x},${this.view.y}) scale(${this.view.k})`);
    document.getElementById("zoomLabel")!.textContent = Math.round(this.view.k * 100) + "%";
  }

  focusOn(cx: number, cy: number): void {
    const rect = this.canvasWrap.getBoundingClientRect();
    this.view.x = rect.width / 2 - cx * this.view.k;
    this.view.y = rect.height / 2 - cy * this.view.k;
    this.applyView();
  }

  fitToScreen(): void {
    if (GRAPH_DATA.dexNodes.length === 0) return;
    const pad = ringRadius * 0.25 + 70;
    // walletHubs included here too (not just dexNodes) — SOLANA in
    // particular sits deliberately outside the DEX ring itself, so a
    // bounding box keyed on dexNodes alone would clip it.
    const xs = [...GRAPH_DATA.dexNodes.map(n => n.cx!), ...walletHubs.map(w => w.cx)];
    const ys = [...GRAPH_DATA.dexNodes.map(n => n.cy!), ...walletHubs.map(w => w.cy)];
    const minX = Math.min(...xs) - pad, maxX = Math.max(...xs) + pad;
    const minY = Math.min(...ys) - pad, maxY = Math.max(...ys) + pad;
    const rect = this.canvasWrap.getBoundingClientRect();
    const k = Math.min(rect.width / (maxX - minX), rect.height / (maxY - minY), 2.2);
    this.view.k = k;
    this.view.x = rect.width / 2 - (minX + maxX) / 2 * k;
    this.view.y = rect.height / 2 - (minY + maxY) / 2 * k;
    this.applyView();
  }

  // ---- selection highlighting (called by state/SelectionController.ts) ----

  clearHighlights(): void {
    document.querySelectorAll(".node.selected").forEach(e => e.classList.remove("selected"));
    document.querySelectorAll(".dimmed").forEach(e => e.classList.remove("dimmed"));
    document.querySelectorAll(".edge.highlight").forEach(e => e.classList.remove("highlight"));
  }

  highlightForDexSelection(name: string): void {
    this.nodeElements.get(name)!.classList.add("selected");
    const connected = new Set([name, ...Object.keys(GRAPH_DATA.paths[name] || {})]);
    for (const [n, el] of this.nodeElements) el.classList.toggle("dimmed", !connected.has(n));
    for (const { e, path, label, dots } of this.edgeElements) {
      const isOutgoing = e.from === name;
      path.classList.toggle("dimmed", !isOutgoing);
      label.classList.toggle("dimmed", !isOutgoing && !label.classList.contains("hidden"));
      if (dots) dots.classList.toggle("dimmed", !isOutgoing);
      if (isOutgoing) path.classList.add("highlight");
    }
  }

  highlightForSwapSelection(): void {
    if (this.swapNodeElement) this.swapNodeElement.classList.add("selected");
  }

  highlightForWalletSelection(chain: string): void {
    const g = this.walletHubElements.get(chain);
    if (g) g.classList.add("selected");
  }

  highlightForEdgeSelection(e: PathEdge): void {
    // Highlights the Wallet hub(s) this specific route actually passes
    // through (see journeyWaypoints) — a waypoint has `.chain` only when
    // it's a wallet hub, never a DEX node, so this filters to hubs alone.
    const hubChains = new Set(
      journeyWaypoints(e, dexByName, walletHubByChain)
        .filter((w): w is WalletHub => "chain" in w)
        .map(w => w.chain)
    );
    for (const [n, el] of this.nodeElements) el.classList.toggle("dimmed", n !== e.from && n !== e.to);
    for (const [chain, el] of this.walletHubElements) el.classList.toggle("dimmed", !hubChains.has(chain));
    for (const { e: other, path, label, dots } of this.edgeElements) {
      const isThis = other.from === e.from && other.to === e.to;
      path.classList.toggle("dimmed", !isThis);
      label.classList.toggle("dimmed", !isThis && !label.classList.contains("hidden"));
      if (dots) dots.classList.toggle("dimmed", !isThis);
      if (isThis) path.classList.add("highlight");
    }
  }
}
