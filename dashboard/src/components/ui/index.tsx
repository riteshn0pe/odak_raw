// Production UI kit. Tailwind v4 utilities over the tokens in styles/tailwind.css.
// Every rebuilt screen composes these instead of raw markup.
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type CSSProperties,
  type HTMLAttributes,
  type InputHTMLAttributes,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  type Ref,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";
import { getTheme, setTheme, toggleTheme } from "../../lib/theme";
import { Icon } from "./icons";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

/* ------------------------------------------------------------------ Button */

type BtnVariant = "primary" | "secondary" | "ghost" | "danger" | "brand";
const BTN: Record<BtnVariant, string> = {
  primary: "bg-primary text-primary-fg hover:opacity-90 shadow-card",
  brand: "bg-brand text-brand-fg hover:bg-brand-hover shadow-card",
  secondary: "bg-surface text-ink border border-line-strong hover:bg-canvas",
  ghost: "text-muted hover:text-ink hover:bg-canvas",
  danger: "text-danger border border-danger/30 hover:bg-danger-soft",
};

const BTN_SIZE = {
  sm: "px-3 py-1.5 text-[13px]",
  md: "px-4 py-2 text-sm",
  lg: "px-5 py-2.5 text-[15px]",
} as const;

export function Button({
  variant = "secondary", size = "md", loading, disabled, className, children, ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: BtnVariant;
  size?: "sm" | "md" | "lg";
  loading?: boolean;
}) {
  return (
    <button
      disabled={disabled || loading}
      className={cx(
        "inline-flex items-center justify-center gap-2 rounded-md font-semibold transition-colors disabled:opacity-45 disabled:pointer-events-none whitespace-nowrap",
        BTN_SIZE[size], BTN[variant], className,
      )}
      {...rest}
    >
      {loading && <Spinner className="w-4 h-4" />}
      {children}
    </button>
  );
}

/* -------------------------------------------------------------------- Card */

export function Card({ className, children, ...rest }: { className?: string; children: ReactNode } & HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cx("bg-surface border border-line rounded-xl shadow-card", className)} {...rest}>
      {children}
    </div>
  );
}

export function CardHeader({ title, description, actions }: { title: ReactNode; description?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 px-5 py-4 border-b border-line">
      <div>
        <h3 className="text-[15px] font-semibold tracking-tight m-0">{title}</h3>
        {description && <p className="text-[13px] text-muted mt-0.5 m-0">{description}</p>}
      </div>
      {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
    </div>
  );
}

export function CardBody({ className, children }: { className?: string; children: ReactNode }) {
  return <div className={cx("p-5", className)}>{children}</div>;
}

/* ------------------------------------------------------------- Form fields */

const INPUT = "w-full rounded-md border border-line-strong bg-surface text-ink px-3 py-2 text-sm placeholder:text-faint outline-none transition-shadow focus:border-brand focus:ring-4 focus:ring-brand-soft";

export function Input({ className, ref, prefix, suffix, invalid, ...rest }: InputHTMLAttributes<HTMLInputElement> & { ref?: Ref<HTMLInputElement>; prefix?: ReactNode; suffix?: ReactNode; invalid?: boolean }) {
  if (prefix != null || suffix != null) {
    return (
      <div className="relative">
        {prefix != null && <span className="absolute left-3 top-1/2 -translate-y-1/2 text-faint pointer-events-none">{prefix}</span>}
        <input
          ref={ref}
          aria-invalid={invalid || undefined}
          className={cx(INPUT, prefix != null && "pl-9", suffix != null && "pr-9", invalid && "border-danger focus:border-danger focus:ring-danger-soft", className)}
          {...rest}
        />
        {suffix != null && <span className="absolute right-3 top-1/2 -translate-y-1/2 text-faint pointer-events-none">{suffix}</span>}
      </div>
    );
  }
  return <input ref={ref} aria-invalid={invalid || undefined} className={cx(INPUT, invalid && "border-danger focus:border-danger focus:ring-danger-soft", className)} {...rest} />;
}

export function Textarea({
  className,
  autoGrow = true,
  resizable,
  mono,
  invalid,
  value,
  ...rest
}: TextareaHTMLAttributes<HTMLTextAreaElement> & {
  autoGrow?: boolean;
  resizable?: boolean;
  mono?: boolean;
  invalid?: boolean;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  // autoGrow and resizable are alternatives: resizable opts back into the old
  // fixed-height resize-y behavior and disables growth. Beyond the 320px cap the
  // grown box scrolls (overflow-y-auto) instead of clipping content silently.
  const effectiveAutoGrow = autoGrow && !resizable;
  useEffect(() => {
    const el = ref.current;
    if (!el || !effectiveAutoGrow) return;
    const grow = () => {
      el.style.height = "auto";
      el.style.height = `${Math.min(el.scrollHeight, 320)}px`;
    };
    grow();
    el.addEventListener("input", grow);
    return () => el.removeEventListener("input", grow);
  }, [effectiveAutoGrow, value]);
  return (
    <textarea
      ref={ref}
      value={value}
      aria-invalid={invalid || undefined}
      className={cx(
        INPUT,
        "min-h-24 text-sm leading-relaxed",
        mono ? "font-mono text-[13px]" : "font-sans",
        effectiveAutoGrow ? "resize-none overflow-y-auto" : "",
        resizable ? "resize-y overflow-auto" : "",
        invalid && "border-danger focus:border-danger focus:ring-danger-soft",
        className,
      )}
      {...rest}
    />
  );
}

export function Select({ className, children, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  // Visible chevron affordance baked in as a background SVG (inherits token colour).
  const chevron =
    "bg-[url(\"data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='%2394a3b8' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='m6 9 6 6 6-6'/></svg>\")] bg-no-repeat bg-[right_0.7rem_center]";
  return (
    <select className={cx(INPUT, "appearance-none pr-9 cursor-pointer", chevron, className)} {...rest}>
      {children}
    </select>
  );
}

export function Field({ label, hint, htmlFor, id, children }: { label?: ReactNode; hint?: ReactNode; htmlFor?: string; id?: string; children: ReactNode }) {
  return (
    <label htmlFor={htmlFor} className="block">
      {label && <span id={id} className="block text-[13px] font-medium text-ink mb-1.5">{label}</span>}
      {children}
      {hint && <span className="block text-xs text-faint mt-1">{hint}</span>}
    </label>
  );
}

/* ------------------------------------------------------------------- Badge */

type Tone = "neutral" | "ok" | "warn" | "danger" | "brand" | "accent";
const BADGE: Record<Tone, string> = {
  neutral: "bg-canvas text-muted border border-line",
  ok: "bg-ok-soft text-ok",
  warn: "bg-warn-soft text-warn",
  danger: "bg-danger-soft text-danger",
  brand: "bg-brand-soft text-brand",
  accent: "bg-accent-soft text-accent",
};
export function Badge({ tone = "neutral", dot, children }: { tone?: Tone; dot?: boolean; children: ReactNode }) {
  return (
    <span className={cx("inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-semibold", BADGE[tone])}>
      {dot && <span className="w-1.5 h-1.5 rounded-full bg-current opacity-80" />}
      {children}
    </span>
  );
}

/* -------------------------------------------------------------- PageHeader */

export function PageHeader({ title, description, actions }: { title: ReactNode; description?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4 mb-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight m-0">{title}</h1>
        {/* text-sm is explicit: without it this inherits the page's 16px base and
            the header reads as two headlines instead of a headline and its
            subtitle (§6.3). */}
        {description && <p className="text-sm text-muted mt-1.5 m-0 max-w-2xl">{description}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

/* -------------------------------------------------------------- EmptyState */

// `tone` colours the icon plate and `footer` adds a line below the action — the
// "or do this other thing instead" escape hatch, which is not a second button.
// Both are optional and both default to exactly the previous rendering, so every
// existing call site is unaffected (§8.3 R13).
export type EmptyTone = "neutral" | "brand" | "ok" | "identity";

const EMPTY_PLATE: Record<EmptyTone, string> = {
  neutral: "bg-canvas border-line text-faint",
  brand: "bg-brand-soft border-brand/20 text-brand",
  ok: "bg-ok-soft border-ok/20 text-ok",
  // Inherits the agent's accent from the nearest [data-agent-identity] ancestor.
  // With no such ancestor the vars are unset and this falls back to the plain
  // plate, so it is safe to use anywhere.
  identity: "bg-[var(--agent-tint,var(--color-canvas))] border-[var(--agent-hairline,var(--color-line))] text-[var(--agent-ink,var(--color-faint))]",
};

export function EmptyState({ icon, title, description, action, tone = "neutral", footer }: { icon?: ReactNode; title: ReactNode; description?: ReactNode; action?: ReactNode; tone?: EmptyTone; footer?: ReactNode }) {
  return (
    <div className="flex flex-col items-center text-center py-16 px-6">
      {icon && <div className={cx("w-12 h-12 rounded-xl border grid place-items-center mb-4", EMPTY_PLATE[tone])}>{icon}</div>}
      <div className="font-semibold text-ink">{title}</div>
      {description && <p className="text-muted text-sm mt-1 max-w-sm">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
      {footer && <div className="mt-3 text-xs text-faint max-w-sm">{footer}</div>}
    </div>
  );
}

/* ---------------------------------------------------------------- StatCard */

export function StatCard({ label, value, sub, tone }: { label: ReactNode; value: ReactNode; sub?: ReactNode; tone?: Tone }) {
  return (
    <Card className="p-5">
      <div className="text-[11px] uppercase tracking-wide font-semibold text-faint">{label}</div>
      <div className="text-3xl font-bold tracking-tight mt-1.5 leading-none">{value}</div>
      {sub && <div className={cx("text-xs mt-2", tone === "ok" ? "text-ok" : tone === "danger" ? "text-danger" : "text-muted")}>{sub}</div>}
    </Card>
  );
}

/* -------------------------------------------------------- Toggle / Switch */

export function Toggle({ checked, onChange, label, disabled }: { checked: boolean; onChange: (v: boolean) => void; label?: ReactNode; disabled?: boolean }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cx("inline-flex items-center gap-2.5 text-sm disabled:opacity-45 disabled:pointer-events-none", "cursor-pointer")}
    >
      <span className={cx("relative w-9 h-5 rounded-full transition-colors", checked ? "bg-ok" : "bg-line-strong")}>
        <span className={cx("absolute top-0.5 left-0.5 w-4 h-4 rounded-full bg-white shadow transition-transform", checked && "translate-x-4")} />
      </span>
      {label && <span className="text-ink">{label}</span>}
    </button>
  );
}
// Alias — the reference calls it a Switch.
export const Switch = Toggle;

/* ----------------------------------------------------------------- Spinner */

export function Spinner({ className }: { className?: string }) {
  return <span className={cx("inline-block w-4 h-4 rounded-full border-2 border-current border-t-transparent animate-spin", className)} />;
}

/* ------------------------------------------------------------------- Alert */

export function Alert({ tone = "danger", children }: { tone?: Tone; children: ReactNode }) {
  return <div className={cx("rounded-md px-3.5 py-2.5 text-sm border", BADGE[tone])}>{children}</div>;
}

/* -------------------------------------------------------------------- Tabs */

export interface TabItem { value: string; label: ReactNode; icon?: ReactNode }
export function Tabs({ items, value, onChange, className }: {
  items: TabItem[];
  value: string;
  onChange: (v: string) => void;
  className?: string;
}) {
  const refs = useRef<Array<HTMLButtonElement | null>>([]);

  // Arrow / Home / End keys move focus and activate the neighbouring tab — the
  // content swaps as you navigate, no separate activation key. Only the active
  // tab is in the tab order (roving tabindex), the tablist pattern.
  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    const index = items.findIndex((t) => t.value === value);
    if (index < 0) return;
    let next = -1;
    if (e.key === "ArrowRight") next = (index + 1) % items.length;
    else if (e.key === "ArrowLeft") next = (index - 1 + items.length) % items.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = items.length - 1;
    if (next < 0) return;
    e.preventDefault();
    const target = items[next];
    if (!target) return;
    onChange(target.value);
    refs.current[next]?.focus();
  };

  return (
    <div
      role="tablist"
      aria-orientation="horizontal"
      onKeyDown={onKeyDown}
      className={cx(
        "flex flex-wrap items-center gap-1 rounded-lg bg-canvas border border-line p-1",
        className,
      )}
    >
      {items.map((t, i) => {
        const on = t.value === value;
        return (
          <button
            key={t.value}
            ref={(el) => { refs.current[i] = el; }}
            role="tab"
            aria-selected={on}
            tabIndex={on ? 0 : -1}
            onClick={() => onChange(t.value)}
            className={cx(
              "inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
              "focus:outline-none focus-visible:ring-4 focus-visible:ring-brand-soft",
              on ? "bg-surface text-ink shadow-card" : "text-muted hover:text-ink hover:bg-surface/60",
            )}
          >
            {t.icon && <span className="shrink-0">{t.icon}</span>}
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

/* -------------------------------------------------------- SegmentedControl */

export interface SegmentItem { value: string; label: ReactNode; icon?: ReactNode }
export function SegmentedControl({ items, value, onChange, size = "md", className }: {
  items: SegmentItem[];
  value: string;
  onChange: (v: string) => void;
  size?: "sm" | "md";
  className?: string;
}) {
  return (
    <div className={cx("inline-flex items-center gap-1 rounded-lg bg-canvas border border-line p-1", className)} role="tablist">
      {items.map((s) => {
        const on = s.value === value;
        return (
          <button
            key={s.value}
            role="tab"
            aria-selected={on}
            onClick={() => onChange(s.value)}
            className={cx(
              "inline-flex items-center gap-1.5 rounded-md font-medium transition-colors",
              size === "sm" ? "px-2.5 py-1 text-[13px]" : "px-3 py-1.5 text-sm",
              on ? "bg-surface text-ink shadow-card" : "text-muted hover:text-ink",
            )}
          >
            {s.icon && <span className="shrink-0">{s.icon}</span>}
            {s.label}
          </button>
        );
      })}
    </div>
  );
}

/* --------------------------------------------------- Accordion / Collapsible */

export function Accordion({ title, subtitle, children, defaultOpen, className }: {
  title: ReactNode;
  subtitle?: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
  className?: string;
}) {
  const [open, setOpen] = useState(!!defaultOpen);
  return (
    <div className={cx("border border-line rounded-lg bg-surface overflow-hidden", className)}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex items-center justify-between gap-3 w-full px-4 py-3 text-left hover:bg-canvas transition-colors"
      >
        <span>
          <span className="block text-sm font-semibold text-ink">{title}</span>
          {subtitle && <span className="block text-xs text-muted mt-0.5">{subtitle}</span>}
        </span>
        <span className={cx("shrink-0 text-faint transition-transform", open && "rotate-180")}>
          <Icon.Chevron size={18} />
        </span>
      </button>
      {open && <div className="px-4 pb-4 pt-1 border-t border-line">{children}</div>}
    </div>
  );
}

/* ------------------------------------------------------------ Modal / Dialog */

export function Modal({ open, onClose, title, description, children, footer, size = "md" }: {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  size?: "sm" | "md" | "lg" | "xl";
}) {
  const dialogRef = useRef<HTMLDivElement>(null);
  // `onClose` is often an inline closure with a fresh identity every render. If
  // it were a dependency, the focus effect would tear down and re-run on every
  // parent re-render, restoring focus out of whatever the tenant is typing in —
  // dropping keystrokes from modal inputs. Hold the latest handler in a ref so
  // the effect only tracks `open`.
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    dialogRef.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onCloseRef.current(); };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      prev?.focus?.();
    };
  }, [open]);

  if (!open) return null;
  const width = size === "sm" ? "max-w-sm" : size === "lg" ? "max-w-2xl" : size === "xl" ? "max-w-4xl" : "max-w-md";
  return (
    <div className="fixed inset-0 z-50 bg-black/50 grid place-items-center p-4" onClick={onClose}>
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        tabIndex={-1}
        className={cx("w-full bg-surface border border-line rounded-xl shadow-pop outline-none", width)}
        onClick={(e) => e.stopPropagation()}
      >
        {(title || description) && (
          <div className="flex items-start justify-between gap-4 px-6 pt-5 pb-3">
            <div className="min-w-0">
              {title && <h3 className="text-[15px] font-semibold tracking-tight m-0">{title}</h3>}
              {description && <p className="text-sm text-muted mt-1 m-0">{description}</p>}
            </div>
            <button onClick={onClose} aria-label="Close" className="shrink-0 text-faint hover:text-ink transition-colors">
              <Icon.X size={18} />
            </button>
          </div>
        )}
        {children != null && <div className="px-6 pb-2 pt-1 max-h-[calc(100vh-6rem)] overflow-y-auto">{children}</div>}
        {footer && <div className="flex items-center justify-end gap-2 px-6 py-4">{footer}</div>}
      </div>
    </div>
  );
}

/* ----------------------------------------------------------------- Tooltip */

export function Tooltip({ label, children, className }: { label: ReactNode; children: ReactNode; className?: string }) {
  return (
    <span className={cx("relative inline-flex items-center group", className)}>
      {children}
      <span
        role="tooltip"
        className="pointer-events-none absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 max-w-[18rem] whitespace-normal leading-snug rounded-md bg-ink text-surface text-xs font-medium px-2 py-1 opacity-0 group-hover:opacity-100 transition-opacity z-50 shadow-pop"
      >
        {label}
      </span>
    </span>
  );
}

/* ---------------------------------------------------------------- Skeleton */

export function Skeleton({ className }: { className?: string }) {
  return (
    <span
      // motion-reduce:bg-line: with the shimmer parked, the block carries the
      // whole "something is loading here" signal, so it stops being translucent.
      className={cx("relative block overflow-hidden rounded-md bg-line/70 motion-reduce:bg-line", className)}
      aria-hidden
    >
      <span
        // The animation is a utility, not an inline style, because an inline
        // `animation` cannot be turned off by motion-reduce: — it wins on
        // specificity and the shimmer keeps running (§6.5). Parked at
        // -translate-x-full it sits outside the clip, leaving a flat block.
        className="absolute inset-0 -translate-x-full animate-[ca-shimmer_1.4s_infinite] motion-reduce:animate-none"
        style={{
          background: "linear-gradient(90deg, transparent, rgba(255,255,255,.45), transparent)",
        }}
      />
    </span>
  );
}

// Full-page loading fallback for lazy routes.
export function PageSkeleton() {
  return (
    <div className="p-8 max-w-[1200px] mx-auto w-full">
      <Skeleton className="h-8 w-56 mb-3" />
      <Skeleton className="h-4 w-80 mb-8" />
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4 mb-6">
        <Skeleton className="h-24" />
        <Skeleton className="h-24" />
        <Skeleton className="h-24" />
      </div>
      <Skeleton className="h-64 w-full" />
    </div>
  );
}

/* ------------------------------------------------------------------ Avatar */

export function Avatar({ name, src, size = 32, className }: { name?: string; src?: string; size?: number; className?: string }) {
  const initial = (name ?? "?").trim().slice(0, 1).toUpperCase() || "?";
  return (
    <span
      className={cx("inline-grid place-items-center rounded-full bg-brand-soft text-brand font-bold shrink-0 overflow-hidden", className)}
      style={{ width: size, height: size, fontSize: Math.round(size * 0.42) }}
    >
      {src ? <img src={src} alt={name ?? ""} className="w-full h-full object-cover" /> : initial}
    </span>
  );
}

/* ----------------------------------------------------------------- Divider */

export function Divider({ label, className }: { label?: ReactNode; className?: string }) {
  if (label) {
    return (
      <div className={cx("flex items-center gap-3 text-xs text-faint my-4", className)}>
        <span className="flex-1 h-px bg-line" />
        {label}
        <span className="flex-1 h-px bg-line" />
      </div>
    );
  }
  return <hr className={cx("border-0 border-t border-line my-4", className)} />;
}

/* ------------------------------------------------------------------ Banner */

type BannerTone = "brand" | "warn" | "ok" | "neutral";
const BANNER: Record<BannerTone, string> = {
  brand: "bg-brand text-brand-fg",
  warn: "bg-warn-soft text-warn border-b border-warn-border",
  ok: "bg-ok-soft text-ok",
  neutral: "bg-canvas text-muted border-b border-line",
};
export function Banner({ tone = "brand", icon, children, action, onDismiss, className }: {
  tone?: BannerTone;
  icon?: ReactNode;
  children: ReactNode;
  action?: ReactNode;
  onDismiss?: () => void;
  className?: string;
}) {
  return (
    <div className={cx("flex items-center justify-center gap-3 px-4 py-2 text-sm font-medium", BANNER[tone], className)}>
      {icon && <span className="shrink-0">{icon}</span>}
      <span className="text-center">{children}</span>
      {action && <span className="shrink-0">{action}</span>}
      {onDismiss && (
        <button onClick={onDismiss} aria-label="Dismiss" className="shrink-0 ml-1 opacity-80 hover:opacity-100 transition-opacity">
          <Icon.X size={16} />
        </button>
      )}
    </div>
  );
}

/* -------------------------------------------------------------- ThemeToggle */

export function ThemeToggle({ compact }: { compact?: boolean }) {
  const [dark, setDark] = useState(getTheme() === "dark");
  return (
    <button
      onClick={() => setDark(toggleTheme() === "dark")}
      title={dark ? "Switch to light" : "Switch to dark"}
      aria-label="Toggle theme"
      className={cx(
        "inline-flex items-center gap-2 rounded-md text-muted hover:text-ink hover:bg-canvas transition-colors",
        compact ? "p-2" : "px-2.5 py-1.5 text-sm w-full",
      )}
    >
      {dark ? <Icon.Sun size={16} /> : <Icon.Moon size={16} />}
      {!compact && (dark ? "Light mode" : "Dark mode")}
    </button>
  );
}

// Segmented ☀ Light | 🌙 Dark switch matching the reference theme control.
export function ThemeSwitch({ className }: { className?: string }) {
  const [theme, setThemeState] = useState(getTheme());
  return (
    <SegmentedControl
      className={className}
      size="sm"
      value={theme}
      onChange={(v) => { setTheme(v as "light" | "dark"); setThemeState(v as "light" | "dark"); }}
      items={[
        { value: "light", label: "Light", icon: <Icon.Sun size={14} /> },
        { value: "dark", label: "Dark", icon: <Icon.Moon size={14} /> },
      ]}
    />
  );
}

/* ================================================================= Wave 1 ===
   §5.1 additions. Appended, never interleaved: this module is imported by every
   screen, so a syntax error in it is a blank app rather than a broken page.

   The imports below are a second import block rather than edits to the header
   for the same reason — ESM hoists them identically. */

import { useId } from "react";
import { Link } from "react-router-dom";
import { IDENTITY_SWATCHES } from "../../lib/agentIdentity";

/* ---------------------------------------------------------------- ToggleRow */

// Graduated from AgentConfig.tsx's local `check()` helper: a bordered
// label-left / switch-right row with an optional hint. The switch markup is
// repeated from Toggle instead of composing it because the label lives on the
// other side of the row and has to be wired with aria-labelledby.
export function ToggleRow({ label, hint, checked, onChange, disabled, tooltip }: {
  label: ReactNode;
  hint?: ReactNode;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  tooltip?: ReactNode;
}) {
  const labelId = useId();
  return (
    <div className={cx("flex items-center justify-between gap-4 rounded-lg border border-line bg-canvas px-4 py-3", disabled && "opacity-60")}>
      <span className="min-w-0">
        <span id={labelId} className="flex items-center gap-1.5 text-sm font-medium text-ink">
          {label}
          {tooltip != null && (
            <Tooltip label={tooltip}>
              <span className="text-faint"><Icon.Info size={14} /></span>
            </Tooltip>
          )}
        </span>
        {hint != null && <span className="mt-0.5 block text-[13px] text-muted">{hint}</span>}
      </span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-labelledby={labelId}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className="shrink-0 rounded-full focus:outline-none focus-visible:ring-4 focus-visible:ring-brand-soft disabled:pointer-events-none"
      >
        <span className={cx("relative block h-5 w-9 rounded-full transition-colors", checked ? "bg-ok" : "bg-line-strong")}>
          <span className={cx("absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-white shadow transition-transform motion-reduce:transition-none", checked && "translate-x-4")} />
        </span>
      </button>
    </div>
  );
}

/* ------------------------------------------------------------- ProgressBar */

const PROGRESS_TONE: Record<"brand" | "ok" | "warn" | "danger", string> = {
  brand: "bg-brand",
  ok: "bg-ok",
  warn: "bg-warn",
  danger: "bg-danger",
};

export function ProgressBar({ value, max = 100, tone = "brand", label, sub, indeterminate, size = "md" }: {
  value: number;
  max?: number;
  tone?: "brand" | "ok" | "warn" | "danger";
  label?: ReactNode;
  /** The honest detail line — "reading page 118 of 120". */
  sub?: ReactNode;
  indeterminate?: boolean;
  size?: "sm" | "md";
}) {
  const span = max > 0 ? max : 100;
  const raw = (value / span) * 100;
  const pct = Math.min(Math.max(Number.isFinite(raw) ? raw : 0, 0), 100);
  return (
    <div>
      {(label != null || sub != null) && (
        <div className="mb-1.5 flex items-baseline justify-between gap-3">
          {label != null && <span className="text-[13px] font-medium text-ink">{label}</span>}
          {sub != null && <span className="text-xs text-muted">{sub}</span>}
        </div>
      )}
      <div
        role="progressbar"
        aria-valuemin={indeterminate ? undefined : 0}
        aria-valuemax={indeterminate ? undefined : 100}
        aria-valuenow={indeterminate ? undefined : Math.round(pct)}
        className={cx("relative w-full overflow-hidden rounded-full bg-line", size === "sm" ? "h-1.5" : "h-2.5")}
      >
        {indeterminate ? (
          // ca-rail carries a 40%-wide segment fully off both ends (tailwind.css:111).
          // With motion parked that segment would sit outside the clip and the rail
          // would read as empty, so reduced motion gets a dimmed full rail instead.
          <span
            className={cx(
              "absolute inset-y-0 left-0 w-[40%] rounded-full",
              PROGRESS_TONE[tone],
              "animate-[ca-rail_1.6s_ease-in-out_infinite]",
              "motion-reduce:w-full motion-reduce:animate-none motion-reduce:opacity-60",
            )}
          />
        ) : (
          <span
            className={cx("block h-full rounded-full transition-[width] duration-300 motion-reduce:transition-none", PROGRESS_TONE[tone])}
            style={{ width: `${pct}%` }}
          />
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ Slider */

// Native range input on purpose: `appearance-none` would need per-engine thumb
// CSS we would then own forever, and `accent-color` already tints the native
// thumb and track in both themes.
export function Slider({ value, onChange, min = 0, max = 1, step = 0.05, minLabel, maxLabel, valueLabel, disabled }: {
  value: number;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
  minLabel?: ReactNode;
  maxLabel?: ReactNode;
  valueLabel?: ReactNode;
  disabled?: boolean;
}) {
  const id = useId();
  const ends = minLabel != null || maxLabel != null;
  const describedBy = [ends ? `${id}-ends` : null, valueLabel != null ? `${id}-value` : null].filter(Boolean).join(" ");
  return (
    <div className={cx(disabled && "opacity-60")}>
      <input
        type="range"
        value={value}
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        aria-describedby={describedBy || undefined}
        aria-valuetext={typeof valueLabel === "string" ? valueLabel : undefined}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ accentColor: "var(--color-brand)" }}
        className="w-full cursor-pointer rounded-full outline-none focus-visible:ring-4 focus-visible:ring-brand-soft disabled:cursor-not-allowed"
      />
      {ends && (
        <div id={`${id}-ends`} className="mt-1 flex items-center justify-between gap-3 text-xs text-faint">
          <span>{minLabel}</span>
          <span>{maxLabel}</span>
        </div>
      )}
      {valueLabel != null && (
        <div id={`${id}-value`} className="mt-1.5 text-[13px] text-muted">{valueLabel}</div>
      )}
    </div>
  );
}

/* --------------------------------------------------------------- clipboard */

// Shared by CopyField and CodeBlock. navigator.clipboard is secure-context only,
// so the offscreen-textarea path is the difference between copy working and
// silently doing nothing when the dashboard is served over plain http.
function useCopy(): [boolean, (text: string) => void] {
  const [done, setDone] = useState(false);
  useEffect(() => {
    if (!done) return;
    const t = window.setTimeout(() => setDone(false), 1500);
    return () => window.clearTimeout(t);
  }, [done]);

  const legacy = (text: string) => {
    const el = document.createElement("textarea");
    el.value = text;
    el.setAttribute("readonly", "");
    el.style.position = "fixed";
    el.style.top = "-1000px";
    el.style.opacity = "0";
    document.body.appendChild(el);
    el.select();
    try {
      document.execCommand("copy");
      setDone(true);
    } finally {
      el.remove();
    }
  };

  const copy = (text: string) => {
    try {
      void navigator.clipboard.writeText(text).then(() => setDone(true)).catch(() => legacy(text));
    } catch {
      legacy(text);
    }
  };

  return [done, copy];
}

/* --------------------------------------------------------------- CopyField */

export function CopyField({ value, label, mono = true, masked, className }: {
  value: string;
  label?: ReactNode;
  mono?: boolean;
  /** Shows dots until revealed — for keys and secrets on a shared screen. */
  masked?: boolean;
  className?: string;
}) {
  const [done, copy] = useCopy();
  const [shown, setShown] = useState(false);
  const labelId = useId();
  const hidden = !!masked && !shown;
  return (
    <div className={className}>
      {label != null && (
        <span id={labelId} className="mb-1.5 block text-[13px] font-medium text-muted">{label}</span>
      )}
      <div className="flex items-center gap-1.5 rounded-md border border-line bg-canvas px-3 py-2">
        <span
          aria-labelledby={label != null ? labelId : undefined}
          className={cx("min-w-0 flex-1 truncate text-[13px] text-ink", mono && "font-mono")}
        >
          {hidden ? "••••••••••••••••••••" : value}
        </span>
        {masked && (
          <button
            type="button"
            onClick={() => setShown((s) => !s)}
            className="shrink-0 rounded px-1.5 py-0.5 text-[11px] font-semibold text-muted transition-colors hover:bg-surface hover:text-ink focus:outline-none focus-visible:ring-4 focus-visible:ring-brand-soft"
          >
            {shown ? "Hide" : "Show"}
          </button>
        )}
        <button
          type="button"
          onClick={() => copy(value)}
          aria-label={done ? "Copied" : "Copy to clipboard"}
          className="shrink-0 rounded p-1 text-faint transition-colors hover:bg-surface hover:text-ink focus:outline-none focus-visible:ring-4 focus-visible:ring-brand-soft"
        >
          {done ? <Icon.Check size={15} className="text-ok" /> : <Icon.Copy size={15} />}
        </button>
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- CodeBlock */

// bg-code / text-code-fg are dark in both themes by design (tailwind.css:53-56),
// so this block is the one surface that does not flip with the theme.
export function CodeBlock({ code, language, copy = true, maxHeight, className }: {
  code: string;
  /** Label only — there is no highlighter and no dependency to pull one in. */
  language?: string;
  copy?: boolean;
  maxHeight?: number;
  className?: string;
}) {
  const [done, doCopy] = useCopy();
  return (
    <div className={cx("overflow-hidden rounded-lg bg-code", className)}>
      {(language != null || copy) && (
        <div className="flex items-center justify-between gap-2 border-b border-white/10 px-3 py-1.5">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-code-fg/55">
            {language ?? "code"}
          </span>
          {copy && (
            <button
              type="button"
              onClick={() => doCopy(code)}
              aria-label={done ? "Copied" : "Copy code"}
              className="rounded p-1 text-code-fg/70 transition-colors hover:bg-white/10 hover:text-code-fg focus:outline-none focus-visible:ring-4 focus-visible:ring-brand-soft"
            >
              {done ? <Icon.Check size={14} /> : <Icon.Copy size={14} />}
            </button>
          )}
        </div>
      )}
      <pre
        style={{ maxHeight }}
        className="m-0 overflow-auto px-3.5 py-3 font-mono text-[12.5px] leading-relaxed text-code-fg"
      >
        <code>{code}</code>
      </pre>
    </div>
  );
}

/* --------------------------------------------------------------- Breadcrumb */

// An item without `to` renders as text, so a trail can carry a non-navigable
// ancestor ("Agents / Support bot / Sources") without faking a link.
export function Breadcrumb({ items }: { items: { label: ReactNode; to?: string }[] }) {
  return (
    <nav aria-label="Breadcrumb" className="mb-3">
      <ol className="m-0 flex list-none flex-wrap items-center gap-1.5 p-0 text-[13px]">
        {items.map((item, i) => {
          const last = i === items.length - 1;
          return (
            <li key={i} className="flex items-center gap-1.5">
              {i > 0 && <Icon.Chevron size={13} className="-rotate-90 text-faint" />}
              {item.to != null && !last ? (
                <Link to={item.to} className="text-muted no-underline transition-colors hover:text-ink">
                  {item.label}
                </Link>
              ) : (
                <span aria-current={last ? "page" : undefined} className={last ? "font-medium text-ink" : "text-muted"}>
                  {item.label}
                </span>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

/* ----------------------------------------------------------------- TagInput */

// Replaces the hand-rolled blocklist chips in AgentConfig.tsx. Enter and comma
// commit, Backspace on an empty field removes the last chip.
export function TagInput({ value, onChange, placeholder, tone = "neutral", validate, max, disabled }: {
  value: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
  tone?: "neutral" | "brand" | "warn";
  /** Returns an error message, or null when the entry is acceptable. */
  validate?: (v: string) => string | null;
  max?: number;
  disabled?: boolean;
}) {
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const field = useRef<HTMLInputElement | null>(null);
  const full = max !== undefined && value.length >= max;

  const commit = () => {
    const entry = draft.trim().replace(/,+$/, "").trim();
    if (!entry) return;
    if (full) {
      setError(`No more than ${max}.`);
      return;
    }
    // A duplicate is not an error the user needs told about — it is already there.
    if (value.includes(entry)) {
      setDraft("");
      setError(null);
      return;
    }
    const failure = validate?.(entry) ?? null;
    if (failure) {
      setError(failure);
      return;
    }
    setError(null);
    setDraft("");
    onChange([...value, entry]);
  };

  const remove = (entry: string) => {
    setError(null);
    onChange(value.filter((v) => v !== entry));
  };

  return (
    <div>
      <div
        onClick={() => field.current?.focus()}
        className={cx(
          "flex flex-wrap items-center gap-1.5 rounded-md border bg-surface px-2 py-1.5 transition-shadow",
          "focus-within:border-brand focus-within:ring-4 focus-within:ring-brand-soft",
          error ? "border-danger" : "border-line-strong",
          disabled && "pointer-events-none opacity-60",
        )}
      >
        {value.map((entry) => (
          <Badge key={entry} tone={tone}>
            <span className="max-w-56 truncate">{entry}</span>
            <button
              type="button"
              aria-label={`Remove ${entry}`}
              onClick={(e) => { e.stopPropagation(); remove(entry); }}
              className="-mr-0.5 rounded-full p-0.5 opacity-70 transition-opacity hover:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-current"
            >
              <Icon.X size={11} />
            </button>
          </Badge>
        ))}
        <input
          ref={field}
          type="text"
          value={draft}
          disabled={disabled}
          placeholder={full ? undefined : placeholder}
          aria-invalid={error ? true : undefined}
          onChange={(e) => { setDraft(e.target.value); setError(null); }}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              commit();
            } else if (e.key === "Backspace" && draft === "") {
              const last = value[value.length - 1];
              if (last !== undefined) remove(last);
            }
          }}
          className="min-w-32 flex-1 border-0 bg-transparent px-1 py-0.5 text-sm text-ink outline-none placeholder:text-faint"
        />
      </div>
      {(error || (max !== undefined && !error)) && (
        <p className={cx("m-0 mt-1.5 text-xs", error ? "font-medium text-danger" : "text-faint")} role={error ? "alert" : undefined}>
          {error ?? `${value.length} of ${max} used.`}
        </p>
      )}
    </div>
  );
}

/* -------------------------------------------------------------- ColorPicker */

const HEX6 = /^#[0-9a-f]{6}$/i;

// Invalid hex keeps the last valid colour rather than clearing it: someone
// mid-edit at "#25" has not asked to lose their accent.
export function ColorPicker({ value, onChange, presets, presetMeta, label }: {
  /** #rrggbb. */
  value: string;
  onChange: (v: string) => void;
  presets?: string[];
  /** Per-swatch overrides keyed by lowercase hex (§6.1): a human name, so a
   *  choice stays nameable when two swatches are not visually separable, and an
   *  optional `disabled` whose reason is shown in a Tooltip. A disabled swatch is
   *  still rendered and still reads as selected when it is the stored value. */
  presetMeta?: Record<string, { name?: string; disabled?: boolean; tooltip?: ReactNode }>;
  label?: ReactNode;
}) {
  const swatches = presets ?? [...IDENTITY_SWATCHES];
  const [text, setText] = useState(value);
  const labelId = useId();
  // Follow an external change (a template applying its accent) without fighting
  // the user's in-progress typing: value only changes here when it is valid.
  useEffect(() => { setText(value); }, [value]);

  const invalid = text.trim() !== "" && !HEX6.test(text.trim());

  const pick = (hex: string) => {
    const next = hex.trim().toLowerCase();
    setText(next);
    if (HEX6.test(next)) onChange(next);
  };

  return (
    <div role="group" aria-labelledby={label != null ? labelId : undefined}>
      {label != null && (
        <span id={labelId} className="mb-1.5 block text-[13px] font-medium text-muted">{label}</span>
      )}
      <div className="flex flex-wrap items-center gap-1.5">
        {swatches.map((hex) => {
          const key = hex.toLowerCase();
          const on = key === value.trim().toLowerCase();
          const meta = presetMeta?.[key];
          const swatch = (
            <button
              type="button"
              aria-label={meta?.name ? `${meta.name} (${hex})` : hex}
              aria-pressed={on}
              disabled={meta?.disabled}
              onClick={() => pick(hex)}
              style={{ backgroundColor: hex }}
              className={cx(
                "h-7 w-7 rounded-md border transition-transform focus:outline-none focus-visible:ring-4 focus-visible:ring-brand-soft",
                on ? "border-ink ring-2 ring-ink/20 scale-105" : "border-black/10 hover:scale-105",
                meta?.disabled && "cursor-not-allowed opacity-45 hover:scale-100",
                "motion-reduce:transform-none",
              )}
            />
          );
          return meta?.disabled && meta.tooltip != null
            ? <Tooltip key={hex} label={meta.tooltip}><span className="inline-flex">{swatch}</span></Tooltip>
            : <span key={hex} className="inline-flex" title={meta?.name}>{swatch}</span>;
        })}
        <span className="mx-1 h-6 w-px bg-line" />
        <input
          type="color"
          value={HEX6.test(value.trim()) ? value.trim() : "#000000"}
          aria-label="Choose a custom colour"
          onChange={(e) => pick(e.target.value)}
          className="h-7 w-9 cursor-pointer rounded-md border border-line-strong bg-surface p-0.5 focus:outline-none focus-visible:ring-4 focus-visible:ring-brand-soft"
        />
        <Input
          value={text}
          aria-label="Hex colour"
          aria-invalid={invalid ? true : undefined}
          spellCheck={false}
          onChange={(e) => pick(e.target.value)}
          className={cx("w-28 font-mono text-[13px]", invalid && "border-danger focus:border-danger focus:ring-danger-soft")}
        />
      </div>
    </div>
  );
}

/* --------------------------------------------------------------------- Menu */

export type MenuItem =
  | { label: ReactNode; onSelect: () => void; icon?: ReactNode; tone?: "danger"; disabled?: boolean; hint?: ReactNode }
  | { separator: true };

// The `⋯` overflow menu. `trigger` is content (an icon, a label) — this wraps it
// in the button itself, so do not pass a Button or another interactive element.
export function Menu({ trigger, items, align = "end" }: {
  trigger: ReactNode;
  items: MenuItem[];
  align?: "start" | "end";
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const wrap = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const menuId = useId();

  // The dropdown is position:fixed at the trigger's viewport rect, not absolute
  // inside the trigger's parent: parents clip (`overflow-hidden` on a card is
  // the common one) and a menu that opens downward inside a card shows its
  // first row and cuts off the rest. Fixed escapes every ancestor clip, so the
  // same Menu works in a card, a panel header or a bare toolbar row.
  const [menuStyle, setMenuStyle] = useState<CSSProperties | null>(null);

  const place = useCallback(() => {
    const trigger = triggerRef.current;
    const menu = menuRef.current;
    if (!trigger || !menu) return;
    const rect = trigger.getBoundingClientRect();
    const width = menu.offsetWidth;
    const height = menu.offsetHeight;
    // Open downward from the trigger; flip up only when the menu would run off
    // the bottom of the viewport, which is where downward menus usually die.
    const top = rect.bottom + 4 + height > window.innerHeight
      ? Math.max(8, rect.top - height - 4)
      : rect.bottom + 4;
    let left = align === "end" ? rect.right - width : rect.left;
    left = Math.min(Math.max(8, left), Math.max(8, window.innerWidth - width - 8));
    setMenuStyle({ position: "fixed", top, left, visibility: "visible" });
  }, [align]);

  useLayoutEffect(() => {
    if (!open) return;
    place();
    // Re-anchor while open: a scroll or resize moves the trigger under a fixed
    // menu, and following it beats closing on the user.
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, place, items]);

  // Indices of items that can hold focus. Separators and disabled rows are
  // skipped by every keyboard path rather than being focusable dead ends.
  const enabled = items.reduce<number[]>((acc, item, i) => {
    if (!("separator" in item) && !item.disabled) acc.push(i);
    return acc;
  }, []);

  const close = (restoreFocus: boolean) => {
    setOpen(false);
    setActive(-1);
    if (restoreFocus) triggerRef.current?.focus();
  };

  const openAt = (edge: "first" | "last") => {
    setOpen(true);
    // stale coordinates from the last open would place the menu off-trigger for
    // one frame; hidden until `place` has measured the fresh render
    setMenuStyle(null);
    setActive((edge === "first" ? enabled[0] : enabled[enabled.length - 1]) ?? -1);
  };

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      // Click-outside closes without stealing focus back — the user is already
      // on their way somewhere else.
      if (!wrap.current?.contains(e.target as Node)) close(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  useEffect(() => {
    if (!open || active < 0) return;
    itemRefs.current[active]?.focus();
  }, [open, active]);

  const onMenuKey = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close(true);
      return;
    }
    if (e.key === "Tab") {
      close(true);
      return;
    }
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp" && e.key !== "Home" && e.key !== "End") return;
    e.preventDefault();
    if (enabled.length === 0) return;
    if (e.key === "Home") { setActive(enabled[0] ?? -1); return; }
    if (e.key === "End") { setActive(enabled[enabled.length - 1] ?? -1); return; }
    const step = e.key === "ArrowDown" ? 1 : -1;
    const at = enabled.indexOf(active);
    const nextAt = at === -1 ? (step === 1 ? 0 : enabled.length - 1) : (at + step + enabled.length) % enabled.length;
    setActive(enabled[nextAt] ?? -1);
  };

  return (
    <div ref={wrap} className="relative inline-flex">
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => (open ? close(false) : openAt("first"))}
        onKeyDown={(e) => {
          if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
          e.preventDefault();
          openAt(e.key === "ArrowDown" ? "first" : "last");
        }}
        className="inline-flex items-center justify-center gap-1.5 rounded-md text-muted transition-colors hover:bg-canvas hover:text-ink focus:outline-none focus-visible:ring-4 focus-visible:ring-brand-soft"
      >
        {trigger}
      </button>

      {open && (
        <div
          id={menuId}
          ref={menuRef}
          role="menu"
          aria-orientation="vertical"
          onKeyDown={onMenuKey}
          style={menuStyle ?? { position: "fixed", visibility: "hidden" }}
          className={cx(
            "z-50 min-w-52 rounded-lg border border-line bg-elevated py-1 shadow-pop",
            "animate-[ca-rise_.12s_ease-out] motion-reduce:animate-none",
          )}
        >
          {items.map((item, i) =>
            "separator" in item ? (
              <div key={i} role="separator" className="my-1 h-px bg-line" />
            ) : (
              <button
                key={i}
                ref={(el) => { itemRefs.current[i] = el; }}
                type="button"
                role="menuitem"
                tabIndex={-1}
                disabled={item.disabled}
                onMouseEnter={() => setActive(i)}
                onClick={() => { close(true); item.onSelect(); }}
                className={cx(
                  "flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm transition-colors",
                  "focus:outline-none focus:bg-canvas disabled:pointer-events-none disabled:opacity-45",
                  item.tone === "danger" ? "text-danger hover:bg-danger-soft" : "text-ink hover:bg-canvas",
                )}
              >
                {item.icon != null && <span className="shrink-0 text-current opacity-70">{item.icon}</span>}
                <span className="min-w-0 flex-1 truncate">{item.label}</span>
                {item.hint != null && <span className="shrink-0 text-xs text-faint">{item.hint}</span>}
              </button>
            ),
          )}
        </div>
      )}
    </div>
  );
}



