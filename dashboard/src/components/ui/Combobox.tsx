// Typeahead select (§5.2): the action picker in procedures, tag and collection
// pickers, the contact attribute picker, template search.
//
// Hand-rolled rather than a native <select> because the lists are long enough to
// need filtering and some of them need "create this one" — and hand-rolled rather
// than a library because the keyboard contract below is the whole point of the
// component, so we own it.
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { cx, Input } from "./index";
import { Icon } from "./icons";

export interface ComboboxOption {
  value: string;
  label: ReactNode;
  hint?: ReactNode;
  icon?: ReactNode;
  group?: string;
}

/** Text we can filter and put in an input. A ReactNode label falls back to value. */
function textOf(o: ComboboxOption): string {
  return typeof o.label === "string" ? o.label : o.value;
}

export function Combobox({
  options, value, onChange, placeholder, emptyLabel = "No matches", allowCreate, onCreate, disabled,
}: {
  options: ComboboxOption[];
  value: string | null;
  onChange: (v: string | null) => void;
  placeholder?: string;
  emptyLabel?: ReactNode;
  allowCreate?: boolean;
  onCreate?: (label: string) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const wrap = useRef<HTMLDivElement | null>(null);
  const input = useRef<HTMLInputElement | null>(null);
  const list = useRef<HTMLDivElement | null>(null);
  const listId = useId();

  const selected = useMemo(() => options.find((o) => o.value === value) ?? null, [options, value]);

  // Grouped options keep their authored order inside a group, and ungrouped
  // options stay first — a picker that reshuffles as you type is unusable.
  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    const matches = q
      ? options.filter((o) => {
          const hint = typeof o.hint === "string" ? o.hint : "";
          return (textOf(o) + " " + o.value + " " + hint).toLowerCase().includes(q);
        })
      : options;
    const groups: string[] = [];
    for (const o of matches) {
      const g = o.group ?? "";
      if (!groups.includes(g)) groups.push(g);
    }
    groups.sort((a, b) => (a === "" ? -1 : b === "" ? 1 : 0));
    return groups.flatMap((g) => matches.filter((o) => (o.group ?? "") === g));
  }, [options, query]);

  const trimmed = query.trim();
  const canCreate =
    !!allowCreate && !!onCreate && trimmed.length > 0 &&
    !options.some((o) => textOf(o).toLowerCase() === trimmed.toLowerCase());
  const createIndex = canCreate ? visible.length : -1;
  const count = visible.length + (canCreate ? 1 : 0);

  useEffect(() => { setActive(0); }, [query, open]);

  // Click-outside. mousedown rather than click so a press that starts outside
  // closes before it can also activate something inside the reflowed layout.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!wrap.current?.contains(e.target as Node)) { setOpen(false); setQuery(""); }
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    list.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active, open]);

  const commit = (i: number) => {
    if (i === createIndex && onCreate) {
      onCreate(trimmed);
    } else {
      const o = visible[i];
      if (!o) return;
      onChange(o.value);
    }
    setOpen(false);
    setQuery("");
    input.current?.focus();
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) { setOpen(true); return; }
      if (count === 0) return;
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive((a) => (a + step + count) % count);
      return;
    }
    if (e.key === "Home" && open) { e.preventDefault(); setActive(0); return; }
    if (e.key === "End" && open) { e.preventDefault(); setActive(Math.max(count - 1, 0)); return; }
    if (e.key === "Enter") {
      if (!open || count === 0) return;
      e.preventDefault();
      commit(active);
      return;
    }
    if (e.key === "Escape") {
      if (!open) return;
      e.preventDefault();
      e.stopPropagation();
      setOpen(false);
      setQuery("");
      return;
    }
    if (e.key === "Tab" && open) { setOpen(false); setQuery(""); }
  };

  let lastGroup: string | null = null;

  return (
    <div ref={wrap} className="relative">
      <div className="relative">
        <Input
          ref={input}
          role="combobox"
          type="text"
          autoComplete="off"
          spellCheck={false}
          aria-expanded={open}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={open && count > 0 ? `${listId}-o${active}` : undefined}
          disabled={disabled}
          placeholder={placeholder}
          value={open ? query : selected ? textOf(selected) : ""}
          onChange={(e) => { setQuery(e.target.value); setOpen(true); }}
          onFocus={() => setOpen(true)}
          onKeyDown={onKeyDown}
          className={cx(
            "text-ink transition-shadow disabled:cursor-not-allowed disabled:opacity-45",
            value ? "pr-16" : "pr-9",
          )}
        />
        <span className="pointer-events-none absolute inset-y-0 right-2.5 flex items-center text-faint">
          <Icon.Chevron size={15} />
        </span>
        {value && !disabled && (
          <button
            type="button"
            aria-label="Clear selection"
            onClick={() => { onChange(null); setQuery(""); input.current?.focus(); }}
            className="absolute inset-y-0 right-8 my-auto h-6 w-6 rounded-md text-faint transition-colors hover:bg-canvas hover:text-ink focus:outline-none focus:ring-4 focus:ring-brand-soft"
          >
            <Icon.X size={13} className="mx-auto" />
          </button>
        )}
      </div>

      {open && (
        <div
          ref={list}
          id={listId}
          role="listbox"
          className="absolute z-50 mt-1 max-h-72 w-full overflow-y-auto rounded-lg border border-line bg-elevated py-1 shadow-pop animate-[ca-rise_.12s_ease-out] motion-reduce:animate-none"
        >
          {count === 0 && <div className="px-3 py-2.5 text-[13px] text-faint">{emptyLabel}</div>}

          {visible.map((o, i) => {
            const group = o.group ?? "";
            const header = group && group !== lastGroup ? group : null;
            lastGroup = group;
            const on = i === active;
            return (
              <div key={o.value} role="presentation">
                {header && (
                  <div className="px-3 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wide text-faint">
                    {header}
                  </div>
                )}
                <div
                  id={`${listId}-o${i}`}
                  data-idx={i}
                  role="option"
                  aria-selected={o.value === value}
                  onMouseEnter={() => setActive(i)}
                  onMouseDown={(e) => { e.preventDefault(); commit(i); }}
                  className={cx(
                    "flex cursor-pointer items-center gap-2.5 px-3 py-2 text-sm",
                    on ? "bg-brand-soft text-ink" : "text-ink",
                  )}
                >
                  {o.icon && <span className="shrink-0 text-muted">{o.icon}</span>}
                  <span className="min-w-0 flex-1 truncate">{o.label}</span>
                  {o.hint && <span className="shrink-0 text-xs text-faint">{o.hint}</span>}
                  {o.value === value && <Icon.Check size={14} className="shrink-0 text-brand" />}
                </div>
              </div>
            );
          })}

          {canCreate && (
            <div
              id={`${listId}-o${createIndex}`}
              data-idx={createIndex}
              role="option"
              aria-selected={false}
              onMouseEnter={() => setActive(createIndex)}
              onMouseDown={(e) => { e.preventDefault(); commit(createIndex); }}
              className={cx(
                "flex cursor-pointer items-center gap-2 border-t border-line px-3 py-2 text-sm",
                active === createIndex ? "bg-brand-soft text-ink" : "text-brand",
              )}
            >
              <Icon.Plus size={14} className="shrink-0" />
              <span className="truncate">Create “{trimmed}”</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
