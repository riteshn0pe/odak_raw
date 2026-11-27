// One card per source. A website is a five-metric object with a live progress
// state, which is why the list is cards and not a DataTable (§7 line 1131).
//
// The two rules this card exists to enforce:
//   1. A crawl in flight shows absolute numbers and the pages themselves going
//      by (LinkTicker), never a percentage that would go backwards.
//   2. A config edit is stored, never crawled. PATCH comes back with
//      pending_changes: true and the card then *offers* Retrain. It does not
//      start one.
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { tenantHeaders } from "../lib/auth";
import { bytes, relativeTime } from "../lib/format";
import type { CrawlConfigIn, CrawlProgressOut, SourceOut } from "../lib/types";
import {
  CrawlConfigFields,
  RETRAIN_WEEKLY_HOURS,
  SOURCE_TITLE_MAX,
  SourceMetaFields,
  defaultCrawlConfig,
  explainSourceFailure,
} from "./AddSourceModal";
import { ConfirmDialog } from "./ConfirmDialog";
import { LinkTicker, isCrawling } from "./LinkTicker";
import { ReviewGateModal } from "./ReviewGateModal";
import { StatusBadge } from "./StatusBadge";
import { Alert, Badge, Banner, Button, Card, Field, Input, Menu, ToggleRow, cx, type MenuItem } from "./ui";
import { Icon } from "./ui/icons";
import { SlideOver } from "./ui/SlideOver";

/* ------------------------------------------------------------------- kinds */

const KIND: Record<string, { label: string; icon: typeof Icon.Doc }> = {
  crawl: { label: "Website", icon: Icon.Globe },
  sitemap: { label: "Sitemap", icon: Icon.Globe },
  links: { label: "Links", icon: Icon.Link },
  snippet: { label: "Text", icon: Icon.Book },
  qa: { label: "Q&A", icon: Icon.Chat },
  file: { label: "File", icon: Icon.Doc },
};

const WEBSITE_KINDS = new Set(["crawl", "sitemap", "links"]);

/** sources.config is a jsonb blob; read it back into the typed shape defensively. */
function readCrawlConfig(raw: Record<string, unknown>): CrawlConfigIn {
  const base = defaultCrawlConfig();
  const paths = (value: unknown): string[] | null =>
    Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === "string") : null;
  return {
    include_paths: paths(raw.include_paths) ?? base.include_paths,
    exclude_paths: paths(raw.exclude_paths) ?? base.exclude_paths,
    max_pages: typeof raw.max_pages === "number" ? raw.max_pages : null,
    max_depth: typeof raw.max_depth === "number" ? raw.max_depth : base.max_depth,
    include_subdomains: raw.include_subdomains === true,
    include_pdf: raw.include_pdf === true,
    review_before_index: raw.review_before_index === true,
  };
}

/**
 * The route may not have polled yet. Every counter LinkTicker needs is already
 * on SourceOut, so the rail renders from the row itself rather than waiting —
 * with an empty `message`, which LinkTicker falls back to a state label for
 * rather than inventing a sentence.
 */
function progressFromSource(source: SourceOut): CrawlProgressOut {
  return {
    crawl_state: source.crawl_state,
    link_total: source.link_total,
    link_trained: source.link_trained,
    link_failed: source.link_failed,
    link_excluded: source.link_excluded,
    link_pending: source.link_pending,
    size_bytes: source.size_bytes,
    error: source.error,
    recent: [],
    message: "",
  };
}

/* --------------------------------------------------------------------- card */

export function SourceCard({
  source,
  progress,
  pollingPaused,
  documentId,
  collections = [],
  onChanged,
  onDeleted,
  className,
}: {
  source: SourceOut;
  /** Fed from the route's 2 s poll. Absent is fine — the card falls back to the row. */
  progress?: CrawlProgressOut | null;
  pollingPaused?: boolean;
  /** Set when this source maps to a single document, for the child-page link. */
  documentId?: string | null;
  collections?: string[];
  onChanged?: (next: SourceOut) => void;
  onDeleted?: (id: string) => void;
  className?: string;
}) {
  // Locally applied mutation results, so the card is honest the instant a write
  // lands even if the route refetches on its own schedule.
  const [applied, setApplied] = useState<SourceOut | null>(null);
  useEffect(() => setApplied(null), [source]);
  const view = applied ?? source;

  const [busy, setBusy] = useState<null | "retrain" | "cancel" | "save" | "delete">(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [editing, setEditing] = useState(false);
  const [savedNotice, setSavedNotice] = useState(false);
  const [reviewingUrls, setReviewingUrls] = useState(false);

  const [title, setTitle] = useState(view.title);
  const [config, setConfig] = useState<CrawlConfigIn>(() => readCrawlConfig(view.config));
  const [tags, setTags] = useState<string[]>(view.tags);
  const [collection, setCollection] = useState<string | null>(view.collection);
  const [weekly, setWeekly] = useState(view.auto_retrain_hours > 0);

  const kind = KIND[view.kind] ?? { label: view.kind, icon: Icon.Doc };
  const KindIcon = kind.icon;
  const website = WEBSITE_KINDS.has(view.kind);
  const crawling = isCrawling(view.crawl_state);
  // parked: every page read, nothing indexed, waiting on a person. not "crawling"
  // — no wave is in flight — so the poll stops and the card offers the list instead
  const awaitingReview = view.crawl_state === "awaiting_review";
  // The review gate and extracted-page preview are workspace-level routes
  // (/knowledge/...) — a source's editing surface is shared, not agent-scoped.
  const reviewHref = `/knowledge/sources/${view.id}`;
  const childId = documentId ?? (typeof view.config.document_id === "string" ? view.config.document_id : null);

  const apply = (next: SourceOut) => {
    setApplied(next);
    onChanged?.(next);
  };

  async function post(action: "retrain" | "cancel") {
    setBusy(action);
    setFailure(null);
    try {
      apply(await api.post<SourceOut>(`/v1/sources/${view.id}/${action}`, undefined, tenantHeaders()));
    } catch (e) {
      setFailure(explainSourceFailure(e, action === "retrain" ? "This retrain" : "This cancellation"));
    } finally {
      setBusy(null);
    }
  }

  async function saveSettings() {
    setBusy("save");
    setFailure(null);
    try {
      // Stored only. A changed exclude path takes effect on the next retrain,
      // and the response carries pending_changes so the card can offer it.
      const next = await api.patch<SourceOut>(`/v1/sources/${view.id}`, {
        title: title.trim() === "" ? view.title : title.trim(),
        config,
        tags,
        collection,
        auto_retrain_hours: weekly ? RETRAIN_WEEKLY_HOURS : 0,
      });
      apply(next);
      setEditing(false);
      setSavedNotice(true);
    } catch (e) {
      setFailure(explainSourceFailure(e, "These settings"));
    } finally {
      setBusy(null);
    }
  }

  async function remove() {
    setBusy("delete");
    setFailure(null);
    try {
      await api.del(`/v1/sources/${view.id}`);
      setConfirming(false);
      onDeleted?.(view.id);
    } catch (e) {
      setFailure(explainSourceFailure(e, "This source"));
    } finally {
      setBusy(null);
    }
  }

  const openEditor = () => {
    setTitle(view.title);
    setConfig(readCrawlConfig(view.config));
    setTags(view.tags);
    setCollection(view.collection);
    setWeekly(view.auto_retrain_hours > 0);
    setFailure(null);
    setEditing(true);
  };

  const menu: MenuItem[] = [
    {
      label: "Retrain now",
      icon: <Icon.Spark size={15} />,
      disabled: crawling || busy !== null,
      hint: crawling ? "Already running" : undefined,
      onSelect: () => void post("retrain"),
    },
    {
      // a parked crawl is cancellable too: "no thanks" has to be an answer, or the
      // only way out of the review gate would be to approve it
      label: awaitingReview ? "Discard these pages" : "Cancel crawl",
      icon: <Icon.X size={15} />,
      disabled: !(crawling || awaitingReview) || busy !== null,
      onSelect: () => void post("cancel"),
    },
    { label: "Edit settings", icon: <Icon.Settings size={15} />, onSelect: openEditor },
    { separator: true },
    {
      label: "Delete source",
      icon: <Icon.Trash size={15} />,
      tone: "danger",
      onSelect: () => setConfirming(true),
    },
  ];

  const counters: { label: string; value: number; cls?: string }[] = [
    { label: "pages", value: view.link_total },
    { label: "trained", value: view.link_trained, cls: "text-ok" },
    { label: "failed", value: view.link_failed, cls: "text-danger" },
    { label: "excluded", value: view.link_excluded, cls: "text-faint" },
    { label: "pending", value: view.link_pending, cls: "text-muted" },
  ].filter((c, i) => i < 2 || c.value > 0);

  return (
    <Card className={cx("p-4", className)}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-2.5">
          <span className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-line bg-canvas text-faint">
            <KindIcon size={16} />
          </span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="truncate text-sm font-semibold text-ink">{view.title}</span>
              <Badge tone="neutral">{kind.label}</Badge>
              <StatusBadge status={view.crawl_state} reason={view.error} crawl />
              {/* sources.pending_changes exists for exactly this affordance. */}
              {view.pending_changes && !crawling && <Badge tone="brand">Retrain available</Badge>}
            </div>
            {website && view.seed_url !== null && (
              <a
                href={view.seed_url}
                target="_blank"
                rel="noreferrer"
                className="mt-0.5 flex items-center gap-1 truncate text-[12px] text-muted hover:text-brand"
              >
                <span className="truncate">{view.origin ?? view.seed_url}</span>
                <Icon.External size={12} className="shrink-0" />
              </a>
            )}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {crawling ? (
            <Button variant="ghost" size="sm" loading={busy === "cancel"} onClick={() => void post("cancel")}>
              Cancel
            </Button>
          ) : (
            view.pending_changes && (
              <Button variant="secondary" size="sm" loading={busy === "retrain"} onClick={() => void post("retrain")}>
                Retrain
              </Button>
            )
          )}
          {awaitingReview && (
            <Button variant="primary" size="sm" onClick={() => setReviewingUrls(true)}>
              Review pages
            </Button>
          )}
          {website ? (
            <Link
              to={reviewHref}
              className="rounded-lg px-3 py-1.5 text-[13px] font-medium text-brand transition-colors hover:bg-brand-soft"
            >
              Review links
            </Link>
          ) : childId !== null ? (
            <Link
              to={`/knowledge/document/${childId}`}
              className="rounded-lg px-3 py-1.5 text-[13px] font-medium text-brand transition-colors hover:bg-brand-soft"
            >
              Preview
            </Link>
          ) : (
            <Button variant="ghost" size="sm" onClick={openEditor}>
              Edit
            </Button>
          )}
          <Menu trigger={<Icon.Settings size={16} />} items={menu} />
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-muted">
        {counters.map((c) => (
          <span key={c.label} className="tabular-nums">
            <span className={cx("font-semibold", c.cls ?? "text-ink")}>{c.value.toLocaleString()}</span> {c.label}
          </span>
        ))}
        <span className="tabular-nums">{bytes(view.size_bytes)}</span>
        <span className="tabular-nums">{view.chunk_count.toLocaleString()} passages</span>
        {view.last_trained_at !== null ? (
          <span>Last trained {relativeTime(view.last_trained_at)}</span>
        ) : (
          <span>Not trained yet</span>
        )}
        {view.auto_retrain_hours > 0 && <span>Auto-retrain on</span>}
      </div>

      {crawling && (
        <div className="mt-3">
          <LinkTicker progress={progress ?? progressFromSource(view)} pollingPaused={pollingPaused} />
        </div>
      )}

      {!crawling && view.error !== null && view.error !== "" && (
        <div className="mt-3">
          <Alert tone={view.crawl_state === "failed" ? "danger" : "warn"}>{view.error}</Alert>
        </div>
      )}

      {savedNotice && (
        <div className="mt-3">
          <Banner
            tone="warn"
            onDismiss={() => setSavedNotice(false)}
            action={
              <Button
                size="sm"
                loading={busy === "retrain"}
                disabled={crawling}
                onClick={() => {
                  setSavedNotice(false);
                  void post("retrain");
                }}
              >
                Retrain now
              </Button>
            }
          >
            Saved. These settings apply the next time this source is trained.
          </Banner>
        </div>
      )}

      {failure !== null && !editing && (
        <div className="mt-3">
          <Alert tone="danger">{failure}</Alert>
        </div>
      )}

      <SlideOver
        open={editing}
        onClose={() => setEditing(false)}
        title="Source settings"
        description="Saved for the next retrain — nothing is re-read until you ask for it."
        width={640}
        footer={
          <div className="flex items-center justify-end gap-2">
            <Button variant="secondary" onClick={() => setEditing(false)}>
              Cancel
            </Button>
            <Button variant="primary" loading={busy === "save"} disabled={busy !== null} onClick={() => void saveSettings()}>
              Save settings
            </Button>
          </div>
        }
      >
        <div className="flex flex-col gap-4">
          <Field label="Name">
            <Input value={title} maxLength={SOURCE_TITLE_MAX} onChange={(e) => setTitle(e.target.value)} />
          </Field>
          {website && view.kind !== "links" && (
            <CrawlConfigFields value={config} onChange={setConfig} disabled={busy !== null} />
          )}
          <SourceMetaFields
            tags={tags}
            onTags={setTags}
            collection={collection}
            onCollection={setCollection}
            collections={collections}
            disabled={busy !== null}
          />
          <ToggleRow
            label="Retrain weekly"
            hint="We re-read this source every week and only re-index what changed."
            checked={weekly}
            disabled={busy !== null}
            onChange={setWeekly}
          />
          {failure !== null && <Alert tone="danger">{failure}</Alert>}
        </div>
      </SlideOver>

      <ReviewGateModal
        open={reviewingUrls}
        sourceId={view.id}
        sourceTitle={view.title}
        onClose={() => setReviewingUrls(false)}
        // the approve response is a full source, so the card flips to `extracting`
        // immediately and the route's poll takes over from there
        onApproved={apply}
      />

      {confirming && (
        <ConfirmDialog
          title="Delete this source?"
          message={`Every page from "${view.title}" stops being answerable. Type the name to confirm.`}
          confirmWord={view.title}
          danger
          error={failure ?? undefined}
          busy={busy === "delete"}
          onCancel={() => setConfirming(false)}
          onConfirm={() => void remove()}
        />
      )}
    </Card>
  );
}
