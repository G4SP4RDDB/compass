import type { DexNode } from "../types/graphData";
import type { PlannedHop } from "../types/api";

export function activity(dex: DexNode): number {
  const withdrawTotal = Object.values(dex.withdrawBalances).reduce((s, v) => s + v, 0);
  return Math.max(Math.abs(dex.inbalance), withdrawTotal);
}

export function sign(dex: DexNode): "pos" | "neg" | "zero" {
  return dex.inbalance > 1e-9 ? "pos" : dex.inbalance < -1e-9 ? "neg" : "zero";
}

export function fmt(x: number | null | undefined): string {
  if (x === null || x === undefined) return "–";
  const abs = Math.abs(x);
  if (abs === 0) return "0.00";
  if (abs >= 1000) return x.toFixed(0);
  if (abs >= 1) return x.toFixed(2);
  // Sub-$1 values (e.g. on-chain transfer gas costs, often a fraction of a
  // cent) : a flat toFixed(2) rounds every single one of them to "0.00",
  // making a real, live-fetched cost indistinguishable from a missing one.
  // Scale decimals to the value's magnitude instead, so the first two
  // significant digits always show (capped at 6 decimals).
  const decimals = Math.min(6, 1 - Math.floor(Math.log10(abs)));
  return x.toFixed(decimals);
}

export function fmtSigned(x: number): string {
  return (x > 0 ? "+" : "") + fmt(x);
}

export function fmtDuration(seconds: number | null | undefined): string {
  const s = Math.round(seconds || 0);
  const m = Math.floor(s / 60);
  return m > 0 ? `${m}m${(s % 60).toString().padStart(2, "0")}s` : `${s}s`;
}

// Une couleur par type d'opération (Withdraw/Deposit/On-chain transfer/
// Bridge/Swap) — voir --type-* dans :root et web_view.py:_hopKind, qui
// calcule `type` côté Python. `typeSlug` sert aussi à teinter le liseré de
// la ligne entière (.hop/.op-row), pas seulement le badge.
export function typeSlug(type: string | null | undefined): string {
  return type ? type.toLowerCase().replace(/[^a-z]+/g, "-") : "";
}

export function typeTag(type: string | null | undefined): string {
  return type ? `<span class="type-tag type-${typeSlug(type)}">${type}</span>` : "";
}

export function escapeHtml(s: unknown): string {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c] as string));
}

// Un temps estimé "(m)" vient de la moyenne mesurée des derniers runs live
// (PlannedHop.timeSource === "measured"), pas de la config saisie à la
// main : le tooltip garde la valeur configurée pour voir de combien le
// frontend du DEX se trompait. Shared by execution/testHop.ts's dry-run
// result and panels/testResults.ts's per-hop table row — the only formatter
// in this file used by both a panel and the execution flow.
export function estTimeTitle(p: PlannedHop): string {
  if (p.timeSource === "measured") {
    const cfg = p.configuredTimeSeconds === null || p.configuredTimeSeconds === undefined ? "n/a" : `${p.configuredTimeSeconds.toFixed(0)}s`;
    return `measured mean of recent live runs (configured value: ${cfg})`;
  }
  return "configured value (no live run measured yet)";
}
