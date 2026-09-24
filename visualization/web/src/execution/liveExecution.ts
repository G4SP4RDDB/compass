import { fmt } from "../utils/format";
import { getExecutionStatus } from "../api/client";
import type { ExecutionStatus } from "../types/api";
import { hopCtxFromButton, showLiveConfirm, executeResultEl } from "./testHop";
import { updateExecuteAllButton } from "./executeAll";

// "Execute" — exécution LIVE d'un hop CHOISI PAR LE SOLVEUR, au montant que
// le solveur a retenu pour cette arête (pas le montant de test minimal de
// "Test This Edge"). Même route (POST /api/test-hop live) et mêmes trois
// gardes-fous que "Run LIVE" (COMPASS_TEST_ALLOW_LIVE côté serveur, confirm
// "YES", caps $ de executor.run_hop — un montant au-dessus du cap est
// refusé par le serveur et affiché tel quel ici). GET /api/execution-status
// dit d'avance si le live est possible, pour griser le bouton sinon.
export function executeButtonHtml(
  t: { dex: string; chain: string; stable: string; toStable?: string; hopType: string },
  amount: number,
  from?: string,
  to?: string
): string {
  // data-from/data-to: the op's own `from`/`to` labels (see
  // computeChosenOperations's _describe output) — NOT used for the single
  // Execute button itself, only so runAllLive (Execute All) can find which
  // later row depends on this one's output (its `to`) without re-deriving
  // it from anywhere else.
  return `<button class="execute-btn" data-dex="${t.dex}" data-chain="${t.chain}" data-stable="${t.stable}" data-to-stable="${t.toStable || ""}" data-hop-type="${t.hopType}" data-amount="${amount}" data-from="${from || ""}" data-to="${to || ""}" title="Move $${fmt(amount)} for real on ${t.dex} (${t.hopType} on ${t.chain})">Execute</button>`;
}

export function bindExecuteButtons(): void {
  const buttons = [...document.querySelectorAll<HTMLButtonElement>(".execute-btn")].filter(b => !b.dataset.bound);
  buttons.forEach(b => { b.dataset.bound = "1"; });
  getExecutionStatus().then(st => {
    buttons.forEach(btn => {
      if (!st.liveAllowed) {
        btn.disabled = true;
        btn.title = st.offline
          ? "Not connected to the server"
          : "Live execution is disabled on this server (COMPASS_TEST_ALLOW_LIVE not set, or no operating wallet configured)";
        return;
      }
      if (st.maxUsdPerHop !== undefined && parseFloat(btn.dataset.amount!) > st.maxUsdPerHop) {
        btn.title = `$${fmt(parseFloat(btn.dataset.amount!))} exceeds COMPASS_TEST_MAX_USD_PER_HOP=$${st.maxUsdPerHop} — the server will refuse it; raise the cap to execute`;
      }
      btn.addEventListener("click", () => onExecuteClick(btn, st));
    });
    // Re-derived every call (not just when `buttons` has fresh ones) since
    // #opsList's own executable-row count doesn't change just because this
    // particular call came from a different panel (e.g. a DEX Details hop).
    updateExecuteAllButton(st);
  });
}

function onExecuteClick(btn: HTMLButtonElement, st: ExecutionStatus): void {
  const ctx = hopCtxFromButton(btn);
  const amount = parseFloat(btn.dataset.amount!);
  const resultEl = executeResultEl(btn);
  if (!resultEl) return;
  showLiveConfirm(resultEl, { resolvedAmountUsd: amount, walletAddress: st.walletAddress }, ctx);
}
