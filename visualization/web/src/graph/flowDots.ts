import { SVG_NS, XLINK_NS } from "./svgNamespace";

// Id du logo <image> défini une seule fois dans <defs> (voir le <svg> plus
// haut, embarqué depuis visualization/assets/) par stable — un badge ne fait
// que <use> cette définition, jamais recopier le data URI base64 (voir la
// remarque juste avant les <defs>).
const STABLE_LOGO_ID: Record<string, string> = { USDC: "stable-logo-usdc", USDT: "stable-logo-usdt" };

// Chapelet de badges "stablecoin" animés le long d'une arête "chosen", du DEX
// source vers le DEX destination (sens naturel du path SVG : M sx,sy ->
// ex,ey) — façon L2Beat, mais avec le vrai logo de la stable transportée
// (voir Journey.stable côté Python et STABLE_LOGO_ID) au lieu d'un point
// neutre. `speedFactor` étire dur/décalage pour les longues arêtes plutôt que
// de faire filer les badges plus vite dessus qu'ailleurs. `stable` peut être
// null (edge pas encore choisie, voir la boucle de création des edges) : pas
// de badge dans ce cas, juste un groupe vide, laissé masqué par .flow-dots.hidden.
export function createFlowDots(pathId: string, speedFactor: number, stable: string | null): SVGGElement {
  const g = document.createElementNS(SVG_NS, "g");
  g.setAttribute("class", "flow-dots");
  const logoId = stable ? STABLE_LOGO_ID[stable] : undefined;
  if (!logoId) return g;

  const count = 3;
  const durSeconds = 2.2 * speedFactor;
  const dur = durSeconds.toFixed(2);
  for (let i = 0; i < count; i++) {
    const badge = document.createElementNS(SVG_NS, "g");
    badge.setAttribute("class", "flow-dot-badge");

    const use = document.createElementNS(SVG_NS, "use");
    use.setAttributeNS(XLINK_NS, "xlink:href", `#${logoId}`);
    use.setAttribute("href", `#${logoId}`);
    badge.appendChild(use);

    const anim = document.createElementNS(SVG_NS, "animateMotion");
    anim.setAttribute("dur", `${dur}s`);
    anim.setAttribute("repeatCount", "indefinite");
    anim.setAttribute("begin", `${(-(i / count) * durSeconds).toFixed(2)}s`);
    const mpath = document.createElementNS(SVG_NS, "mpath");
    mpath.setAttributeNS(XLINK_NS, "xlink:href", `#${pathId}`);
    mpath.setAttribute("href", `#${pathId}`);
    anim.appendChild(mpath);
    badge.appendChild(anim);

    g.appendChild(badge);
  }
  return g;
}
