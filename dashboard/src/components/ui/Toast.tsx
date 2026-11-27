// Toasts. Dependency-free by design (§8.3 R11): this mounts AROUND
// RouterProvider in main.tsx, so anything it imports runs before the router
// exists, and anything it throws blanks the app instead of one route.
//
// The no-provider path returns no-ops rather than throwing, for the same reason:
// a screen that fires a toast outside the provider should stay usable and lose
// the toast, not white-screen. Toast actions that need to navigate are the one
// case that wants a root-route provider instead — see R11.
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { cx } from "./index";
import { Icon } from "./icons";

export type ToastTone = "neutral" | "ok" | "warn" | "danger";

export interface ToastAction {
  label: string;
  onClick: () => void;
}

export interface ToastOptions {
  tone?: ToastTone;
  /** Second line, for the detail that explains the headline. */
  description?: ReactNode;
  action?: ToastAction;
  /** ms on screen. 0 keeps it up until dismissed. Errors default to sticky. */
  duration?: number;
}

export interface Toast extends ToastOptions {
  id: string;
  message: ReactNode;
}

export interface ToastApi {
  /** Returns the id so a long-running caller can dismiss its own toast. */
  show: (message: ReactNode, options?: ToastOptions) => string;
  success: (message: ReactNode, options?: ToastOptions) => string;
  error: (message: ReactNode, options?: ToastOptions) => string;
  dismiss: (id: string) => void;
}

const NOOP_ID = "";
const NOOP: ToastApi = {
  show: () => NOOP_ID,
  success: () => NOOP_ID,
  error: () => NOOP_ID,
  dismiss: () => {},
};

const ToastContext = createContext<ToastApi>(NOOP);

/** Never throws when unmounted — see the file header. */
export function useToast(): ToastApi {
  return useContext(ToastContext);
}

const DEFAULT_MS = 5000;
const MAX_VISIBLE = 3;

const TONE_BAR: Record<ToastTone, string> = {
  neutral: "bg-line-strong",
  ok: "bg-ok",
  warn: "bg-warn",
  danger: "bg-danger",
};

const TONE_ICON: Record<ToastTone, ReactNode> = {
  neutral: <Icon.Info size={17} />,
  ok: <Icon.Check size={17} />,
  warn: <Icon.Alert size={17} />,
  danger: <Icon.Alert size={17} />,
};

const TONE_INK: Record<ToastTone, string> = {
  neutral: "text-muted",
  ok: "text-ok",
  warn: "text-warn",
  danger: "text-danger",
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const seq = useRef(0);
  // Timers are cleared on unmount so a dismiss never fires into a dead tree.
  const timers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  const dismiss = useCallback((id: string) => {
    const timer = timers.current.get(id);
    if (timer !== undefined) {
      clearTimeout(timer);
      timers.current.delete(id);
    }
    setToasts((list) => list.filter((t) => t.id !== id));
  }, []);

  const show = useCallback(
    (message: ReactNode, options: ToastOptions = {}) => {
      seq.current += 1;
      const id = `t${seq.current}`;
      const tone = options.tone ?? "neutral";
      // An error the user did not read is an error they will hit again, so
      // danger sticks until dismissed unless the caller says otherwise.
      const duration = options.duration ?? (tone === "danger" ? 0 : DEFAULT_MS);
      const toast: Toast = { ...options, tone, id, message };

      setToasts((list) => {
        const next = [...list, toast];
        // Oldest beyond the cap goes, and its timer with it.
        while (next.length > MAX_VISIBLE) {
          const dropped = next.shift();
          if (dropped) {
            const timer = timers.current.get(dropped.id);
            if (timer !== undefined) {
              clearTimeout(timer);
              timers.current.delete(dropped.id);
            }
          }
        }
        return next;
      });

      if (duration > 0) {
        timers.current.set(
          id,
          setTimeout(() => {
            timers.current.delete(id);
            setToasts((list) => list.filter((t) => t.id !== id));
          }, duration),
        );
      }
      return id;
    },
    [],
  );

  useEffect(() => {
    const pending = timers.current;
    return () => {
      pending.forEach((timer) => clearTimeout(timer));
      pending.clear();
    };
  }, []);

  const api = useMemo<ToastApi>(
    () => ({
      show,
      success: (message, options) => show(message, { ...options, tone: "ok" }),
      error: (message, options) => show(message, { ...options, tone: "danger" }),
      dismiss,
    }),
    [show, dismiss],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      <ToastViewport toasts={toasts} onDismiss={dismiss} />
    </ToastContext.Provider>
  );
}

function ToastViewport({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: string) => void }) {
  if (toasts.length === 0) return null;
  return (
    <div
      // aria-live on the container, not the toast: the region has to exist before
      // the row arrives or a screen reader announces nothing.
      role="region"
      aria-live="polite"
      aria-label="Notifications"
      className="fixed bottom-5 right-5 z-[60] flex flex-col gap-2 w-[min(22rem,calc(100vw-2.5rem))]"
    >
      {toasts.map((t) => {
        const tone = t.tone ?? "neutral";
        return (
          <div
            key={t.id}
            className={cx(
              "flex items-start gap-3 rounded-xl border border-line bg-elevated pl-0 pr-3 py-3 shadow-pop overflow-hidden",
              "animate-[ca-rise_220ms_cubic-bezier(0,0,.2,1)] motion-reduce:animate-none",
            )}
          >
            <span className={cx("self-stretch w-1 shrink-0 rounded-r-full", TONE_BAR[tone])} aria-hidden />
            <span className={cx("shrink-0 mt-0.5", TONE_INK[tone])} aria-hidden>
              {TONE_ICON[tone]}
            </span>
            <div className="min-w-0 flex-1">
              <div className="text-[13px] font-semibold text-ink break-words [overflow-wrap:anywhere]">{t.message}</div>
              {t.description && (
                <div className="text-xs text-muted mt-0.5 break-words [overflow-wrap:anywhere]">{t.description}</div>
              )}
              {t.action && (
                <button
                  type="button"
                  onClick={() => {
                    t.action?.onClick();
                    onDismiss(t.id);
                  }}
                  className="mt-1.5 text-[13px] font-semibold text-brand hover:underline"
                >
                  {t.action.label}
                </button>
              )}
            </div>
            <button
              type="button"
              onClick={() => onDismiss(t.id)}
              aria-label="Dismiss notification"
              className="shrink-0 text-faint hover:text-ink transition-colors"
            >
              <Icon.X size={15} />
            </button>
          </div>
        );
      })}
    </div>
  );
}
