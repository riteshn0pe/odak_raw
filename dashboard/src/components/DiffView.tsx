// Line-level diff, no dependency (§4.2, §4.8).
//
// Two callers, both of which need the same thing — "show me exactly what moved":
//   1. Compare / A-B on Instructions: `View diff` between the live prompt and the
//      draft prompt, opened in a Modal size="lg".
//   2. The concurrent-edit guard: `See what changed` between the config this
//      editor loaded and the config someone else just saved.
//
// Longest-common-subsequence over lines. An LCS table on two 400-line prompts is
// 160k cells, which is nothing, and the alternative — a word-level diff or a
// library — buys precision nobody asked for on text where the unit of meaning is
// the line anyway. The guard rail below caps the table so a pasted 20k-line blob
// degrades to a plain two-pane view instead of allocating 400M cells.
import { useMemo } from "react";
import { cx } from "./ui";

/** One rendered row. `kind` decides the gutter, the tint, and the sign. */
export interface DiffRow {
  kind: "same" | "add" | "del";
  /** 1-based line number in the `before` text; null on an addition. */
  beforeLine: number | null;
  /** 1-based line number in the `after` text; null on a deletion. */
  afterLine: number | null;
  text: string;
}

/** Above this, the LCS table is too big to be worth building. */
const LCS_LINE_CAP = 4000;

function lines(text: string): string[] {
  // A trailing newline produces a final empty element that reads as a phantom
  // changed line, so it is dropped — but only the last one, because a document
  // that genuinely ends in a blank line keeps it.
  const out = text.replace(/\r\n?/g, "\n").split("\n");
  if (out.length > 1 && out[out.length - 1] === "") out.pop();
  return out;
}

/**
 * Diff two texts into a flat row list. Exported so a caller can count changes
 * without rendering — `Compare` wants "12 lines changed" in its own header.
 */
export function diffLines(before: string, after: string): DiffRow[] {
  const a = lines(before);
  const b = lines(after);

  // Degenerate and oversized cases: emit every deletion then every addition.
  // Honest, and it never lies about which side a line is on.
  if (a.length + b.length > LCS_LINE_CAP * 2 || a.length === 0 || b.length === 0) {
    return [
      ...a.map((text, i): DiffRow => ({ kind: "del", beforeLine: i + 1, afterLine: null, text })),
      ...b.map((text, i): DiffRow => ({ kind: "add", beforeLine: null, afterLine: i + 1, text })),
    ];
  }

  // lcs[i][j] = length of the longest common subsequence of a[i:] and b[j:].
  // Built backwards so the walk that follows can move forward greedily.
  const lcs: number[][] = Array.from({ length: a.length + 1 }, () =>
    new Array<number>(b.length + 1).fill(0),
  );
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      lcs[i]![j] = a[i] === b[j]
        ? lcs[i + 1]![j + 1]! + 1
        : Math.max(lcs[i + 1]![j]!, lcs[i]![j + 1]!);
    }
  }

  const rows: DiffRow[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      rows.push({ kind: "same", beforeLine: i + 1, afterLine: j + 1, text: a[i]! });
      i++; j++;
    } else if (lcs[i + 1]![j]! >= lcs[i]![j + 1]!) {
      rows.push({ kind: "del", beforeLine: i + 1, afterLine: null, text: a[i]! });
      i++;
    } else {
      rows.push({ kind: "add", beforeLine: null, afterLine: j + 1, text: b[j]! });
      j++;
    }
  }
  while (i < a.length) { rows.push({ kind: "del", beforeLine: i + 1, afterLine: null, text: a[i]! }); i++; }
  while (j < b.length) { rows.push({ kind: "add", beforeLine: null, afterLine: j + 1, text: b[j]! }); j++; }
  return rows;
}

/** Added and removed line counts. `Compare`'s "12 lines changed" reads this. */
export function diffStat(before: string, after: string): { added: number; removed: number; changed: number } {
  let added = 0;
  let removed = 0;
  for (const row of diffLines(before, after)) {
    if (row.kind === "add") added++;
    else if (row.kind === "del") removed++;
  }
  return { added, removed, changed: added + removed };
}

/**
 * Stable, diffable text for an arbitrary object — the config guard's input.
 * Keys are sorted at every level, so two reads of the same config always
 * stringify identically and key order never shows up as a spurious change.
 */
export function stableJson(value: unknown): string {
  const seen = new WeakSet<object>();
  const walk = (node: unknown): unknown => {
    if (Array.isArray(node)) return node.map(walk);
    if (node !== null && typeof node === "object") {
      if (seen.has(node)) return "[circular]";
      seen.add(node);
      const out: Record<string, unknown> = {};
      for (const key of Object.keys(node as Record<string, unknown>).sort()) {
        out[key] = walk((node as Record<string, unknown>)[key]);
      }
      return out;
    }
    return node;
  };
  return JSON.stringify(walk(value), null, 2) ?? "";
}

const GUTTER = "shrink-0 w-11 select-none pr-2 text-right text-[11px] leading-[1.7] tabular-nums text-faint";

const KIND_ROW: Record<DiffRow["kind"], string> = {
  same: "",
  add: "bg-ok-soft",
  del: "bg-danger-soft",
};

const KIND_SIGN: Record<DiffRow["kind"], string> = {
  same: " ",
  add: "+",
  del: "−",
};

const KIND_INK: Record<DiffRow["kind"], string> = {
  same: "text-ink",
  add: "text-ok",
  del: "text-danger",
};

/**
 * Unified diff, one row per line, with both line numbers in the gutter.
 *
 * Unified rather than side-by-side deliberately: side-by-side needs horizontal
 * room this renders inside a Modal, and the question a reader has here is "what
 * changed", not "what sits opposite what".
 */
export function DiffView({
  before,
  after,
  beforeLabel = "Before",
  afterLabel = "After",
  context,
  maxHeight = 460,
  className,
}: {
  before: string;
  after: string;
  beforeLabel?: string;
  afterLabel?: string;
  /**
   * Unchanged lines to keep around each change. Omit to show the whole text,
   * which is right for a prompt; set it for a config blob, where 200 identical
   * lines bury the two that moved.
   */
  context?: number;
  maxHeight?: number;
  className?: string;
}) {
  const rows = useMemo(() => diffLines(before, after), [before, after]);

  // Collapse long unchanged stretches into one "N unchanged lines" marker. Done
  // after the diff, not inside it, so `diffStat` stays exact.
  const shown = useMemo(() => {
    if (context === undefined) return rows.map((row) => ({ row, skipped: 0 }));
    const keep = new Array<boolean>(rows.length).fill(false);
    rows.forEach((row, index) => {
      if (row.kind === "same") return;
      for (let k = Math.max(0, index - context); k <= Math.min(rows.length - 1, index + context); k++) {
        keep[k] = true;
      }
    });
    const out: { row: DiffRow; skipped: number }[] = [];
    let run = 0;
    rows.forEach((row, index) => {
      if (keep[index]) {
        out.push({ row, skipped: run });
        run = 0;
      } else {
        run++;
      }
    });
    if (run > 0 && out.length > 0) out.push({ row: rows[rows.length - 1]!, skipped: run });
    return out;
  }, [rows, context]);

  const added = rows.filter((r) => r.kind === "add").length;
  const removed = rows.filter((r) => r.kind === "del").length;

  return (
    <div className={cx("rounded-lg border border-line bg-surface overflow-hidden", className)}>
      <div className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5">
        <div className="min-w-0 flex items-center gap-2 text-[13px] font-medium text-muted">
          <span className="truncate">{beforeLabel}</span>
          <span className="text-faint">→</span>
          <span className="truncate text-ink">{afterLabel}</span>
        </div>
        <div className="shrink-0 flex items-center gap-2 text-xs tabular-nums">
          <span className="text-ok">+{added}</span>
          <span className="text-danger">−{removed}</span>
        </div>
      </div>

      {added === 0 && removed === 0 ? (
        <p className="m-0 px-4 py-6 text-center text-[13px] text-muted">Nothing changed.</p>
      ) : (
        <div className="overflow-auto font-mono text-[13px]" style={{ maxHeight }}>
          {shown.map(({ row, skipped }, index) => (
            <div key={`${index}-${row.beforeLine}-${row.afterLine}`}>
              {skipped > 0 && (
                <div className="border-y border-line bg-canvas px-4 py-1 text-[11px] text-faint tabular-nums">
                  {skipped} unchanged {skipped === 1 ? "line" : "lines"}
                </div>
              )}
              <div className={cx("flex items-start px-2", KIND_ROW[row.kind])}>
                <span className={GUTTER} aria-hidden>{row.beforeLine ?? ""}</span>
                <span className={GUTTER} aria-hidden>{row.afterLine ?? ""}</span>
                <span className={cx("shrink-0 w-4 select-none text-center leading-[1.7]", KIND_INK[row.kind])} aria-hidden>
                  {KIND_SIGN[row.kind]}
                </span>
                <span className={cx("min-w-0 flex-1 whitespace-pre-wrap break-words leading-[1.7]", KIND_INK[row.kind])}>
                  {row.text === "" ? " " : row.text}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
