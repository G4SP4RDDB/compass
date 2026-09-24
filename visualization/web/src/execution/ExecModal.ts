import { execOutcomeClass, execOutcomeIcon, type ExecOutcome } from "./StageTracker";

// Popup shown for the whole duration of a LIVE run — the inline stage
// tracker in the Details panel can be scrolled out of view or collapsed by
// the time the run actually finishes (e.g. an Execute button clicked from
// the "Chosen Operations" list further down the page); this puts the same
// live-updating steps front and center regardless.
//
// Closing it does NOT abandon a still-running hop (the underlying fetch
// keeps streaming regardless of whether the popup is on screen) — it
// collapses into the bottom-right "Transaction pending…" pill instead, so
// there's still a way back in to watch it. Once the hop is actually done,
// closing just closes: nothing left to watch. `running` is the one flag
// both paths check, so the popup and the pill never disagree about whether
// something is still in flight.
//
// Kept as a class (unlike most of execution/): 5 DOM refs, a real 3-state
// lifecycle (hidden -> open -> minimized-to-pill -> hidden), and 3
// constructor-bound listeners — enough actual state/lifecycle to justify
// instantiation, unlike the plain-function orchestration modules around it.
export class ExecModal {
  private overlayEl: HTMLElement;
  private titleEl: HTMLElement;
  private bodyEl: HTMLElement;
  private miniEl: HTMLElement;
  private miniTextEl: HTMLElement;
  private running = false;

  constructor() {
    this.overlayEl = document.getElementById("execModalOverlay")!;
    this.titleEl = document.getElementById("execModalTitle")!;
    this.bodyEl = document.getElementById("execModalBody")!;
    this.miniEl = document.getElementById("execMiniIndicator")!;
    this.miniTextEl = document.getElementById("execMiniText")!;

    document.getElementById("execModalClose")!.addEventListener("click", () => this.hide());
    this.overlayEl.addEventListener("click", (ev) => {
      if (ev.target === this.overlayEl) this.hide(); // backdrop only, not the modal itself
    });
    this.miniEl.addEventListener("click", () => {
      this.miniEl.hidden = true;
      this.overlayEl.hidden = false;
    });
  }

  open(hopLabel: string): HTMLElement {
    this.running = true;
    this.titleEl.textContent = "Executing…";
    this.bodyEl.innerHTML = `<div class="exec-hop-label">${hopLabel}</div><div class="exec-modal-stages"></div>`;
    this.miniEl.hidden = true;
    this.overlayEl.hidden = false;
    return this.bodyEl.querySelector(".exec-modal-stages")!;
  }

  setTitle(text: string): void {
    this.titleEl.textContent = text;
  }

  appendBody(html: string): void {
    this.bodyEl.insertAdjacentHTML("beforeend", html);
  }

  // "Close" the popup: minimize to the pending pill if the hop is still
  // running, or dismiss everything if it already finished.
  hide(): void {
    this.overlayEl.hidden = true;
    if (!this.running) {
      this.miniEl.hidden = true;
      return;
    }
    this.miniEl.className = "";
    this.miniTextEl.textContent = "Transaction pending…";
    this.miniEl.hidden = false;
  }

  // Called once a run's terminal outcome is known — updates whichever of
  // the popup/pill is currently the visible one, and flips the pill (if
  // that's what's showing) from a spinner to a final ✓/⏳/✕ so closing it
  // mid-run and walking away still surfaces the real outcome later, not a
  // premature "done".
  finish(outcome: ExecOutcome, hopLabel: string): void {
    this.running = false;
    if (this.miniEl.hidden) return; // popup is open — the caller's own title/result updates cover it
    this.miniEl.className = execOutcomeClass(outcome);
    this.miniTextEl.textContent = `${execOutcomeIcon(outcome)} ${hopLabel} — click to view`;
  }
}
