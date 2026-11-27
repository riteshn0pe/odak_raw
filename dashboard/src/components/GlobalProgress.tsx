// Top-of-viewport request progress bar. Subscribes to the global in-flight
// counter (lib/progress.ts) — the single choke point every non-silent request
// funnels through — and renders a thin brand bar above the app while work is
// happening. Short calls never flash: the bar waits 120ms after the first
// request starts, eases toward 90% while requests are in flight, then snaps to
// 100% and holds for 180ms once they settle. Under prefers-reduced-motion it
// skips the easing loop and shows a static 90% so the affordance survives
// without animating.
import { useEffect, useRef, useState } from "react";
import { inFlightCount, subscribeProgress } from "../lib/progress";

const SHOW_DELAY_MS = 120; // a request that settles inside this never flashes
const HOLD_MS = 180; // time at 100% before the bar hides and resets
const MAX_IN_FLIGHT = 90; // never exceed this while requests are still running

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia
    ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
    : false;
}

export function GlobalProgress() {
  const [count, setCount] = useState(() => inFlightCount());
  const [active, setActive] = useState(false);
  const [value, setValue] = useState(0);
  const [reduced, setReduced] = useState(prefersReducedMotion);
  // Bumped on every 0→>0 rising edge so the easing loop restarts even when the
  // bar never left its 100% hold window (a fresh request mid-hold must re-ease
  // from a low width, NProgress-style, instead of sitting static at full).
  const [easeTick, setEaseTick] = useState(0);

  const prevCount = useRef(0);
  const showTimer = useRef<number | null>(null);
  const hideTimer = useRef<number | null>(null);
  const rafRef = useRef<number | null>(null);

  // Keep the static-bar decision in sync with the OS preference.
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = () => setReduced(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    const unsub = subscribeProgress(setCount);
    // Child routes mount before this shell's own effect runs, so a request
    // already in flight at subscribe time would otherwise never trip the rising
    // edge. Re-read the counter and let the transition effect see it.
    const alreadyInFlight = inFlightCount();
    if (alreadyInFlight > 0) setCount(alreadyInFlight);
    return () => {
      unsub();
      if (showTimer.current !== null) window.clearTimeout(showTimer.current);
      if (hideTimer.current !== null) window.clearTimeout(hideTimer.current);
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
  }, []);

  // Drive the show/hide state machine off the in-flight count.
  useEffect(() => {
    const prev = prevCount.current;
    prevCount.current = count;

    // 0 → >0: a request began. Wait before showing so sub-120ms calls never flash.
    if (prev === 0 && count > 0) {
      if (hideTimer.current !== null) {
        window.clearTimeout(hideTimer.current);
        hideTimer.current = null;
      }
      showTimer.current = window.setTimeout(() => {
        showTimer.current = null;
        if (prevCount.current <= 0) return; // settled within the delay — stay hidden
        setActive(true);
        // A request began while the bar was still in its 100% hold: restart from a
        // low width so the ease-to-90 loop visibly resumes for the new request.
        setValue((v) => (v >= MAX_IN_FLIGHT ? 8 : v));
        setEaseTick((t) => t + 1);
        if (reduced) setValue(MAX_IN_FLIGHT);
      }, SHOW_DELAY_MS);
      return;
    }

    // >0 → 0: everything settled. Snap to 100%, hold, then reset.
    if (prev > 0 && count === 0) {
      if (showTimer.current !== null) {
        window.clearTimeout(showTimer.current);
        showTimer.current = null;
      }
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
      setValue(100);
      hideTimer.current = window.setTimeout(() => {
        hideTimer.current = null;
        setActive(false);
        setValue(0);
      }, HOLD_MS);
    }
  }, [count, reduced]);

  // Ease toward 90 while requests are in flight. motion-reduce: static bar.
  // Keyed on easeTick so a fresh rising edge (even mid-hold) restarts the loop.
  useEffect(() => {
    if (!active || reduced) return;
    const tick = () => {
      setValue((v) => Math.min(MAX_IN_FLIGHT, v + (MAX_IN_FLIGHT - v) * 0.12));
      if (prevCount.current > 0) rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
  }, [active, reduced, easeTick]);

  if (!active) return null;

  return (
    <div className="fixed top-0 left-0 right-0 z-[70] h-[2px] bg-transparent pointer-events-none">
      <div className="h-full bg-brand transition-[width] duration-300 ease-out" style={{ width: `${value}%` }} />
    </div>
  );
}
