/**
 * The discovered-links tree: every URL a crawl found, grouped by host and path,
 * every one of them checked. The tenant unchecks what does not belong.
 *
 * Why included-by-default. A crawl that found 791 pages has already done the work
 * of deciding what is on the site; asking a person to re-type the paths they want
 * is asking them to do it again by hand, worse. So the list arrives complete and
 * the only action is subtraction — which is also the cheap direction, because
 * nothing here has been embedded yet.
 *
 * Selection is expressed as the set of *excluded* URLs rather than included ones:
 * an empty set means "index everything", which is both the common case and the
 * one that stays correct when the crawl discovers more pages behind our back.
 *
 * The tree itself comes from the server already grouped (`crawl.group_urls`), so
 * a section header's count and the count the crawl acts on cannot disagree. This
 * component adds no grouping of its own — only checkboxes, search and order.
 */
import { useEffect, useMemo, useState } from "react";

import type { ReviewGroupOut, ReviewLinkOut } from "../lib/types";
import { Button, Input, cx } from "./ui";
import { Icon } from "./ui/icons";

// a search that matches four thousand URLs is not a search result, it is the list
// again. matches past this are counted, not drawn
const SEARCH_DRAW_CAP = 300;

// one node can hold hundreds of leaf URLs (the server only splits a bucket that
// is both large and shallow), so a node opens showing this many and offers the rest
const LEAF_DRAW_CAP = 100;

// hosts are open on arrival only when there are few enough for that to be
// readable; past this the tenant opens the one they care about
const AUTO_OPEN_HOSTS = 3;

const CHECKBOX =
  "h-4 w-4 shrink-0 cursor-pointer accent-[var(--color-brand)] focus:outline-none focus-visible:ring-4 focus-visible:ring-brand-soft disabled:cursor-not-allowed disabled:opacity-40";

const NOTHING_EXCLUDED: ReadonlySet<string> = new Set<string>();

/** Every URL beneath a node, the node's own included. */
function urlsUnder(group: ReviewGroupOut): string[] {
  return [...group.urls, ...group.children.flatMap(urlsUnder)];
}

/** `https://x.com/a/b?c=1` → `/a/b?c=1`, falling back to the whole URL. */
function pathOf(url: string): string {
  try {
    const u = new URL(url);
    return `${u.pathname}${u.search}` || "/";
  } catch {
    return url;
  }
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** Alphabetical order, applied all the way down. The server's own order is by count. */
function sortDeep(groups: ReviewGroupOut[]): ReviewGroupOut[] {
  return [...groups]
    .sort((a, b) => a.label.localeCompare(b.label))
    .map((g) => ({ ...g, urls: [...g.urls].sort(), children: sortDeep(g.children) }));
}

/**
 * A checkbox that can also be neither on nor off. `indeterminate` is a DOM
 * property with no attribute behind it, so React cannot set it from JSX and it
 * has to be written on the node itself.
 */
function TriCheckbox({
  checked,
  indeterminate,
  disabled,
  label,
  onChange,
}: {
  checked: boolean;
  indeterminate?: boolean;
  disabled?: boolean;
  label: string;
  onChange: (next: boolean) => void;
}) {
  return (
    <input
      type="checkbox"
      checked={checked}
      ref={(el) => {
        if (el) el.indeterminate = !checked && !!indeterminate;
      }}
      disabled={disabled}
      aria-label={label}
      onChange={(e) => onChange(e.target.checked)}
      onClick={(e) => e.stopPropagation()}
      className={CHECKBOX}
    />
  );
}

/**
 * Everything a row needs that is not the row itself. Passed down rather than
 * closed over so `UrlRow` and `TreeNode` can live at module scope: a component
 * declared inside a render is a new type on every render, which remounts the
 * subtree and takes the focused checkbox with it.
 */
interface TreeCtx {
  excluded: ReadonlySet<string>;
  disabled?: boolean;
  expanded: ReadonlySet<string>;
  drawAll: ReadonlySet<string>;
  under: Map<string, string[]>;
  toggleOpen: (key: string) => void;
  showAll: (key: string) => void;
  setIncluded: (urls: string[], on: boolean) => void;
}

function UrlRow({ url, depth, ctx }: { url: string; depth: number; ctx: TreeCtx }) {
  const on = !ctx.excluded.has(url);
  return (
    <div
      className="flex items-center gap-2 py-1.5 pr-3 transition-colors hover:bg-canvas"
      style={{ paddingLeft: 12 + depth * 18 }}
    >
      <TriCheckbox
        checked={on}
        disabled={ctx.disabled}
        label={`Include ${url}`}
        onChange={(next) => ctx.setIncluded([url], next)}
      />
      <Icon.Link size={13} className="shrink-0 text-faint" />
      <span
        className={cx(
          "min-w-0 flex-1 truncate font-mono text-[13px]",
          on ? "text-ink" : "text-faint line-through",
        )}
        title={url}
      >
        {pathOf(url)}
      </span>
    </div>
  );
}

/**
 * One node: its own checkbox over everything beneath it, its own disclosure, and
 * a count that says how much of it is still in.
 */
function TreeNode({ group, depth, ctx }: { group: ReviewGroupOut; depth: number; ctx: TreeCtx }) {
  const urls = ctx.under.get(group.key) ?? urlsUnder(group);
  const off = urls.reduce((n, url) => n + (ctx.excluded.has(url) ? 1 : 0), 0);
  const open = ctx.expanded.has(group.key);
  const leaves = ctx.drawAll.has(group.key) ? group.urls : group.urls.slice(0, LEAF_DRAW_CAP);
  const hidden = group.urls.length - leaves.length;

  return (
    <div>
      <div
        className="flex items-center gap-2 py-2 pr-3 transition-colors hover:bg-canvas"
        style={{ paddingLeft: 12 + depth * 18 }}
      >
        <TriCheckbox
          checked={off === 0}
          indeterminate={off > 0 && off < urls.length}
          disabled={ctx.disabled}
          label={`Include every page under ${group.label}`}
          onChange={(next) => ctx.setIncluded(urls, next)}
        />
        <button
          type="button"
          onClick={() => ctx.toggleOpen(group.key)}
          aria-expanded={open}
          className="flex min-w-0 flex-1 items-center gap-2 text-left"
        >
          <span className={cx("shrink-0 text-faint transition-transform", open && "rotate-180")}>
            <Icon.Chevron size={15} />
          </span>
          {depth === 0 && <Icon.Globe size={14} className="shrink-0 text-faint" />}
          <span
            className={cx(
              "min-w-0 truncate font-medium",
              depth === 0 ? "text-sm text-ink" : "font-mono text-[13px] text-ink",
            )}
            title={group.label}
          >
            {group.label}
          </span>
          <span className="shrink-0 text-xs tabular-nums text-muted">
            {off > 0 ? `${urls.length - off}/${urls.length}` : urls.length}
          </span>
        </button>
      </div>
      {open && (
        <div className="border-t border-line">
          {group.children.map((child) => (
            <TreeNode key={child.key} group={child} depth={depth + 1} ctx={ctx} />
          ))}
          {leaves.map((url) => (
            <UrlRow key={url} url={url} depth={depth + 1} ctx={ctx} />
          ))}
          {hidden > 0 && (
            <div style={{ paddingLeft: 12 + (depth + 1) * 18 }} className="py-1.5">
              <button
                type="button"
                onClick={() => ctx.showAll(group.key)}
                className="text-xs text-brand hover:underline"
              >
                Show {plural(hidden, "more page", "more pages")}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function LinkTree({
  groups,
  links,
  excluded = NOTHING_EXCLUDED,
  onExcludedChange,
  disabled,
  emptyMessage = "No pages were found.",
  className,
}: {
  groups: ReviewGroupOut[];
  /** Used for title matching in search. The tree itself needs only `groups`. */
  links?: ReviewLinkOut[];
  /** URLs the tenant has unchecked. Empty means everything is included. */
  excluded?: ReadonlySet<string>;
  onExcludedChange: (next: Set<string>) => void;
  disabled?: boolean;
  emptyMessage?: string;
  className?: string;
}) {
  const [query, setQuery] = useState("");
  const [alpha, setAlpha] = useState(false);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(NOTHING_EXCLUDED);
  const [drawAll, setDrawAll] = useState<ReadonlySet<string>>(NOTHING_EXCLUDED);

  // seed the disclosure state from the tree rather than from a render count, so a
  // list that arrives late (the usual case — it arrives with the crawl) still opens
  useEffect(() => {
    setExpanded(
      new Set(groups.length <= AUTO_OPEN_HOSTS ? groups.map((g) => g.key) : []),
    );
    setDrawAll(new Set<string>());
  }, [groups]);

  const shown = useMemo(() => (alpha ? sortDeep(groups) : groups), [groups, alpha]);

  // node key → every URL under it, computed once per tree so a toggle three levels
  // deep does not walk the whole thing again
  const under = useMemo(() => {
    const map = new Map<string, string[]>();
    const walk = (list: ReviewGroupOut[]) => {
      for (const g of list) {
        map.set(g.key, urlsUnder(g));
        walk(g.children);
      }
    };
    walk(groups);
    return map;
  }, [groups]);

  const allUrls = useMemo(() => groups.flatMap(urlsUnder), [groups]);
  const total = allUrls.length;
  const excludedHere = useMemo(
    () => allUrls.reduce((n, url) => n + (excluded.has(url) ? 1 : 0), 0),
    [allUrls, excluded],
  );
  const included = total - excludedHere;

  const titleByUrl = useMemo(() => {
    const map = new Map<string, string>();
    for (const link of links ?? []) if (link.title) map.set(link.url, link.title);
    return map;
  }, [links]);

  const matches = useMemo<string[]>(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return [];
    return allUrls.filter(
      (url) =>
        url.toLowerCase().includes(needle) ||
        (titleByUrl.get(url) ?? "").toLowerCase().includes(needle),
    );
  }, [allUrls, query, titleByUrl]);

  function setIncluded(urls: string[], on: boolean) {
    if (disabled || !urls.length) return;
    const next = new Set(excluded);
    for (const url of urls) {
      if (on) next.delete(url);
      else next.add(url);
    }
    onExcludedChange(next);
  }

  function toggleOpen(key: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  const ctx: TreeCtx = {
    excluded,
    disabled,
    expanded,
    drawAll,
    under,
    toggleOpen,
    showAll: (key) => setDrawAll((prev) => new Set(prev).add(key)),
    setIncluded,
  };

  return (
    <div className={cx("space-y-3", className)}>
      <div className="flex items-end justify-between gap-3">
        <div className="min-w-0">
          <p className="m-0 text-sm font-semibold text-ink">
            {plural(included, "link included", "links included")}
            {excludedHere > 0 && (
              <span className="font-normal text-muted"> · {excludedHere} excluded</span>
            )}
          </p>
          <p className="m-0 mt-0.5 text-xs text-muted">
            Uncheck the links you want to exclude.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {excludedHere > 0 && (
            <Button
              variant="ghost"
              size="sm"
              disabled={disabled}
              onClick={() => onExcludedChange(new Set<string>())}
            >
              Select all
            </Button>
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setAlpha((v) => !v)}
            title={alpha ? "Sorted A–Z" : "Sorted by page count"}
          >
            <Icon.Selector size={14} />
            {alpha ? "A–Z" : "Most pages"}
          </Button>
        </div>
      </div>

      <div className="relative">
        <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-faint">
          <Icon.Search size={15} />
        </span>
        <Input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search links"
          className="pl-8"
        />
      </div>

      <div className="max-h-[46vh] overflow-y-auto rounded-xl border border-line bg-surface">
        {total === 0 ? (
          <p className="m-0 py-8 text-center text-sm text-muted">{emptyMessage}</p>
        ) : query.trim() ? (
          matches.length === 0 ? (
            <p className="m-0 py-8 text-center text-sm text-muted">
              No link matches “{query.trim()}”.
            </p>
          ) : (
            <>
              {matches.slice(0, SEARCH_DRAW_CAP).map((url) => (
                <div key={url} className="border-b border-line last:border-0">
                  <UrlRow url={url} depth={0} ctx={ctx} />
                </div>
              ))}
              {matches.length > SEARCH_DRAW_CAP && (
                <p className="m-0 px-3 py-2 text-xs text-muted">
                  {matches.length - SEARCH_DRAW_CAP} more matches. Narrow the search to see them.
                </p>
              )}
            </>
          )
        ) : (
          shown.map((group) => (
            <div key={group.key} className="border-b border-line last:border-0">
              <TreeNode group={group} depth={0} ctx={ctx} />
            </div>
          ))
        )}
      </div>
    </div>
  );
}
