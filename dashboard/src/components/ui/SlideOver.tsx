// Right-docked panel for editing one thing without losing your place in a list
// (§5.2): the actions editor, add-source, manage-channel flows.
//
// Overlay and Escape mechanics match Modal (index.tsx:332-374); the geometry and
// the focus handling do not. A panel that covers a list while leaving that list
// tabbable strands keyboard users in content they cannot see, so this one traps
// Tab inside the panel and hands focus back to whatever opened it on close.
import { useEffect, useId, useRef, type ReactNode } from "react";
import { cx } from "./index";
import { Icon } from "./icons";

// Everything the browser will focus, in DOM order. `[tabindex="-1"]` is excluded
// deliberately: it is programmatically focusable but not part of the tab ring.
const FOCUSABLE = [
  "a[href]",
  "area[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "summary",
  '[contenteditable="true"]',
  '[tabindex]:not([tabindex="-1"])',
].join(",");

function focusables(root: HTMLElement | null): HTMLElement[] {
  if (!root) return [];
  // offsetParent is null for `display: none` subtrees — a collapsed Accordion
  // inside the panel must not swallow the tab cycle.
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) => el.offsetParent !== null || el === document.activeElement,
  );
}

export function SlideOver({ open, onClose, title, description, children, footer, width = 640 }: {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  width?: number;
}) {
  const panel = useRef<HTMLDivElement | null>(null);
  const restoreTo = useRef<HTMLElement | null>(null);
  const titleId = useId();
  const descId = useId();

  // Entry focus + restore. Split from the key handler so it runs once per open,
  // not on every onClose identity change.
  useEffect(() => {
    if (!open) return;
    restoreTo.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const node = panel.current;
    const first = focusables(node)[0];
    // The panel itself carries tabIndex -1 as the fallback, so focus never stays
    // behind on the page that is now covered.
    (first ?? node)?.focus();
    return () => {
      const back = restoreTo.current;
      restoreTo.current = null;
      // The opener may have unmounted with the list behind it; document.body
      // keeping focus is better than focusing a detached node.
      if (back && back.isConnected) back.focus();
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key !== "Tab") return;
      const node = panel.current;
      if (!node) return;
      const items = focusables(node);
      if (items.length === 0) {
        e.preventDefault();
        node.focus();
        return;
      }
      const first = items[0]!;
      const last = items[items.length - 1]!;
      const active = document.activeElement;
      if (!e.shiftKey && (active === last || !node.contains(active))) {
        e.preventDefault();
        first.focus();
      } else if (e.shiftKey && (active === first || !node.contains(active))) {
        e.preventDefault();
        last.focus();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/40" onMouseDown={onClose}>
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={title != null ? titleId : undefined}
        aria-describedby={description != null ? descId : undefined}
        tabIndex={-1}
        style={{ width }}
        // ca-rise rather than a slide: the panel is already a large motion event
        // by virtue of its size, and a 2px settle reads as arrival without the
        // eye chasing 640px of travel (§6.5).
        className={cx(
          "flex h-full w-full max-w-full flex-col border-l border-line bg-surface shadow-pop outline-none",
          "animate-[ca-rise_.16s_ease-out] motion-reduce:animate-none",
        )}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-4 border-b border-line px-6 py-4">
          <div className="min-w-0">
            {title != null && (
              <h2 id={titleId} className="m-0 text-[15px] font-semibold tracking-tight text-ink">
                {title}
              </h2>
            )}
            {description != null && (
              <p id={descId} className="m-0 mt-1 text-[13px] text-muted">
                {description}
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close panel"
            className="-mr-1 shrink-0 rounded-md p-1 text-faint transition-colors hover:bg-canvas hover:text-ink focus:outline-none focus:ring-4 focus:ring-brand-soft"
          >
            <Icon.X size={18} />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">{children}</div>

        {footer && (
          <div className="flex items-center justify-end gap-2 border-t border-line bg-canvas/60 px-6 py-4">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}
