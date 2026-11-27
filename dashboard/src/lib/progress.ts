// Global in-flight counter. Every non-silent request through `request()` in
// lib/api.ts calls beginRequest/endRequest, and GlobalProgress subscribes here
// so the top bar tracks the whole app's activity at the one choke point. The
// settle listeners get one event per drained batch (0 → ... → 0) describing
// whether it wrote and how long it ran, so a slow batch that finished can
// confirm itself instead of leaving the top bar hanging with no signal.
export type ProgressListener = (inFlight: number) => void;
export type SettleInfo = { hadWrite: boolean; elapsedMs: number };
export type SettleListener = (info: SettleInfo) => void;

const listeners = new Set<ProgressListener>();
const settleListeners = new Set<SettleListener>();
let inFlight = 0;

// Describe the current batch for the settle event. Reset when the first request
// of a batch starts, read once when the batch drains back to zero.
let batchStartedAt = 0;
let batchHadWrite = false;

export function beginRequest(write = false): void {
  if (inFlight === 0) {
    batchStartedAt = Date.now();
    batchHadWrite = false;
  }
  if (write) batchHadWrite = true;
  inFlight += 1;
  emit();
}
export function endRequest(): void {
  inFlight = Math.max(0, inFlight - 1);
  emit();
  if (inFlight === 0) {
    emitSettle({ hadWrite: batchHadWrite, elapsedMs: Date.now() - batchStartedAt });
  }
}
export function inFlightCount(): number {
  return inFlight;
}
export function subscribeProgress(fn: ProgressListener): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}
export function subscribeSettle(fn: SettleListener): () => void {
  settleListeners.add(fn);
  return () => { settleListeners.delete(fn); };
}

function emit(): void {
  for (const fn of listeners) fn(inFlight);
}
function emitSettle(info: SettleInfo): void {
  for (const fn of settleListeners) fn(info);
}
