// Read-only markdown preview for extracted page content (crawler §7).
//
// SECURITY — read this before changing anything below.
// The input is markdown extracted from a *third-party* web page that a tenant
// pointed a crawler at. It is untrusted, attacker-influenceable text arriving in
// a multi-tenant dashboard. Therefore:
//
//   1. This file contains no `dangerouslySetInnerHTML`, and must never contain
//      one. Every construct is parsed into React elements, so anything the parser
//      does not recognise — including a literal `<script>` tag or an `onerror=`
//      attribute — lands in a text node and is displayed, never executed.
//   2. Link hrefs are passed through `safeHref`, which admits only http, https
//      and mailto. `javascript:` and `data:` URLs render as inert text.
//
// It is also dependency-free on purpose: FRONTEND_SPEC.md's zero-dependency claim
// is itself a trust surface, so the renderer is hand-rolled over the subset that
// crawl extraction actually emits (headings, paragraphs, emphasis, inline code,
// fenced code, lists, blockquotes, links, rules, pipe tables).
import { type ReactNode, useMemo } from "react";
import { cx } from "./ui";

/* -------------------------------------------------------------------------- */
/* Inline                                                                      */
/* -------------------------------------------------------------------------- */

/**
 * Only web-navigable schemes survive. A relative or root-relative path is kept
 * (crawled pages are full of them) but rendered without a working origin, which
 * is honest: the preview is not a browser.
 */
function safeHref(raw: string): string | null {
  const href = raw.trim();
  if (href === "") return null;
  if (/^(https?:|mailto:)/i.test(href)) return href;
  // Scheme-relative and path-relative links have no colon before the first
  // slash; anything else carrying a colon is some other scheme and is dropped.
  if (/^(\/|#|\.)/.test(href) && !/^[a-z][a-z0-9+.-]*:/i.test(href)) return href;
  return null;
}

// One alternation, walked left to right. Ordered longest-delimiter-first so that
// `**` is never mistaken for two `*`.
const INLINE =
  /(`+)([\s\S]*?)\1|!\[([^\]]*)\]\(([^)\s]*)(?:\s+"[^"]*")?\)|\[([^\]]*)\]\(([^)\s]*)(?:\s+"[^"]*")?\)|(\*\*|__)(?=\S)([\s\S]*?\S)\7|(\*|_)(?=\S)([\s\S]*?\S)\9|(~~)(?=\S)([\s\S]*?\S)\11|<((?:https?:\/\/|mailto:)[^>\s]+)>/g;

const CODE_INLINE =
  "rounded bg-sunken border border-line px-1 py-px font-mono text-[0.85em] text-ink";
const LINK_INLINE = "text-brand underline decoration-brand/40 hover:decoration-brand break-words";

/**
 * Parses emphasis, code spans, links and autolinks into React nodes.
 *
 * `key` is derived from the match offset, which is stable for a given input and
 * unique within one call, so React never has to guess about reordering.
 */
function inline(text: string, keyPrefix: string): ReactNode[] {
  const out: ReactNode[] = [];
  let cursor = 0;
  // A fresh matcher per call. `inline` recurses into link labels and emphasis
  // bodies, and a shared /g/ regex carries `lastIndex` across those nested
  // calls, which silently eats the tail of the outer string.
  const re = new RegExp(INLINE.source, "g");

  for (let m = re.exec(text); m !== null; m = re.exec(text)) {
    if (m.index > cursor) out.push(text.slice(cursor, m.index));
    const k = `${keyPrefix}-${m.index}`;

    if (m[1] !== undefined) {
      // Code span: contents are never re-parsed, per CommonMark.
      out.push(
        <code key={k} className={CODE_INLINE}>
          {m[2]?.replace(/^ (.*) $/, "$1") ?? ""}
        </code>,
      );
    } else if (m[4] !== undefined) {
      // Image. Remote images are NOT fetched: a preview that loads assets from a
      // crawled origin leaks the viewer's IP to that origin and lets it track
      // dashboard opens. The alt text stands in for it instead.
      const alt = m[3] ?? "";
      out.push(
        <span key={k} className="text-faint italic">
          {alt === "" ? "[image]" : `[image: ${alt}]`}
        </span>,
      );
    } else if (m[6] !== undefined) {
      const href = safeHref(m[6]);
      const label = m[5] === "" ? m[6] : (m[5] ?? "");
      out.push(
        href === null ? (
          <span key={k}>{label}</span>
        ) : (
          <a
            key={k}
            href={href}
            target="_blank"
            rel="noopener noreferrer nofollow"
            className={LINK_INLINE}
          >
            {inline(label, k)}
          </a>
        ),
      );
    } else if (m[7] !== undefined) {
      out.push(
        <strong key={k} className="font-semibold text-ink">
          {inline(m[8] ?? "", k)}
        </strong>,
      );
    } else if (m[9] !== undefined) {
      out.push(<em key={k}>{inline(m[10] ?? "", k)}</em>);
    } else if (m[11] !== undefined) {
      out.push(
        <span key={k} className="line-through text-faint">
          {inline(m[12] ?? "", k)}
        </span>,
      );
    } else if (m[13] !== undefined) {
      const href = safeHref(m[13]);
      out.push(
        href === null ? (
          <span key={k}>{m[13]}</span>
        ) : (
          <a
            key={k}
            href={href}
            target="_blank"
            rel="noopener noreferrer nofollow"
            className={LINK_INLINE}
          >
            {m[13]}
          </a>
        ),
      );
    }

    cursor = m.index + m[0].length;
    // A zero-length match would spin forever; the alternation cannot produce one
    // today, but the guard costs nothing and outlives the assumption.
    if (m[0].length === 0) re.lastIndex += 1;
  }

  if (cursor < text.length) out.push(text.slice(cursor));
  return out;
}

/* -------------------------------------------------------------------------- */
/* Blocks                                                                     */
/* -------------------------------------------------------------------------- */

const HEADING_SIZE: Record<number, string> = {
  1: "text-[19px] font-semibold text-ink mt-5 mb-2 first:mt-0 leading-snug",
  2: "text-[17px] font-semibold text-ink mt-5 mb-2 first:mt-0 leading-snug",
  3: "text-[15px] font-semibold text-ink mt-4 mb-1.5 first:mt-0",
  4: "text-[14px] font-semibold text-ink mt-4 mb-1.5 first:mt-0",
  5: "text-[13px] font-semibold text-muted mt-3 mb-1 first:mt-0 uppercase tracking-wide",
  6: "text-[12px] font-semibold text-faint mt-3 mb-1 first:mt-0 uppercase tracking-wide",
};

const RE_FENCE = /^\s{0,3}(```+|~~~+)\s*([^\s`]*)/;
const RE_HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RE_RULE = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
const RE_QUOTE = /^\s{0,3}>\s?(.*)$/;
const RE_UL = /^(\s*)([-*+])\s+(.*)$/;
const RE_OL = /^(\s*)(\d{1,9})[.)]\s+(.*)$/;
const RE_TABLE_DIVIDER = /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/;

/** Splits a pipe-table row, tolerating the optional leading/trailing pipes. */
function tableCells(line: string): string[] {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  const cells: string[] = [];
  let buf = "";
  for (let i = 0; i < trimmed.length; i += 1) {
    const ch = trimmed[i] ?? "";
    if (ch === "\\" && trimmed[i + 1] === "|") {
      buf += "|";
      i += 1;
    } else if (ch === "|") {
      cells.push(buf.trim());
      buf = "";
    } else {
      buf += ch;
    }
  }
  cells.push(buf.trim());
  return cells;
}

function alignOf(spec: string): "left" | "center" | "right" {
  const s = spec.trim();
  if (s.startsWith(":") && s.endsWith(":")) return "center";
  if (s.endsWith(":")) return "right";
  return "left";
}

function indentOf(line: string): number {
  const m = /^(\s*)/.exec(line);
  return (m?.[1] ?? "").replace(/\t/g, "    ").length;
}

function startsBlock(line: string): boolean {
  return (
    RE_FENCE.test(line) ||
    RE_HEADING.test(line) ||
    RE_RULE.test(line) ||
    RE_QUOTE.test(line) ||
    RE_UL.test(line) ||
    RE_OL.test(line)
  );
}

/**
 * A list item's body may be a whole document (nested list, fenced code) or a
 * single run of prose. Recursing unconditionally would wrap every one-line
 * bullet in a `<p>` and space the list out like a loose list, so prose items are
 * rendered inline and only structured ones recurse.
 */
function renderItem(text: string, key: string): ReactNode[] {
  const lines = text.split("\n");
  const structured =
    /\n[ \t]*\n/.test(text) || lines.some((l, i) => i > 0 && l.trim() !== "" && startsBlock(l));
  if (structured) return renderBlocks(text, key);
  return inline(text.replace(/\s*\n\s*/g, " ").trim(), key);
}

/**
 * The block-level walker. Deliberately a hand-written line scanner rather than a
 * two-pass AST: the supported grammar is small, and one pass keeps the whole
 * renderer auditable in a single read.
 */
function renderBlocks(src: string, keyPrefix: string): ReactNode[] {
  const lines = src.replace(/\r\n?/g, "\n").split("\n");
  const out: ReactNode[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i] ?? "";
    const k = `${keyPrefix}:${i}`;

    if (line.trim() === "") {
      i += 1;
      continue;
    }

    // ---- fenced code -----------------------------------------------------
    const fence = RE_FENCE.exec(line);
    if (fence) {
      const marker = fence[1] ?? "```";
      const lang = fence[2] ?? "";
      const body: string[] = [];
      i += 1;
      while (i < lines.length) {
        const cur = lines[i] ?? "";
        if (new RegExp(`^\\s{0,3}${marker[0] === "`" ? "`{3,}" : "~{3,}"}\\s*$`).test(cur)) {
          i += 1;
          break;
        }
        body.push(cur);
        i += 1;
      }
      out.push(
        <div key={k} className="my-3 overflow-hidden rounded-lg border border-line bg-sunken">
          {lang !== "" && (
            <div className="border-b border-line px-3 py-1 font-mono text-[11px] uppercase tracking-wide text-faint">
              {lang}
            </div>
          )}
          <pre className="overflow-x-auto px-3 py-2 font-mono text-[12.5px] leading-relaxed text-ink">
            <code>{body.join("\n")}</code>
          </pre>
        </div>,
      );
      continue;
    }

    // ---- horizontal rule (checked before lists: `- - -` is not a bullet) --
    if (RE_RULE.test(line)) {
      out.push(<hr key={k} className="my-4 border-0 border-t border-line" />);
      i += 1;
      continue;
    }

    // ---- ATX heading -----------------------------------------------------
    const heading = RE_HEADING.exec(line);
    if (heading) {
      const level = (heading[1] ?? "#").length;
      const Tag = (`h${level}` as "h1");
      out.push(
        <Tag key={k} className={HEADING_SIZE[level] ?? HEADING_SIZE[6]}>
          {inline(heading[2] ?? "", k)}
        </Tag>,
      );
      i += 1;
      continue;
    }

    // ---- blockquote ------------------------------------------------------
    if (RE_QUOTE.test(line)) {
      const body: string[] = [];
      while (i < lines.length) {
        const q = RE_QUOTE.exec(lines[i] ?? "");
        if (q) {
          body.push(q[1] ?? "");
          i += 1;
          continue;
        }
        // Lazy continuation: an unmarked prose line still belongs to the quote.
        const cur = lines[i] ?? "";
        if (cur.trim() !== "" && !startsBlock(cur)) {
          body.push(cur.trim());
          i += 1;
          continue;
        }
        break;
      }
      out.push(
        <blockquote
          key={k}
          className="my-3 border-l-2 border-line pl-3 text-muted [&>*:first-child]:mt-0 [&>*:last-child]:mb-0"
        >
          {renderBlocks(body.join("\n"), k)}
        </blockquote>,
      );
      continue;
    }

    // ---- lists -----------------------------------------------------------
    const ul = RE_UL.exec(line);
    const ol = RE_OL.exec(line);
    if (ul || ol) {
      const ordered = ol !== null && ul === null;
      const base = indentOf(line);
      const items: string[] = [];
      let start = ordered ? Number(ol?.[2] ?? 1) : 1;
      let current: string[] | null = null;

      while (i < lines.length) {
        const cur = lines[i] ?? "";
        const curUl = RE_UL.exec(cur);
        const curOl = RE_OL.exec(cur);
        const isItem = curUl !== null || curOl !== null;
        const ind = indentOf(cur);

        if (cur.trim() === "") {
          // A blank line only continues the list if indented content follows.
          const next = lines[i + 1] ?? "";
          const nextItem = RE_UL.test(next) || RE_OL.test(next);
          if (next.trim() !== "" && (indentOf(next) > base || (nextItem && indentOf(next) >= base))) {
            current?.push("");
            i += 1;
            continue;
          }
          break;
        }

        if (isItem && ind <= base) {
          // A different marker family at this level starts a *new* list, so the
          // ordered/unordered switch is not silently swallowed.
          const curOrdered = curOl !== null && curUl === null;
          if (curOrdered !== ordered) break;
          const marker = curUl ?? curOl;
          if (current) items.push(current.join("\n"));
          current = [marker?.[3] ?? ""];
          if (ordered && items.length === 0) start = Number(curOl?.[2] ?? 1);
          i += 1;
          continue;
        }

        if (ind > base) {
          // Nested content: dedent by the parent's content offset so the
          // recursive pass sees it at column zero.
          const strip = Math.min(ind, base + 2);
          current?.push(cur.replace(/\t/g, "    ").slice(strip));
          i += 1;
          continue;
        }

        if (!startsBlock(cur) && current !== null) {
          current.push(cur.trim());
          i += 1;
          continue;
        }
        break;
      }
      if (current) items.push(current.join("\n"));

      const rendered = items.map((body, n) => (
        <li key={`${k}-li-${n}`} className="pl-0.5 [&>*:first-child]:mt-0 [&>*:last-child]:mb-0">
          {renderItem(body, `${k}-li-${n}`)}
        </li>
      ));
      out.push(
        ordered ? (
          <ol key={k} start={start} className="my-2.5 ml-5 list-decimal space-y-1 marker:text-faint">
            {rendered}
          </ol>
        ) : (
          <ul key={k} className="my-2.5 ml-5 list-disc space-y-1 marker:text-faint">
            {rendered}
          </ul>
        ),
      );
      continue;
    }

    // ---- pipe table ------------------------------------------------------
    if (line.includes("|") && RE_TABLE_DIVIDER.test(lines[i + 1] ?? "")) {
      const header = tableCells(line);
      const aligns = tableCells(lines[i + 1] ?? "").map(alignOf);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length) {
        const cur = lines[i] ?? "";
        if (cur.trim() === "" || !cur.includes("|")) break;
        rows.push(tableCells(cur));
        i += 1;
      }
      out.push(
        <div key={k} className="my-3 overflow-x-auto rounded-lg border border-line">
          <table className="w-full border-collapse text-[13px]">
            <thead className="bg-sunken">
              <tr>
                {header.map((cell, c) => (
                  <th
                    key={`${k}-h-${c}`}
                    className="border-b border-line px-2.5 py-1.5 font-semibold text-muted"
                    style={{ textAlign: aligns[c] ?? "left" }}
                  >
                    {inline(cell, `${k}-h-${c}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, r) => (
                <tr key={`${k}-r-${r}`} className="border-b border-line last:border-0">
                  {header.map((_, c) => (
                    <td
                      key={`${k}-r-${r}-${c}`}
                      className="px-2.5 py-1.5 align-top text-ink"
                      style={{ textAlign: aligns[c] ?? "left" }}
                    >
                      {inline(row[c] ?? "", `${k}-r-${r}-${c}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }

    // ---- paragraph -------------------------------------------------------
    const para: string[] = [line.trim()];
    i += 1;
    while (i < lines.length) {
      const cur = lines[i] ?? "";
      if (cur.trim() === "" || startsBlock(cur)) break;
      if (cur.includes("|") && RE_TABLE_DIVIDER.test(lines[i + 1] ?? "")) break;
      para.push(cur.trim());
      i += 1;
    }
    out.push(
      <p key={k} className="my-2 leading-relaxed text-ink first:mt-0 last:mb-0">
        {inline(para.join(" "), k)}
      </p>,
    );
  }

  return out;
}

/* -------------------------------------------------------------------------- */
/* Component                                                                  */
/* -------------------------------------------------------------------------- */

export interface MarkdownPreviewProps {
  /** The markdown to render. */
  value?: string | null;
  /** Alias for `value`, for call sites that read better as `markdown={…}`. */
  markdown?: string | null;
  /**
   * Caps the body height and scrolls instead of growing. A number is px.
   * Omit it and the block grows to its content, which is what a modal wants.
   */
  maxHeight?: number | string;
  /** Shown when there is no body yet — a degraded state, not an empty box. */
  emptyLabel?: string;
  className?: string;
}

/**
 * Renders extracted page markdown as React elements. Never as HTML — see the
 * security note at the top of this file.
 *
 * The body sits on `--color-sunken` so it reads as quoted, machine-produced
 * content rather than as dashboard chrome: the tenant is looking at what the
 * crawler kept, and the recessed surface is the signal that this is *their*
 * page's text and not ours.
 */
export function MarkdownPreview({
  value,
  markdown,
  maxHeight,
  emptyLabel = "No extracted text yet.",
  className,
}: MarkdownPreviewProps) {
  const source = (value ?? markdown ?? "").toString();
  const blocks = useMemo(
    () => (source.trim() === "" ? null : renderBlocks(source, "md")),
    [source],
  );

  if (blocks === null) {
    return (
      <div
        className={cx(
          "rounded-lg border border-line bg-sunken px-3 py-2.5 text-[13px] text-faint",
          className,
        )}
      >
        {emptyLabel}
      </div>
    );
  }

  return (
    <div
      className={cx(
        "rounded-lg border border-line bg-sunken px-3.5 py-3 text-[13.5px] text-ink",
        maxHeight === undefined ? "" : "overflow-y-auto",
        className,
      )}
      style={maxHeight === undefined ? undefined : { maxHeight }}
    >
      {blocks}
    </div>
  );
}

export default MarkdownPreview;
