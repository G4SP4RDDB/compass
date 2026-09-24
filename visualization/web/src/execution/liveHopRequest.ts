import { postTestHopLive } from "../api/client";
import type { TestableHopInfo, TestHopResult, TestHopStageEvent, TestHopStreamEvent } from "../types/api";
import { readNdjsonStream } from "./NdjsonStreamReader";

// Factored out of a duplication found by reading the original closely:
// runLiveHop and runAllLive each independently built the same
// fetch-then-readNdjsonStream-then-collect-final-event sequence (~15 lines,
// copy-pasted). Both now call this. Throws on a network/stream-level
// failure (no response body at all); returns null only if the stream ended
// without ever sending a terminal event — the caller still has to check
// `.ok` on what comes back, same as the original did.
export async function runLiveHopRequest(
  ctx: TestableHopInfo,
  amount: number,
  onStage: (message: string, domain: "onchain" | "exchange" | undefined) => void
): Promise<TestHopResult | null> {
  const res = await postTestHopLive({ ...ctx, amount, live: true, confirm: "YES" });
  if (!res.body) throw new Error(`test-hop failed (${res.status})`);

  let finalEvent: TestHopResult | null = null;
  await readNdjsonStream<TestHopStreamEvent>(res, (event) => {
    if ((event as TestHopStageEvent).type === "stage") {
      const stage = event as TestHopStageEvent;
      onStage(stage.message, stage.domain);
    } else {
      finalEvent = event as TestHopResult;
    }
  });
  return finalEvent;
}

// A live hop just moved real funds — the cached wallet balance is now
// stale, unlike every other UI action (this is the only place that
// actually triggers an on-chain operation). runLiveHop (single hop) and
// runAllLive (batch) each call this exactly once when they're fully done
// (runAllLive after its whole loop, not per-hop) — App.ts wires the
// listener to refresh the wallet-balances cache/sidebar and whichever
// wallet Details panel is currently selected, breaking what would
// otherwise be a execution -> panels import cycle (see the plan's
// "Breaking the cycles").
let handler: (() => void) | null = null;

export function onLiveRunComplete(cb: () => void): void {
  handler = cb;
}

export function notifyLiveRunComplete(): void {
  handler?.();
}
