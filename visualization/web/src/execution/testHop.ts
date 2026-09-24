import { fmt, fmtDuration, estTimeTitle } from "../utils/format";
import { getExecutionStatus, postTestHop, fetchDexBalance } from "../api/client";
import { dexByName } from "../state/derivedGraphData";
import type { TestableHopInfo, TestHopResult } from "../types/api";
import { createStageTracker, errorNoteHtml } from "./StageTracker";
import { ExecModal } from "./ExecModal";
import { runLiveHopRequest, notifyLiveRunComplete } from "./liveHopRequest";

// Shared exec modal instance — a single popup for the whole page, used by
// this module, execution/liveExecution.ts (onExecuteClick -> showLiveConfirm
// below) and execution/executeAll.ts.
export const execModal = new ExecModal();

// The (dex, chain, stable[, toStable], hopType) tuple POST /api/test-hop
// takes, read back off a Test/Execute button's data-* attributes.
export function hopCtxFromButton(btn: HTMLElement): TestableHopInfo {
  const ctx: TestableHopInfo = {
    dex: btn.dataset.dex!,
    chain: btn.dataset.chain!,
    stable: btn.dataset.stable!,
    hopType: btn.dataset.hopType as TestableHopInfo["hopType"],
  };
  if (btn.dataset.toStable) ctx.toStable = btn.dataset.toStable;
  if (btn.dataset.toChain) ctx.toChain = btn.dataset.toChain;
  return ctx;
}

export function hopStablesLabel(ctx: TestableHopInfo): string {
  return ctx.toStable ? `${ctx.stable} → ${ctx.toStable}` : ctx.stable;
}

// Bridge only: the chain crossing ("BSC → ARBITRUM") — same shape as
// hopStablesLabel above, just the other axis (Bridge moves the same stable
// across two chains, Swap moves two stables on the same chain, so exactly
// one of these two helpers ever has something to show beyond the bare
// chain/stable).
export function hopChainLabel(ctx: TestableHopInfo): string {
  return ctx.toChain ? `${ctx.chain} → ${ctx.toChain}` : ctx.chain;
}

// Chosen Operations row: the result div is the row's last child; in a
// details-panel hop it's the .hop-test-result of the same .hop-test box.
// Lives here (not execution/liveExecution.ts, despite only Execute buttons
// using it) so that both liveExecution.ts and executeAll.ts can depend on
// this shared base module without depending on each other — see the plan's
// "Breaking the cycles" for the same pattern applied to panels/forms.
export function executeResultEl(btn: HTMLElement): HTMLElement | null {
  const row = btn.closest(".op-row");
  if (row) return row.querySelector(".op-exec-result .hop-test-result");
  return btn.parentElement!.querySelector(".hop-test-result");
}

// "Test This Edge" — POSTs to /api/test-hop (visualization/server.py),
// which runs compass_test.hop_runner.run_single_hop for real against the
// live DEX API. ALWAYS dry-run first (no funds moved, no confirmation
// needed) — "Run LIVE" only appears after a successful dry run, and only if
// the server itself reports liveAllowed (COMPASS_TEST_ALLOW_LIVE=1 there
// AND an operating wallet configured) — see compass_test/README.md "Safety
// model". Re-binds are guarded (data-bound) since renderEdgeDetails can
// re-run on the same details panel.
//
// Hidden entirely in "prod" mode (server started with --mode prod, see
// GET /api/execution-status's uiMode) — an operator running the solved
// plan only ever needs "Execute" (the solver's own chosen hop), not this
// exploratory probe at an unrelated test amount. Re-applied on every call
// (not just newly-bound buttons) since new "Test This Edge" buttons keep
// getting rendered into the DOM as the user clicks around (each Details
// panel render is a fresh batch) — the alternative, a CSS rule keyed off
// a `<body>` class set once, would need every one of those render sites to
// remember to set it, and forgetting one would only be even more error-prone.
export function bindTestEdgeButtons(): void {
  document.querySelectorAll<HTMLButtonElement>(".test-edge-btn").forEach(btn => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => onTestEdgeClick(btn));
  });
  getExecutionStatus().then(st => {
    if (st.uiMode !== "prod") return;
    document.querySelectorAll<HTMLButtonElement>(".test-edge-btn").forEach(btn => { btn.hidden = true; });
  });
}

async function onTestEdgeClick(btn: HTMLButtonElement): Promise<void> {
  const ctx = hopCtxFromButton(btn);
  // Not nextElementSibling: an "Execute" button may sit between this
  // button and the result box (see testEdgeSectionHtml).
  const resultEl = btn.parentElement!.querySelector(".hop-test-result") as HTMLElement;
  btn.disabled = true;
  const originalLabel = btn.textContent;
  btn.textContent = "Testing…";
  resultEl.innerHTML = `<div class="test-note">Running dry-run test against the real ${ctx.dex} API…</div>`;

  let data: TestHopResult;
  try {
    data = await postTestHop({ ...ctx, live: false });
  } catch (e) {
    resultEl.innerHTML = `<div class="test-note test-status-error">${(e as Error).message}</div>`;
    btn.disabled = false;
    btn.textContent = originalLabel;
    return;
  }

  btn.disabled = false;
  btn.textContent = "Test Again";
  renderHopTestResult(resultEl, data, ctx);
}

export function renderHopTestResult(resultEl: HTMLElement, data: TestHopResult, ctx: TestableHopInfo, stagesHtml?: string): void {
  const p = data.planned, ex = data.executed;
  const hasCostErr = data.costErrorPct !== null && data.costErrorPct !== undefined;
  const costErr = hasCostErr ? `${data.costErrorPct! > 0 ? "+" : ""}${data.costErrorPct!.toFixed(1)}%` : "n/a";
  const costErrClass = !hasCostErr ? "" : data.costErrorPct! > 0 ? "test-err-pos" : data.costErrorPct! < 0 ? "test-err-neg" : "";

  // stagesHtml: the step-by-step trail a LIVE run just streamed through
  // (see runLiveHop / executor.py's on_stage) — kept above the summary
  // rows instead of being thrown away once the final result is in, so the
  // on-chain-vs-exchange sequence stays visible, not just the end state.
  let html = stagesHtml || "";
  html += `
    <div class="test-row"><span class="test-label">Amount tested</span><span>$${data.resolvedAmountUsd.toFixed(2)}${data.usedConfiguredMinimum ? " (config min)" : ""}</span></div>
    <div class="test-row"><span class="test-label">Est. cost / time</span><span title="${estTimeTitle(p)}">$${p.estimatedCostUsd.toFixed(4)} · ${fmtDuration(p.estimatedTimeSeconds)}${p.timeSource === "measured" ? " (measured)" : ""}</span></div>
    <div class="test-row"><span class="test-label">Actual cost / time</span><span>${ex.actualCostUsd === null ? "n/a" : "$" + ex.actualCostUsd.toFixed(4)} · ${fmtDuration(ex.actualTimeSeconds)}</span></div>
    <div class="test-row"><span class="test-label">Cost error</span><span class="${costErrClass}">${costErr}</span></div>
    <div class="test-row"><span class="test-label">Status</span><span class="test-status-${ex.status}">${ex.status}</span></div>`;
  if (ex.notes) html += `<div class="test-note">${ex.notes}</div>`;
  if (!data.live) {
    html += data.liveAllowed
      ? `<button class="run-live-btn">Run LIVE</button>`
      : `<div class="test-note">Live runs are disabled on this server (COMPASS_TEST_ALLOW_LIVE not set, or no operating wallet configured).</div>`;
  }

  resultEl.innerHTML = html;

  const liveBtn = resultEl.querySelector(".run-live-btn");
  if (liveBtn) liveBtn.addEventListener("click", () => showLiveConfirm(resultEl, data, ctx));
}

export function showLiveConfirm(
  resultEl: HTMLElement,
  data: { resolvedAmountUsd: number; walletAddress?: string | null },
  ctx: TestableHopInfo
): void {
  if (resultEl.querySelector(".live-confirm-box")) return; // already open
  const box = document.createElement("div");
  box.className = "live-confirm-box";
  box.innerHTML = `
    <div class="live-warning">⚠ This will move real funds — no undo</div>
    <dl>
      <dt>Hop</dt><dd>${ctx.hopType} · ${ctx.dex} on ${hopChainLabel(ctx)} (${hopStablesLabel(ctx)})</dd>
      <dt>Amount</dt><dd>$${data.resolvedAmountUsd.toFixed(2)}</dd>
      <dt>Wallet</dt><dd style="word-break:break-all">${data.walletAddress || "n/a"}</dd>
    </dl>
    <input type="text" placeholder="Type YES to confirm" class="live-confirm-input" autocomplete="off">
    <div class="live-confirm-actions">
      <button class="confirm-run-btn" disabled>Confirm &amp; Run Live</button>
      <button class="cancel-live-btn">Cancel</button>
    </div>`;
  resultEl.appendChild(box);

  const input = box.querySelector(".live-confirm-input") as HTMLInputElement;
  const confirmBtn = box.querySelector(".confirm-run-btn") as HTMLButtonElement;
  input.addEventListener("input", () => { confirmBtn.disabled = input.value !== "YES"; });
  box.querySelector(".cancel-live-btn")!.addEventListener("click", () => box.remove());
  confirmBtn.addEventListener("click", () => runLiveHop(resultEl, box, data, ctx));
  input.focus();
}

async function runLiveHop(
  resultEl: HTMLElement,
  box: HTMLElement,
  data: { resolvedAmountUsd: number; walletAddress?: string | null },
  ctx: TestableHopInfo
): Promise<void> {
  const confirmBtn = box.querySelector(".confirm-run-btn") as HTMLButtonElement;
  const cancelBtn = box.querySelector(".cancel-live-btn") as HTMLButtonElement;
  confirmBtn.disabled = true;
  cancelBtn.disabled = true;
  confirmBtn.textContent = "Running… this can take a few minutes";
  box.remove();
  resultEl.innerHTML = `<div class="test-note">Running — see the popup for live progress…</div>`;

  // Live only: dry runs stay on the plain postTestHop/renderHopTestResult
  // path (onTestEdgeClick) since they're fast enough end-to-end that
  // staged progress wouldn't show anything. A live run genuinely passes
  // through distinct stages — see executor.py's on_stage / server.py's
  // streamed POST /api/test-hop — so this reads the response as it
  // arrives instead of waiting for the whole thing to finish. Shown in the
  // popup (not just inline) so it stays visible regardless of where on the
  // page the Execute/Run LIVE button was.
  const hopLabel = `${ctx.hopType} · ${ctx.dex} on ${hopChainLabel(ctx)} (${hopStablesLabel(ctx)})`;
  const modalStagesEl = execModal.open(hopLabel);
  const tracker = createStageTracker(modalStagesEl, ctx.dex);
  // Placeholder only for the brief gap before the backend's own first
  // stage arrives (executor.py's on_stage, e.g. "preparing"/"requesting")
  // — kept deliberately generic and short-lived, not naming a "server",
  // since that read as a mystery step of its own rather than a momentary
  // loading state.
  tracker.push("Sending the request…");

  let finalEvent: TestHopResult | null = null;
  try {
    finalEvent = await runLiveHopRequest(ctx, data.resolvedAmountUsd, (message, domain) => tracker.push(message, domain));
  } catch (e) {
    tracker.finish("error");
    execModal.setTitle("Execution failed");
    execModal.appendBody(errorNoteHtml((e as Error).message));
    resultEl.innerHTML = errorNoteHtml((e as Error).message);
    execModal.finish("error", hopLabel);
    return;
  }

  if (!finalEvent || !finalEvent.ok) {
    tracker.finish("error");
    const msg = finalEvent ? finalEvent.error : "the connection ended before a result came back";
    execModal.setTitle("Execution failed");
    execModal.appendBody(errorNoteHtml(msg));
    resultEl.innerHTML = errorNoteHtml(msg);
    execModal.finish("error", hopLabel);
    return;
  }

  // finalEvent.ok only means the HTTP round trip succeeded — it says
  // NOTHING about whether the hop itself did. The real outcome is
  // executed.status, straight from executor.py: "ok" only when the DEX's
  // OWN balance was actually observed to move (see _run_deposit/
  // _run_withdraw's poll loop) — "unconfirmed" means the on-chain leg
  // confirmed but that balance change was never seen within the wait
  // window. Treating "ok: true" alone as success used to show a green
  // "done" popup for an unconfirmed deposit, which is exactly backwards.
  const status = finalEvent.executed.status;
  tracker.finish(status);
  execModal.setTitle(status === "ok" ? "Execution complete" : status === "unconfirmed" ? "Not yet confirmed" : "Execution failed");
  renderHopTestResult(resultEl, finalEvent, ctx, tracker.html());
  if (status === "unconfirmed") addRecheckBalanceButton(resultEl, ctx.dex);
  execModal.finish(status, hopLabel);

  notifyLiveRunComplete();
}

// Shown only for an "unconfirmed" outcome: the on-chain leg is done, but
// executor.py gave up waiting for the DEX's own balance to move within
// POLL_TIMEOUT_SECONDS — that's a wait-window limit, not proof it never
// landed. Re-checks the SAME live balance read the DEX's own Details panel
// uses (GET /api/dex-balances/<name>, see loadDexRealBalance) so the user
// can keep checking back without re-running the whole hop.
export function addRecheckBalanceButton(resultEl: HTMLElement, dexName: string): void {
  const wrap = document.createElement("div");
  wrap.innerHTML = `<button class="exec-recheck-btn" type="button">Check ${dexName}'s balance now</button><div class="exec-recheck-result"></div>`;
  resultEl.appendChild(wrap);
  const btn = wrap.querySelector(".exec-recheck-btn") as HTMLButtonElement;
  const out = wrap.querySelector(".exec-recheck-result") as HTMLElement;
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "Checking…";
    try {
      const data = await fetchDexBalance(dexName);
      const stables = dexByName.get(dexName)?.stables || ["USDT", "USDC"];
      out.textContent = data.balances
        ? `${dexName} balance right now — ${stables.map(s => `${s}: $${fmt(data.balances![s] || 0)}`).join(", ")}`
        : `Couldn't read ${dexName}'s balance (${data.error || "unavailable"}).`;
    } catch (e) {
      out.textContent = `Couldn't read ${dexName}'s balance (${(e as Error).message}).`;
    }
    btn.disabled = false;
    btn.textContent = `Check ${dexName}'s balance now`;
  });
}

