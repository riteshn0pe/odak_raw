// The thing the tenant looks at for four minutes (§7 "Crawl-in-progress").
//
// A determinate bar is a lie during a crawl — link_total grows as discovery
// proceeds, so a percentage would go backwards. So the rail is deliberately
// ProgressBar's `indeterminate` (the ca-rail keyframe), and the honest
// information is carried by the server's own message line, an absolute
// "reading page 118 of 120" position, and the pages themselves scrolling by.
//
// Presentational only: the route owns the 2 s poll (Documents.tsx:53-57's
// pattern). There is deliberately no fetch in here.
import { useEffect, useState, type ReactNode } from "react";
import type { CrawlProgressOut, SourceLinkOut } from "../lib/types";
import { Badge, ProgressBar, cx } from "./ui";
import { Icon } from "./ui/icons";

/* ------------------------------------------------- crawl-state vocabulary */

// sources.crawl_state CHECK is ('idle','discovering','fetching','extracting',
// 'awaiting_review','ready','failed','cancelled'); the first three are the
// non-terminal phases that keep the poll alive (§7 line 1186). awaiting_review
// is deliberately not one of them — every page is already fetched and no wave
// is in flight while the crawl waits for a human to approve the URL list, so
// the poll must stop. Exported because SourceCard and AddSourceModal both need
// the same answer and must not each guess.
export const CRAWLING_STATES: ReadonlySet<string> = new Set([
  "discovering",
  "fetching",
  "extracting",
]);

export function isCrawling(state: string | null | undefined): boolean {
  return state != null && CRAWLING_STATES.has(state);
}

export const CRAWL_STATE_LABEL: Record<string, string> = {
  idle: "Not crawled yet",
  discovering: "Discovering",
  fetching: "Reading pages",
  extracting: "Adding to your assistant",
  awaiting_review: "Waiting for your review",
  ready: "Trained",
  failed: "Failed",
  cancelled: "Cancelled",
};

export function crawlStateLabel(state: string): string {
  return CRAWL_STATE_LABEL[state] ?? state;
}

/* ------------------------------------------------------ per-link presentation */

type Look = { icon: (p: { size?: number; className?: string }) => ReactNode; cls: string; label: string };

// source_links.state, both schema revisions in the design doc, mapped to the
// three-icon vocabulary §7 line 1198 spells out: check/ok, x/faint, alert/danger.
const LINK_LOOK: Record<string, Look> = {
  trained: { icon: Icon.Check, cls: "text-ok", label: "Trained" },
  unchanged: { icon: Icon.Check, cls: "text-muted", label: "Unchanged" },
  fetched: { icon: Icon.Doc, cls: "text-brand", label: "Read" },
  fetching: { icon: Icon.Bolt, cls: "text-brand", label: "Reading" },
  pending: { icon: Icon.Doc, cls: "text-faint", label: "Queued" },
  queued: { icon: Icon.Doc, cls: "text-faint", label: "Queued" },
  excluded: { icon: Icon.X, cls: "text-faint", label: "Excluded" },
  stale: { icon: Icon.X, cls: "text-faint", label: "Stale" },
  gone: { icon: Icon.Alert, cls: "text-warn", label: "Gone" },
  failed: { icon: Icon.Alert, cls: "text-danger", label: "Failed" },
};

const FALLBACK_LOOK: Look = { icon: Icon.Doc, cls: "text-faint", label: "Pending" };

function look(state: string): Look {
  return LINK_LOOK[state] ?? FALLBACK_LOOK;
}

/** `crawl_failed_dns` → `crawl failed dns`. A label, never a sentence. */
function reasonLabel(reason: string): string {
  return reason.replace(/_/g, " ");
}

/**
 * Middle-out, so the tail of the path — the part that identifies the page —
 * survives. `https://acme.com/docs/very/long/thing/pricing` becomes
 * `acme.com/docs/…/thing/pricing` rather than `https://acme.com/docs/very/lo…`.
 */
export function midTruncate(value: string, max = 56): string {
  if (value.length <= max) return value;
  // Bias to the tail: the last segment is what the tenant recognises.
  const head = Math.max(Math.floor((max - 1) * 0.38), 4);
  const tail = max - 1 - head;
  return `${value.slice(0, head)}…${value.slice(value.length - tail)}`;
}

/** Strip the scheme and any `www.`, keep host + path + query. */
export function prettyUrl(url: string): string {
  const bare = url.replace(/^https?:\/\//i, "").replace(/^www\./i, "");
  return bare.length > 1 ? bare.replace(/\/$/, "") : bare;
}

/* ------------------------------------------------------------------ component */

export function LinkTicker({
  progress,
  pollingPaused,
  maxRows = 6,
  className,
}: {
  progress: CrawlProgressOut;
  /** The route stopped polling (tab hidden, error backoff). Said out loud rather than faked. */
  pollingPaused?: boolean;
  maxRows?: number;
  className?: string;
}) {
  // Accumulated locally so a row survives falling out of the server's
  // ten-transition window, and hard-capped so a 10,000-page crawl cannot grow
  // the DOM. Keyed by link id: React moves an existing row instead of
  // remounting it, which is exactly why ca-rise plays once, on arrival only.
  const [rows, setRows] = useState<SourceLinkOut[]>([]);
  const recent = progress.recent;

  useEffect(() => {
    if (recent.length === 0) return;
    setRows((prev) => {
      const incoming = new Set(recent.map((l) => l.id));
      return [...recent, ...prev.filter((l) => !incoming.has(l.id))].slice(0, maxRows);
    });
  }, [recent, maxRows]);

  const total = progress.link_total;
  const resolved = progress.link_trained + progress.link_failed + progress.link_excluded;
  const position =
    total <= 0
      ? "finding pages…"
      : progress.link_pending > 0
        ? `reading page ${Math.min(resolved + 1, total)} of ${total}`
        : `${resolved} of ${total} pages read`;

  const live = isCrawling(progress.crawl_state);

  return (
    <div className={cx("flex flex-col gap-2.5", className)}>
      <ProgressBar
        indeterminate
        size="sm"
        value={resolved}
        max={total > 0 ? total : 100}
        label={
          <span className="flex min-w-0 items-center gap-1.5">
            {live && (
              // ca-breathe on the dot, so a settling card still reads as "working"
              // at a glance. Decoration on top of copy that already says it.
              <span
                aria-hidden
                className={cx(
                  "h-1.5 w-1.5 shrink-0 rounded-full bg-brand",
                  !pollingPaused && "animate-[ca-breathe_1.8s_ease-in-out_infinite] motion-reduce:animate-none",
                )}
              />
            )}
            {/* Server-computed copy, so the sentence is identical everywhere it
                appears. Never reassembled here from the counters. */}
            <span className="min-w-0 truncate">{progress.message || crawlStateLabel(progress.crawl_state)}</span>
            {pollingPaused === true && <Badge tone="neutral">paused</Badge>}
          </span>
        }
        sub={<span className="tabular-nums">{position}</span>}
      />

      {progress.error != null && progress.error !== "" && (
        <p className="text-[12px] text-danger">{progress.error}</p>
      )}

      {rows.length > 0 && (
        // Not an aria-live region on purpose: six rows replaced every two
        // seconds would be an unreadable stream of announcements. The rail's
        // role="progressbar" and the message line carry the state for AT.
        <ul aria-label="Recently crawled pages" className="flex flex-col gap-1">
          {rows.map((link) => {
            const l = look(link.state);
            const Ico = l.icon;
            const badge =
              link.http_status != null && link.http_status >= 400
                ? String(link.http_status)
                : link.reason != null && link.reason !== ""
                  ? reasonLabel(link.reason)
                  : l.label;
            return (
              <li
                key={link.id}
                className="flex items-start gap-2 animate-[ca-rise_200ms_cubic-bezier(0,0,.2,1)] motion-reduce:animate-none"
              >
                <Ico size={14} className={cx("mt-[3px] shrink-0", l.cls)} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-mono text-[12px] text-ink" title={link.url}>
                    {midTruncate(prettyUrl(link.url))}
                  </span>
                  {link.reason_detail != null && link.reason_detail !== "" && (
                    <span className="block truncate text-[11px] text-faint" title={link.reason_detail}>
                      {link.reason_detail}
                    </span>
                  )}
                </span>
                <span className={cx("shrink-0 pt-[2px] text-[11px] font-medium tabular-nums", l.cls)}>
                  {badge}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
