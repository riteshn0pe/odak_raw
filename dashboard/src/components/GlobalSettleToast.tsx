// Global "work finished" confirmation toast. Subscribes to the settle event in
// lib/progress.ts, which fires once when a batch of requests drains back to
// zero. Short batches (<1s) already end in a per-screen toast of their own, so
// this only speaks for slow batches that would otherwise leave the top bar
// hanging with no signal that the work actually completed.
import { useEffect } from "react";
import { subscribeSettle } from "../lib/progress";
import { useToast } from "./ui/Toast";

// Anything under a second settles fast enough that the screen itself shows the
// result; toasting here too would double up with those per-screen toasts.
const MIN_SETTLE_MS = 1000;

export function GlobalSettleToast() {
  const toast = useToast();

  useEffect(() => {
    // The provider's api is referentially stable, so this subscribes once for
    // the life of the shell and unsubscribes on unmount.
    return subscribeSettle(({ hadWrite, elapsedMs }) => {
      if (elapsedMs < MIN_SETTLE_MS) return;
      if (hadWrite) toast.success("Changes saved.");
      else toast.show("Everything is up to date.", { tone: "neutral" });
    });
  }, [toast]);

  return null;
}
