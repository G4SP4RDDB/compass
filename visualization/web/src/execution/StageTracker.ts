import { escapeHtml } from "../utils/format";

export type ExecOutcome = "ok" | "unconfirmed" | "error";

// Shared outcome -> (css class, icon) mapping for the stage tracker, the
// popup title and the mini pill, so all three always agree: "ok" is the
// only outcome that ever reads as done/green — see execution/testHop.ts's
// runLiveHop, which derives this straight from ExecutedHop.status, never
// from whether the HTTP request itself merely succeeded.
export function execOutcomeClass(outcome: ExecOutcome): string {
  return outcome === "ok" ? "done" : outcome === "unconfirmed" ? "pending" : "error";
}

export function execOutcomeIcon(outcome: ExecOutcome): string {
  return outcome === "ok" ? "✓" : outcome === "unconfirmed" ? "⏳" : "✕";
}

// A LIVE run's terminal failure message, "smoothly" integrated instead of
// dumped as one raw line: a short summary always visible, the full
// text tucked behind <details> so a long exception/traceback doesn't blow
// up the row — the stage tracker right above it already shows the
// step-by-step trail up to the point of failure (see createStageTracker),
// this only cleans up the final message itself. Used by both a single
// Execute/Run LIVE (runLiveHop) and Execute All (runAllLive).
export function errorNoteHtml(message: unknown): string {
  const text = String(message || "unknown error");
  const isLong = text.length > 140 || text.includes("\n");
  if (!isLong) return `<div class="test-note test-status-error"><div class="error-summary">${escapeHtml(text)}</div></div>`;
  const short = text.slice(0, 140).split("\n")[0] + "…";
  return `<div class="test-note test-status-error">
    <div class="error-summary">${escapeHtml(short)}</div>
    <details><summary>Show details</summary><pre>${escapeHtml(text)}</pre></details>
  </div>`;
}

export interface StageTracker {
  push(message: string, domain?: "onchain" | "exchange"): void;
  finish(outcome: ExecOutcome): void;
  html(): string;
}

// Renders the "stage" events above into #exec-stages as they stream in:
// the previously-active stage freezes as done/error (spinner -> ✓/✕) and a
// new spinner starts for the incoming one. `domain` ("onchain" vs
// "exchange", set by executor.py's on_stage) gets its own colored tag —
// "On-chain" or the venue's own name (`venueName`, e.g. "Ondo Perps") — so
// "confirmed on-chain" and "the DEX is now processing it internally" never
// read as the same kind of event.
export function createStageTracker(container: HTMLElement, venueName: string | undefined): StageTracker {
  container.innerHTML = `<div class="exec-stages"></div>`;
  const list = container.querySelector(".exec-stages")!;
  let current: HTMLElement | null = null;
  return {
    push(message, domain) {
      if (current) {
        current.classList.remove("active");
        current.classList.add("done");
        current.querySelector(".exec-stage-icon")!.innerHTML = "✓";
      }
      current = document.createElement("div");
      current.className = "exec-stage active";
      const tagHtml = domain
        ? `<span class="exec-stage-tag ${domain === "onchain" ? "onchain" : "exchange"}">${domain === "onchain" ? "On-chain" : (venueName || "Exchange")}</span>`
        : "";
      current.innerHTML = `<span class="exec-stage-icon"><span class="exec-spinner"></span></span>${tagHtml}<span class="exec-stage-msg">${message}</span>`;
      list.appendChild(current);
    },
    // outcome matches ExecutedHop.status exactly (see runLiveHop), so this
    // never claims "done" (green ✓) for anything short of an
    // actually-observed balance change on the venue. "unconfirmed" (the
    // on-chain leg confirmed but the venue's own balance never moved within
    // the wait window) gets its own amber state, not lumped into either
    // done or error.
    finish(outcome) {
      if (!current) return;
      current.classList.remove("active");
      current.classList.add(execOutcomeClass(outcome));
      current.querySelector(".exec-stage-icon")!.innerHTML = execOutcomeIcon(outcome);
    },
    html() {
      return list.outerHTML;
    },
  };
}
