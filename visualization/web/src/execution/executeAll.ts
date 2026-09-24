import { fmt } from "../utils/format";
import { getExecutionStatus } from "../api/client";
import type { ExecutionStatus, TestableHopInfo, TestHopResult } from "../types/api";
import { hopCtxFromButton, hopStablesLabel, hopChainLabel, executeResultEl, renderHopTestResult, addRecheckBalanceButton, execModal } from "./testHop";
import { createStageTracker, errorNoteHtml } from "./StageTracker";
import { runLiveHopRequest, notifyLiveRunComplete } from "./liveHopRequest";

// "Execute All" only ever reflects rows in #opsList (the flat Chosen
// Operations list, see panels/operations.ts) — never the per-DEX "Test This
// Edge"/journey hop buttons rendered elsewhere, which are exploratory, not
// the solver's actual chosen plan.
export function updateExecuteAllButton(st: ExecutionStatus): void {
  const btn = document.getElementById("executeAllBtn") as HTMLButtonElement | null;
  if (!btn) return;
  const runnable = [...document.querySelectorAll<HTMLButtonElement>("#opsList .execute-btn")].filter(b => !b.disabled);
  if (!st.liveAllowed || runnable.length === 0) {
    btn.hidden = true;
    return;
  }
  const total = runnable.reduce((sum, b) => sum + parseFloat(b.dataset.amount!), 0);
  btn.hidden = false;
  btn.textContent = `⚡ Perform Rebalancing (${runnable.length} operation${runnable.length === 1 ? "" : "s"} · $${fmt(total)})`;
  if (!btn.dataset.bound) {
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => onExecuteAllClick());
  }
}

// "Execute All" — runs every runnable row in #opsList back-to-back, ONE AT
// A TIME, never concurrently: the operating wallet has no nonce-locking
// (compass_test/chain_ops.py::_build_tx fetches a fresh "pending" nonce per
// call, no lock/queue anywhere in wallet.py), so two on-chain submissions
// in flight at once from this one wallet can collide. Reuses the exact
// same POST /api/test-hop + NDJSON stage stream as a single Execute/Run
// LIVE (execution/liveHopRequest.ts) — the only new logic here is the
// queue, the ordering, and skipping a row whose input depended on an
// earlier row that failed.
const HOP_TYPE_ORDER: Record<string, number> = { Withdraw: 0, Swap: 1, Deposit: 2 };

interface ExecuteAllOp {
  ctx: TestableHopInfo;
  amount: number;
  from: string | undefined;
  to: string | undefined;
  resultEl: HTMLElement | null;
}

function gatherExecuteAllOps(): ExecuteAllOp[] {
  return [...document.querySelectorAll<HTMLButtonElement>("#opsList .execute-btn")]
    .filter(b => !b.disabled)
    .map(btn => ({
      ctx: hopCtxFromButton(btn),
      amount: parseFloat(btn.dataset.amount!),
      from: btn.dataset.from,
      to: btn.dataset.to,
      resultEl: executeResultEl(btn),
    }))
    // Withdraw before Deposit (a journey's Deposit needs the matching
    // Withdraw's funds to have actually landed first) — the flat
    // #opsList itself is sorted by $ amount (see computeChosenOperations),
    // not causal order, so this can't just reuse DOM order.
    .sort((a, b) => (HOP_TYPE_ORDER[a.ctx.hopType] ?? 9) - (HOP_TYPE_ORDER[b.ctx.hopType] ?? 9));
}

async function onExecuteAllClick(): Promise<void> {
  const msgEl = document.getElementById("executeAllMessage")!;
  const st = await getExecutionStatus();
  const ops = gatherExecuteAllOps();
  if (!ops.length) return;

  const total = ops.reduce((s, o) => s + o.amount, 0);
  // The one cap the web path never enforces per-hop: executor._check_caps
  // supports a cumulative `spent_so_far` against MAX_USD_PER_RUN, but
  // hop_runner.run_single_hop (what POST /api/test-hop calls) never
  // populates it, so every individual hop is only ever capped alone. A
  // batch of many small hops could otherwise sail past MAX_USD_PER_RUN one
  // server-approved hop at a time — block it here instead, client-side,
  // before a single request goes out.
  if (st.maxUsdPerRun !== undefined && total > st.maxUsdPerRun) {
    msgEl.innerHTML = `<div class="live-confirm-box">
      <div class="live-warning">⚠ Blocked</div>
      <div class="live-confirm-cap-block">Total $${fmt(total)} across ${ops.length} operations exceeds COMPASS_TEST_MAX_USD_PER_RUN=$${fmt(st.maxUsdPerRun)}. Perform Rebalancing won't start a batch above the run cap — raise it, or execute rows individually below.</div>
      <div class="live-confirm-actions"><button class="cancel-live-btn" id="executeAllDismiss">Dismiss</button></div>
    </div>`;
    document.getElementById("executeAllDismiss")!.addEventListener("click", () => { msgEl.innerHTML = ""; });
    return;
  }

  const rowsHtml = ops.map(o =>
    `<div class="live-confirm-list-row"><span>${o.ctx.hopType} · ${o.ctx.dex} on ${hopChainLabel(o.ctx)}</span><span>$${fmt(o.amount)}</span></div>`
  ).join("");
  msgEl.innerHTML = `<div class="live-confirm-box">
    <div class="live-warning">⚠ This will move real funds across ${ops.length} operations, one at a time — no undo</div>
    <dl>
      <dt>Total</dt><dd>$${fmt(total)}</dd>
      <dt>Wallet</dt><dd style="word-break:break-all">${st.walletAddress || "n/a"}</dd>
    </dl>
    <div class="live-confirm-list">${rowsHtml}</div>
    <input type="text" placeholder="Type YES to confirm" class="live-confirm-input" autocomplete="off">
    <div class="live-confirm-actions">
      <button class="confirm-run-btn" disabled>Confirm &amp; Perform Rebalancing</button>
      <button class="cancel-live-btn">Cancel</button>
    </div>
  </div>`;
  const input = msgEl.querySelector(".live-confirm-input") as HTMLInputElement;
  const confirmBtn = msgEl.querySelector(".confirm-run-btn") as HTMLButtonElement;
  input.addEventListener("input", () => { confirmBtn.disabled = input.value !== "YES"; });
  msgEl.querySelector(".cancel-live-btn")!.addEventListener("click", () => { msgEl.innerHTML = ""; });
  confirmBtn.addEventListener("click", () => { msgEl.innerHTML = ""; runAllLive(ops); });
  input.focus();
}

async function runAllLive(ops: ExecuteAllOp[]): Promise<void> {
  const modalStagesEl = execModal.open(`${ops.length} operations, one at a time`);

  const rows = ops.map(o => {
    const label = `${o.ctx.hopType} · ${o.ctx.dex} on ${hopChainLabel(o.ctx)} (${hopStablesLabel(o.ctx)})`;
    const rowEl = document.createElement("div");
    rowEl.className = "exec-batch-row queued";
    rowEl.innerHTML = `<div class="exec-batch-row-label"><span>${label}</span><span class="exec-batch-amount">$${fmt(o.amount)}</span></div><div class="exec-batch-tracker"></div>`;
    modalStagesEl.appendChild(rowEl);
    return { ...o, label, rowEl, trackerEl: rowEl.querySelector(".exec-batch-tracker") as HTMLElement };
  });

  let okCount = 0, unconfirmedCount = 0, failedCount = 0, skippedCount = 0, moved = 0;
  const failedTo = new Set<string>(); // `to` of every hop that ended in a real "error" — later rows reading from it get skipped

  for (const row of rows) {
    if (row.from && failedTo.has(row.from)) {
      row.rowEl.classList.remove("queued");
      row.rowEl.classList.add("skipped");
      row.rowEl.insertAdjacentHTML("beforeend", `<div class="test-note">Skipped — depends on a failed hop above.</div>`);
      if (row.resultEl) row.resultEl.innerHTML = `<div class="test-note">Skipped — depends on a failed hop above.</div>`;
      skippedCount++;
      continue;
    }

    row.rowEl.classList.remove("queued");
    const tracker = createStageTracker(row.trackerEl, row.ctx.dex);
    tracker.push("Sending the request…");

    let finalEvent: TestHopResult | null = null;
    let networkError: string | null = null;
    try {
      finalEvent = await runLiveHopRequest(row.ctx, row.amount, (message, domain) => tracker.push(message, domain));
    } catch (e) {
      networkError = (e as Error).message;
    }

    if (networkError || !finalEvent || !finalEvent.ok) {
      const msg = networkError || (finalEvent ? finalEvent.error : "the connection ended before a result came back");
      tracker.finish("error");
      row.rowEl.insertAdjacentHTML("beforeend", errorNoteHtml(msg));
      if (row.resultEl) row.resultEl.innerHTML = errorNoteHtml(msg);
      failedCount++;
      if (row.to) failedTo.add(row.to);
      continue;
    }

    const status = finalEvent.executed.status;
    tracker.finish(status);
    if (status === "ok") { okCount++; moved += row.amount; }
    else if (status === "unconfirmed") { unconfirmedCount++; moved += row.amount; }
    else { failedCount++; if (row.to) failedTo.add(row.to); }

    if (row.resultEl) {
      renderHopTestResult(row.resultEl, finalEvent, row.ctx, tracker.html());
      if (status === "unconfirmed") addRecheckBalanceButton(row.resultEl, row.ctx.dex);
    }
  }

  const outcome = failedCount > 0 ? "error" : unconfirmedCount > 0 ? "unconfirmed" : "ok";
  execModal.setTitle(failedCount > 0 ? "Perform Rebalancing — finished with errors" : "Perform Rebalancing — complete");
  modalStagesEl.insertAdjacentHTML("beforeend", `<div class="exec-batch-summary">
    ${okCount} done${unconfirmedCount ? ` · ${unconfirmedCount} unconfirmed` : ""}${failedCount ? ` · ${failedCount} failed` : ""}${skippedCount ? ` · ${skippedCount} skipped` : ""} — $${fmt(moved)} moved
  </div>`);
  execModal.finish(outcome, `Perform Rebalancing (${okCount + unconfirmedCount}/${ops.length} done)`);

  notifyLiveRunComplete();
}
