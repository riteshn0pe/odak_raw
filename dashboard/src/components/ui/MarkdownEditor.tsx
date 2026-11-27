// Markdown editor primitive — write/preview textarea with a formatting toolbar.
//
// Named by productisation-next-agents-ui.md §4.3 for the text-snippet tile
// ("Text snippet and Q&A open SlideOvers with MarkdownEditor / a repeating
// question-answer SortableList") but absent from both primitive inventories, so
// it is built here in S9. Follows JsonEditor.tsx, the closest sibling: controlled
// `value`/`onChange`, a `minHeight` in px, a `role="status"` footer line for the
// one piece of live feedback, and Tab inserting spaces instead of escaping the
// field, because this is an editing surface and not a form control.
//
// Zero dependencies: the selection work below is plain setSelectionRange, and
// preview rendering is delegated to MarkdownPreview, which parses to React
// elements and never to HTML.
import { useMemo, useRef, useState, type ReactNode } from "react";
import { SegmentedControl, Textarea, cx } from "./index";
import { Icon } from "./icons";
import { MarkdownPreview } from "../MarkdownPreview";

/** `snippets.body` is capped at 200,000 chars server-side (crawler §6). */
export const MARKDOWN_MAX_LENGTH = 200_000;

interface Edit {
  text: string;
  /** Absolute offsets to select once the new text has been committed. */
  from: number;
  to: number;
}

/** Wraps (or unwraps) the selection in a delimiter pair. */
function wrap(value: string, start: number, end: number, open: string, close: string, fallback: string): Edit {
  const selected = value.slice(start, end);
  const already =
    value.slice(Math.max(0, start - open.length), start) === open &&
    value.slice(end, end + close.length) === close;

  if (already && selected !== "") {
    const from = start - open.length;
    return { text: value.slice(0, from) + selected + value.slice(end + close.length), from, to: from + selected.length };
  }
  const body = selected === "" ? fallback : selected;
  const text = value.slice(0, start) + open + body + close + value.slice(end);
  return { text, from: start + open.length, to: start + open.length + body.length };
}

/** Expands an offset pair to whole lines, so line-prefix actions are stable. */
function lineSpan(value: string, start: number, end: number): { from: number; to: number } {
  const from = value.lastIndexOf("\n", start - 1) + 1;
  const nextBreak = value.indexOf("\n", end);
  return { from, to: nextBreak === -1 ? value.length : nextBreak };
}

/** Toggles a per-line prefix (`- `, `1. `, `> `) across the selected lines. */
function prefixLines(
  value: string,
  start: number,
  end: number,
  make: (index: number) => string,
  match: RegExp,
): Edit {
  const span = lineSpan(value, start, end);
  const block = value.slice(span.from, span.to);
  const lines = block.split("\n");
  const allPrefixed = lines.every((l) => l.trim() === "" || match.test(l));
  const next = lines
    .map((l, i) => {
      if (l.trim() === "") return l;
      return allPrefixed ? l.replace(match, "") : make(i) + l;
    })
    .join("\n");
  return {
    text: value.slice(0, span.from) + next + value.slice(span.to),
    from: span.from,
    to: span.from + next.length,
  };
}

/**
 * Cycles the current line's heading level: none → ## → ### → … → ###### → none.
 * A cycle rather than a fixed insert because one button has to serve six levels
 * and pressing it twice should be visibly different from pressing it once.
 */
function cycleHeading(value: string, start: number, end: number): Edit {
  const span = lineSpan(value, start, end);
  const first = value.indexOf("\n", span.from);
  const lineEnd = first === -1 || first > span.to ? span.to : first;
  const line = value.slice(span.from, lineEnd);
  const current = /^(#{1,6})\s+/.exec(line);
  const level = current ? (current[1] ?? "").length : 0;
  const stripped = line.replace(/^#{1,6}\s+/, "");
  const next = level === 0 ? "## " : level >= 6 ? "" : "#".repeat(level + 1) + " ";
  const replaced = next + stripped;
  return {
    text: value.slice(0, span.from) + replaced + value.slice(lineEnd),
    from: span.from + next.length,
    to: span.from + replaced.length,
  };
}

/**
 * `[label](url)`, with whichever half the selection did not supply pre-selected
 * so the next keystroke fills it in.
 */
function linkEdit(value: string, start: number, end: number): Edit {
  const selected = value.slice(start, end).trim();
  const looksLikeUrl = /^(https?:\/\/|mailto:|www\.)\S*$/i.test(selected);
  const label = looksLikeUrl || selected === "" ? "text" : selected;
  const href = looksLikeUrl ? selected : "https://";
  const inserted = `[${label}](${href})`;
  const text = value.slice(0, start) + inserted + value.slice(end);
  // Select the half the author still has to write.
  const from = looksLikeUrl ? start + 1 : start + label.length + 3;
  const to = looksLikeUrl ? start + 1 + label.length : start + label.length + 3 + href.length;
  return { text, from, to };
}

/* -------------------------------------------------------------------------- */
/* Component                                                                  */
/* -------------------------------------------------------------------------- */

export interface MarkdownEditorProps {
  value: string;
  onChange: (text: string) => void;
  placeholder?: string;
  disabled?: boolean;
  /** Server cap, surfaced as a live character count. Defaults to 200,000. */
  maxLength?: number;
  /** Editor height in rows. Ignored when `height` is given. */
  minRows?: number;
  /** Editor height in px. Wins over `minRows`. */
  height?: number;
  /** Scroll cap for the preview pane. Defaults to the editor height. */
  previewMaxHeight?: number;
  /** Placeholder shown by the preview pane while the body is empty. */
  previewEmptyLabel?: string;
  id?: string;
  className?: string;
}

const TOOL_BUTTON =
  "inline-flex h-7 min-w-7 items-center justify-center rounded-md px-1.5 text-[13px] font-medium text-muted transition-colors hover:bg-sunken hover:text-ink disabled:opacity-40 disabled:pointer-events-none";

/**
 * Controlled markdown editor with a write/preview toggle.
 *
 * The toggle is `SegmentedControl` rather than a bespoke pair of buttons so the
 * affordance matches every other two-state switch in the dashboard.
 */
export function MarkdownEditor({
  value,
  onChange,
  placeholder,
  disabled = false,
  maxLength = MARKDOWN_MAX_LENGTH,
  minRows = 10,
  height,
  previewMaxHeight,
  previewEmptyLabel = "Nothing to preview yet.",
  id,
  className,
}: MarkdownEditorProps) {
  const [mode, setMode] = useState<"write" | "preview">("write");
  const host = useRef<HTMLDivElement | null>(null);

  // `Textarea` is typed as bare TextareaHTMLAttributes and so accepts no ref.
  // Locating the element through the host element keeps this primitive from
  // having to fork the shared one.
  const field = (): HTMLTextAreaElement | null => host.current?.querySelector("textarea") ?? null;

  const boxHeight = height ?? Math.round(minRows * 21 + 18);
  const stats = useMemo(
    () => ({ chars: value.length, lines: value === "" ? 0 : value.split("\n").length }),
    [value],
  );
  const ratio = maxLength > 0 ? stats.chars / maxLength : 0;

  function commit(edit: Edit) {
    if (disabled) return;
    // Silently refusing beyond the cap beats truncating text the author typed.
    if (edit.text.length > maxLength) return;
    onChange(edit.text);
    const el = field();
    if (el === null) return;
    window.requestAnimationFrame(() => {
      el.focus();
      el.setSelectionRange(edit.from, edit.to);
    });
  }

  function act(kind: "bold" | "italic" | "code" | "link" | "heading" | "list") {
    const el = field();
    if (el === null || disabled) return;
    const s = el.selectionStart;
    const e = el.selectionEnd;
    switch (kind) {
      case "bold":
        commit(wrap(value, s, e, "**", "**", "bold text"));
        return;
      case "italic":
        commit(wrap(value, s, e, "*", "*", "italic text"));
        return;
      case "code":
        // A multi-line selection wants a fence; a word wants a span.
        commit(
          value.slice(s, e).includes("\n")
            ? wrap(value, s, e, "```\n", "\n```", "code")
            : wrap(value, s, e, "`", "`", "code"),
        );
        return;
      case "link":
        commit(linkEdit(value, s, e));
        return;
      case "heading":
        commit(cycleHeading(value, s, e));
        return;
      case "list":
        commit(prefixLines(value, s, e, () => "- ", /^\s*[-*+]\s+/));
        return;
    }
  }

  const tools: Array<{ kind: Parameters<typeof act>[0]; title: string; icon: ReactNode }> = [
    { kind: "bold", title: "Bold (Ctrl/⌘ B)", icon: <span className="font-bold">B</span> },
    { kind: "italic", title: "Italic (Ctrl/⌘ I)", icon: <span className="font-serif italic">I</span> },
    { kind: "code", title: "Code", icon: <span className="font-mono text-[12px]">{"</>"}</span> },
    { kind: "link", title: "Link (Ctrl/⌘ K)", icon: <Icon.Link size={14} /> },
    { kind: "heading", title: "Heading", icon: <span className="font-semibold">H</span> },
    { kind: "list", title: "Bulleted list", icon: <span className="text-[15px] leading-none">•—</span> },
  ];

  return (
    <div ref={host} className={cx("flex flex-col gap-2", className)}>
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-0.5">
          {tools.map((t) => (
            <button
              key={t.kind}
              type="button"
              title={t.title}
              aria-label={t.title}
              disabled={disabled || mode === "preview"}
              // Keeps focus and therefore the selection inside the textarea: a
              // toolbar button that steals focus first has nothing to wrap.
              onMouseDown={(ev) => ev.preventDefault()}
              onClick={() => act(t.kind)}
              className={TOOL_BUTTON}
            >
              {t.icon}
            </button>
          ))}
        </div>

        <SegmentedControl
          size="sm"
          value={mode}
          onChange={(v) => setMode(v === "preview" ? "preview" : "write")}
          items={[
            { value: "write", label: "Write" },
            { value: "preview", label: "Preview" },
          ]}
        />
      </div>

      {mode === "write" ? (
        <Textarea
          id={id}
          value={value}
          disabled={disabled}
          maxLength={maxLength}
          placeholder={placeholder}
          spellCheck
          mono
          resizable
          onChange={(ev) => onChange(ev.target.value)}
          onKeyDown={(ev) => {
            if (disabled) return;
            const mod = ev.metaKey || ev.ctrlKey;
            if (mod && !ev.altKey) {
              const key = ev.key.toLowerCase();
              if (key === "b" || key === "i" || key === "k") {
                ev.preventDefault();
                act(key === "b" ? "bold" : key === "i" ? "italic" : "link");
                return;
              }
            }
            // Tab indents rather than leaving the field, matching JsonEditor:
            // Escape then Tab is still the way out.
            if (ev.key !== "Tab" || ev.shiftKey || mod) return;
            ev.preventDefault();
            const el = ev.currentTarget;
            const s = el.selectionStart;
            const e = el.selectionEnd;
            commit({ text: value.slice(0, s) + "  " + value.slice(e), from: s + 2, to: s + 2 });
          }}
          style={{ minHeight: boxHeight }}
          className="leading-relaxed"
        />
      ) : (
        <div style={{ minHeight: boxHeight }}>
          <MarkdownPreview
            value={value}
            maxHeight={previewMaxHeight ?? boxHeight}
            emptyLabel={previewEmptyLabel}
          />
        </div>
      )}

      <div className="flex items-center justify-between gap-3 text-xs">
        <span className="text-faint">
          Markdown supported — headings, <span className="font-mono">**bold**</span>, lists, links,
          tables.
        </span>
        <span
          role="status"
          aria-live="polite"
          className={cx(
            "tabular-nums whitespace-nowrap",
            ratio >= 1 ? "font-medium text-danger" : ratio >= 0.9 ? "text-warn" : "text-faint",
          )}
        >
          {stats.chars.toLocaleString()} / {maxLength.toLocaleString()}
          {stats.lines > 0 && <span className="text-faint"> · {stats.lines} lines</span>}
        </span>
      </div>
    </div>
  );
}

export default MarkdownEditor;
