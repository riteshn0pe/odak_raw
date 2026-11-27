// Link-group review (crawler §7 "Link-group review", route
// /knowledge/sources/:sourceId). The doc calls this file SourceDetail.tsx
// at /sources/:id; it became workspace-level when sources moved to the shared
// Knowledge page (P2) — the APIs it calls are source-id-addressed, so the agent
// scope was only ever breadcrumbs.
//
// This is the screen that stops "Excluded 412" being a black box. Three groups —
// Trained, Not found, Excluded — each a table over one link state, with the
// server's own sentence in the Why column. That sentence is rendered verbatim
// from `reason_detail` and never composed here: channels and the dashboard have
// to agree about why a page is missing, and they only agree if both print what
// the crawler wrote.
//
// It also carries the rest of a source's editing surface, because a source has
// three shapes: a website (link groups + crawl settings), a text snippet (one
// markdown body) and a Q&A set (pairs, each owning its own write). One route,
// branched on `kind`, rather than three near-identical screens.
//
// Everything degrades. The /v1/sources family is landing behind this screen, so a
// 404/501/no-network renders the screen with one honest sentence instead of a
// spinner that never resolves.
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../../../lib/api";
import { tenantHeaders } from "../../../lib/auth";
import { bytes, relativeTime } from "../../../lib/format";
import type {
  CrawlConfigIn, QaPairOut, SnippetDetail, SourceDetailOut, SourceLinkOut, SourceLinkPage, SourceOut,
} from "../../../lib/types";
import { DataTable, type Column } from "../../../components/DataTable";
import { StatusBadge } from "../../../components/StatusBadge";
import { ConfirmDialog } from "../../../components/ConfirmDialog";
import { SearchInput } from "../../../components/SearchInput";
import { MarkdownPreview } from "../../../components/MarkdownPreview";
import { MarkdownEditor } from "../../../components/ui/MarkdownEditor";
import { QaPairEditor } from "../../../components/QaPairEditor";
import { ReviewGateModal } from "../../../components/ReviewGateModal";
import {
  Accordion, Alert, Badge, Breadcrumb, Button, Card, CardBody, CardHeader, Divider,
  EmptyState, Field, Input, Modal, PageHeader, Select, Skeleton, StatCard, Tabs, Textarea,
  ToggleRow,
} from "../../../components/ui";
import { Icon } from "../../../components/ui/icons";

/** The states with a crawl wave actually in flight. `awaiting_review` is
 *  deliberately not one of them: every page has been fetched, nothing is moving,
 *  and the source is parked until a person approves the list — so polling it would
 *  never end. */
const LIVE_CRAWL = new Set(["discovering", "fetching", "extracting"]);
/** `POST /links/bulk` takes at most 500 ids. Enforced here so a request is never
 *  sent already knowing it will 422. */
const BULK_CAP = 500;
/** `GET /links` caps at 200 per page. */
const PAGE_SIZE = 200;

type GroupKey = "trained" | "not_found" | "excluded";

const GROUP_LABEL: Record<GroupKey, string> = {
  trained: "Trained",
  not_found: "Not found",
  excluded: "Excluded",
};

function notEnabled(e: unknown): boolean {
  if (e instanceof ApiError) return e.status === 404 || e.status === 405 || e.status === 501 || e.status === 0;
  return true;
}

function reason(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.message : fallback;
}

function gateOrReason(e: unknown, fallback: string): string {
  if (e instanceof ApiError && e.status === 403) return `${e.message} — that feature is off for this workspace.`;
  if (e instanceof ApiError && e.status === 409) return e.message;
  if (e instanceof ApiError && e.status === 429) return "Too many requests just now — try again in a minute.";
  return reason(e, fallback);
}

/** The path, which is what a tenant scans a list of 400 URLs by. Falls back to
 *  the whole string for anything unparseable. */
function pathOf(url: string): string {
  try {
    const u = new URL(url);
    return (u.pathname === "/" ? "/" : u.pathname.replace(/\/$/, "")) + u.search;
  } catch {
    return url;
  }
}

/** `reason` is a machine code (`archive_or_taxonomy`); this makes it readable
 *  without inventing a sentence — the sentence is `reason_detail`, server-side. */
function readableCode(code: string): string {
  const spaced = code.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

function isUrl(value: string): boolean {
  return /^https?:\/\//i.test(value.trim());
}

/* --------------------------------------------------- config coercion helpers */
// `SourceOut.config` is a free-form map on the wire, so every field is read
// defensively: an older row missing a key must open the editor, not crash it.

function textList(v: unknown): string {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string").join(" ") : "";
}

function numberOrNull(v: unknown): string {
  return typeof v === "number" && Number.isFinite(v) ? String(v) : "";
}

function depthOf(v: unknown): number {
  return typeof v === "number" && v >= 1 && v <= 10 ? Math.round(v) : 3;
}

function flag(v: unknown): boolean {
  return v === true;
}

function ReviewSkeleton() {
  return (
    <div>
      <Skeleton className="h-3.5 w-40 mb-4" />
      <div className="flex items-start justify-between gap-4 mb-6">
        <div className="flex flex-col gap-2">
          <Skeleton className="h-6 w-56" />
          <Skeleton className="h-3.5 w-72" />
        </div>
        <Skeleton className="h-9 w-40 rounded-lg" />
      </div>
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {Array.from({ length: 4 }).map((_, i) => (
          <Card key={i}>
            <CardBody className="flex flex-col gap-2">
              <Skeleton className="h-3 w-20" />
              <Skeleton className="h-6 w-16" />
            </CardBody>
          </Card>
        ))}
      </div>
      <Card className="overflow-hidden mt-6">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="flex items-center gap-4 px-4 py-3.5 border-b border-line last:border-0">
            <Skeleton className="h-4 w-64" />
            <Skeleton className="h-5 w-20 rounded-full" />
            <Skeleton className="h-4 w-40 ml-auto" />
          </div>
        ))}
      </Card>
    </div>
  );
}

export function SourceLinkReview() {
  const { sourceId = "" } = useParams<{ sourceId: string }>();
  const nav = useNavigate();

  const [detail, setDetail] = useState<SourceDetailOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [missing, setMissing] = useState(false);
  const [crawlerOff, setCrawlerOff] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const [group, setGroup] = useState<GroupKey>("trained");
  const [links, setLinks] = useState<SourceLinkOut[] | null>(null);
  const [linkTotal, setLinkTotal] = useState(0);
  const [linkCounts, setLinkCounts] = useState<Record<string, number>>({});
  const [offset, setOffset] = useState(0);
  const [q, setQ] = useState("");
  const [qQuery, setQQuery] = useState("");
  const [reasonFilter, setReasonFilter] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [preview, setPreview] = useState<SourceLinkOut | null>(null);

  const [addOpen, setAddOpen] = useState(false);
  const [addText, setAddText] = useState("");
  const [addingLinks, setAddingLinks] = useState(false);

  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmBulk, setConfirmBulk] = useState<"exclude" | "restore" | null>(null);
  const [reviewingUrls, setReviewingUrls] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [linkBusy, setLinkBusy] = useState<string | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);

  // Crawl settings buffer. Loaded from the row once, then owned by the editor so
  // a poll or a refetch never yanks a half-typed exclude path away.
  const [cfgLoadedFor, setCfgLoadedFor] = useState("");
  const [title, setTitle] = useState("");
  const [includePaths, setIncludePaths] = useState("");
  const [excludePaths, setExcludePaths] = useState("");
  const [maxPages, setMaxPages] = useState("");
  const [maxDepth, setMaxDepth] = useState(3);
  const [includeSubdomains, setIncludeSubdomains] = useState(false);
  const [includePdf, setIncludePdf] = useState(false);
  const [reviewBeforeIndex, setReviewBeforeIndex] = useState(false);
  const [tags, setTags] = useState("");
  const [collection, setCollection] = useState("");
  const [autoRetrainHours, setAutoRetrainHours] = useState(0);
  const [savingCfg, setSavingCfg] = useState(false);

  const [snippet, setSnippet] = useState<SnippetDetail | null>(null);
  const [snippetBody, setSnippetBody] = useState("");
  const [snippetLoadedFor, setSnippetLoadedFor] = useState("");
  const [savingSnippet, setSavingSnippet] = useState(false);

  const [pairs, setPairs] = useState<QaPairOut[]>([]);
  const [pairsOff, setPairsOff] = useState(false);

  useEffect(() => {
    const t = window.setTimeout(() => setQQuery(q), 300);
    return () => clearTimeout(t);
  }, [q]);

  /* ----------------------------------------------------------------- fetches */

  const loadDetail = useCallback(async () => {
    try {
      const row = await api.get<SourceDetailOut>(`/v1/sources/${sourceId}`, tenantHeaders());
      setDetail(row);
      setMissing(false);
      setCrawlerOff(false);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) { setMissing(true); return; }
      if (notEnabled(e)) { setCrawlerOff(true); return; }
      setError(reason(e, "could not load this source"));
    }
  }, [sourceId]);

  const loadLinks = useCallback(async () => {
    if (!sourceId) return;
    try {
      const params = new URLSearchParams({
        state: group,
        limit: String(PAGE_SIZE),
        offset: String(offset),
      });
      if (qQuery.trim()) params.set("q", qQuery.trim());
      if (reasonFilter) params.set("reason", reasonFilter);
      const page = await api.get<SourceLinkPage>(`/v1/sources/${sourceId}/links?${params}`, tenantHeaders());
      setLinks(page.items);
      setLinkTotal(page.total);
      setLinkCounts(page.counts);
      setNote("");
    } catch (e) {
      // Fall back to the capped group the detail read already carried. 25 rows
      // beats an empty table, and the note says so rather than pretending.
      if (notEnabled(e)) {
        setLinks(null);
        setNote("Showing the first 25 pages of this group — the full, searchable list is not available here yet.");
        return;
      }
      setError(reason(e, "could not load these pages"));
    }
  }, [sourceId, group, offset, qQuery, reasonFilter]);

  const loadSnippet = useCallback(async () => {
    try {
      const row = await api.get<SnippetDetail>(`/v1/sources/snippets/${sourceId}`, tenantHeaders());
      setSnippet(row);
    } catch (e) {
      if (!notEnabled(e)) setError(reason(e, "could not load this snippet"));
      setSnippet(null);
    }
  }, [sourceId]);

  const loadPairs = useCallback(async () => {
    try {
      setPairs(await api.get<QaPairOut[]>(`/v1/sources/qa/${sourceId}/pairs`, tenantHeaders()));
      setPairsOff(false);
    } catch (e) {
      if (notEnabled(e)) { setPairsOff(true); setPairs([]); return; }
      setError(reason(e, "could not load these Q&A pairs"));
    }
  }, [sourceId]);

  useEffect(() => {
    let live = true;
    void (async () => {
      await loadDetail();
      if (live) setLoading(false);
    })();
    return () => { live = false; };
  }, [loadDetail]);

  const kind = detail?.kind ?? "";
  const isSnippet = kind === "snippet";
  const isQa = kind === "qa";
  const isWebsite = detail !== null && !isSnippet && !isQa;

  useEffect(() => { if (isWebsite) void loadLinks(); }, [isWebsite, loadLinks]);
  useEffect(() => { if (isSnippet) void loadSnippet(); }, [isSnippet, loadSnippet]);
  useEffect(() => { if (isQa) void loadPairs(); }, [isQa, loadPairs]);

  // Seed the editors exactly once per source, so a refetch never overwrites what
  // is being typed.
  useEffect(() => {
    if (!detail || cfgLoadedFor === detail.id) return;
    const cfg = detail.config ?? {};
    setTitle(detail.title);
    setIncludePaths(textList(cfg.include_paths));
    setExcludePaths(textList(cfg.exclude_paths));
    setMaxPages(numberOrNull(cfg.max_pages));
    setMaxDepth(depthOf(cfg.max_depth));
    setIncludeSubdomains(flag(cfg.include_subdomains));
    setIncludePdf(flag(cfg.include_pdf));
    setReviewBeforeIndex(flag(cfg.review_before_index));
    setTags((detail.tags ?? []).join(", "));
    setCollection(detail.collection ?? "");
    setAutoRetrainHours(detail.auto_retrain_hours);
    setCfgLoadedFor(detail.id);
  }, [detail, cfgLoadedFor]);

  useEffect(() => {
    if (!snippet || snippetLoadedFor === snippet.source.id) return;
    setSnippetBody(snippet.body);
    setSnippetLoadedFor(snippet.source.id);
  }, [snippet, snippetLoadedFor]);

  // Selection belongs to one group and one filter set. Changing either makes the
  // ids meaningless, so drop them rather than acting on a stale set.
  useEffect(() => { setSelected(new Set()); setOffset(0); }, [group, qQuery, reasonFilter]);

  /* --------------------------------------------------------------- mutations */

  async function refresh() {
    await loadDetail();
    if (isWebsite) await loadLinks();
  }

  async function retrain() {
    if (busy) return;
    setBusy(true);
    try {
      await api.post(`/v1/sources/${sourceId}/retrain`, undefined, tenantHeaders());
      setError("");
      await refresh();
    } catch (e) {
      setError(gateOrReason(e, "could not start that retrain"));
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    if (busy) return;
    setBusy(true);
    try {
      const updated = await api.post<SourceOut>(`/v1/sources/${sourceId}/cancel`, undefined, tenantHeaders());
      setDetail((prev) => (prev ? { ...prev, ...updated } : prev));
      setNote("Crawl cancelled. Everything already trained stays answerable — retrain when you want the rest.");
      await refresh();
    } catch (e) {
      setError(gateOrReason(e, "could not cancel that crawl"));
    } finally {
      setBusy(false);
    }
  }

  async function removeSource() {
    setDeleting(true);
    try {
      await api.del(`/v1/sources/${sourceId}`);
      setConfirmDelete(false);
      nav("/knowledge", { replace: true });
    } catch (e) {
      setError(reason(e, "could not delete this source"));
    } finally {
      setDeleting(false);
    }
  }

  async function setLinkState(link: SourceLinkOut, action: "exclude" | "restore") {
    setLinkBusy(link.id);
    try {
      const updated = await api.post<SourceLinkOut>(
        `/v1/sources/${sourceId}/links/${link.id}/${action}`,
        undefined,
        tenantHeaders(),
      );
      // Patch the row in place: the group it now belongs to may be a different
      // tab, and yanking it out from under the cursor reads as a bug.
      setLinks((prev) => (prev ? prev.map((l) => (l.id === updated.id ? updated : l)) : prev));
      setPreview((prev) => (prev && prev.id === updated.id ? updated : prev));
      void loadDetail();
    } catch (e) {
      setError(gateOrReason(e, `could not ${action} that page`));
    } finally {
      setLinkBusy(null);
    }
  }

  async function bulk(action: "exclude" | "restore") {
    const ids = [...selected].slice(0, BULK_CAP);
    if (!ids.length || busy) return;
    setBusy(true);
    setBulkBusy(true);
    try {
      const res = await api.post<{ updated: number }>(
        `/v1/sources/${sourceId}/links/bulk`,
        { link_ids: ids, action },
        tenantHeaders(),
      );
      setSelected(new Set());
      setConfirmBulk(null);
      setNote(`${res.updated} ${res.updated === 1 ? "page" : "pages"} ${action === "exclude" ? "excluded" : "queued to be read again"}.`);
      await refresh();
    } catch (e) {
      setError(gateOrReason(e, `could not ${action} those pages`));
    } finally {
      setBusy(false);
      setBulkBusy(false);
    }
  }

  async function addLinks() {
    const urls = addText.split(/\s+/).map((u) => u.trim()).filter(Boolean);
    if (!urls.length || addingLinks) return;
    setAddingLinks(true);
    try {
      await api.post<SourceLinkOut[]>(`/v1/sources/${sourceId}/links`, { urls }, tenantHeaders());
      setAddText("");
      setAddOpen(false);
      setNote(`${urls.length} ${urls.length === 1 ? "page" : "pages"} queued. They appear under Trained once they've been read.`);
      await refresh();
    } catch (e) {
      setError(gateOrReason(e, "could not add those pages"));
    } finally {
      setAddingLinks(false);
    }
  }

  function configBody(): CrawlConfigIn {
    const parsed = Number.parseInt(maxPages, 10);
    return {
      include_paths: includePaths.split(/\s+/).map((p) => p.trim()).filter(Boolean),
      exclude_paths: excludePaths.split(/\s+/).map((p) => p.trim()).filter(Boolean),
      max_pages: Number.isFinite(parsed) && parsed > 0 ? parsed : null,
      max_depth: maxDepth,
      include_subdomains: includeSubdomains,
      include_pdf: includePdf,
      // hydrated from the stored config above, so saving an unrelated field here
      // cannot switch the review gate off behind the tenant's back
      review_before_index: reviewBeforeIndex,
    };
  }

  async function saveConfig(thenRetrain: boolean) {
    if (savingCfg) return;
    setSavingCfg(true);
    try {
      const updated = await api.patch<SourceDetailOut>(`/v1/sources/${sourceId}`, {
        title: title.trim() || detail?.title,
        config: configBody(),
        tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
        collection: collection.trim() || null,
        auto_retrain_hours: autoRetrainHours,
      });
      setDetail((prev) => (prev ? { ...prev, ...updated } : prev));
      setError("");
      if (thenRetrain) await retrain();
      else setNote("Saved. These settings take effect on the next retrain — nothing is re-read yet.");
    } catch (e) {
      setError(gateOrReason(e, "could not save those settings"));
    } finally {
      setSavingCfg(false);
    }
  }

  async function saveSnippet() {
    if (savingSnippet || !snippet) return;
    setSavingSnippet(true);
    try {
      await api.put(`/v1/sources/snippets/${sourceId}`, {
        title: title.trim() || snippet.source.title,
        body: snippetBody,
        tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
        collection: collection.trim() || null,
      }, tenantHeaders());
      setNote("Saved. Your assistant is re-reading it now.");
      await loadDetail();
    } catch (e) {
      setError(gateOrReason(e, "could not save this snippet"));
    } finally {
      setSavingSnippet(false);
    }
  }

  /* ------------------------------------------------------------------ derived */

  // Memoised: reasonOptions below depends on it, and a fresh array every
  // render would defeat that memo.
  const fallbackGroup: SourceLinkOut[] = useMemo(
    () => (detail ? (group === "trained" ? detail.trained : group === "not_found" ? detail.not_found : detail.excluded) : []),
    [detail, group],
  );

  const needle = q.trim().toLowerCase();
  // When the links endpoint answered, it already applied `q`/`reason` server-side.
  // When we are on the capped fallback group, apply them here so the toolbar is
  // not a decoration.
  const rows: SourceLinkOut[] = links ?? fallbackGroup.filter((l) => {
    if (reasonFilter && l.reason !== reasonFilter) return false;
    if (!needle) return true;
    return l.url.toLowerCase().includes(needle) || (l.title ?? "").toLowerCase().includes(needle);
  });

  const counts: Record<GroupKey, number> = {
    trained: linkCounts.trained ?? detail?.link_trained ?? 0,
    not_found: linkCounts.not_found ?? detail?.link_failed ?? 0,
    excluded: linkCounts.excluded ?? detail?.link_excluded ?? 0,
  };

  const reasonOptions = useMemo(() => {
    const seen = new Set<string>();
    for (const l of [...rows, ...fallbackGroup]) if (l.reason) seen.add(l.reason);
    return [...seen].sort();
  }, [rows, fallbackGroup]);

  const crawling = detail !== null && LIVE_CRAWL.has(detail.crawl_state);
  const awaitingReview = detail !== null && detail.crawl_state === "awaiting_review";
  const heading = detail?.origin || detail?.title || "Source";

  const linkColumns: Column<SourceLinkOut>[] = [
    {
      key: "page", header: "Page", sortValue: (l) => l.title ?? l.url,
      render: (l) => (
        <div className="min-w-0">
          <div className="font-medium truncate">{l.title || pathOf(l.url)}</div>
          <a
            href={l.url}
            target="_blank"
            rel="noreferrer noopener"
            className="inline-flex items-center gap-1 text-[12px] text-faint hover:text-brand hover:underline"
          >
            <span className="truncate max-w-[22rem]">{l.url}</span>
            <Icon.External size={14} />
          </a>
        </div>
      ),
    },
    { key: "state", header: "Status", sortValue: (l) => l.state, render: (l) => <StatusBadge status={l.state} reason={l.reason_detail ?? l.reason} /> },
    {
      key: "why", header: "Why", sortValue: (l) => l.reason ?? "",
      render: (l) => {
        if (!l.reason && l.http_status === null) return <span className="text-faint">—</span>;
        const detailText = l.reason_detail ?? "";
        return (
          <div className="min-w-0 max-w-[24rem]">
            <div className="flex items-center gap-1.5">
              {l.reason && <span className="text-[13px]">{readableCode(l.reason)}</span>}
              {l.http_status !== null && <Badge tone={l.http_status >= 400 ? "danger" : "neutral"}>{l.http_status}</Badge>}
            </div>
            {/* Rendered verbatim: the crawler writes this sentence so the channel
                and the dashboard say the same thing. */}
            {detailText && (isUrl(detailText) ? (
              <a href={detailText} target="_blank" rel="noreferrer noopener" className="text-[12px] text-brand hover:underline break-all">
                {detailText}
              </a>
            ) : (
              <div className="text-[12px] text-faint">{detailText}</div>
            ))}
          </div>
        );
      },
    },
    { key: "size", header: "Size", align: "right", sortValue: (l) => l.content_bytes, render: (l) => <span className="tabular-nums">{l.content_bytes ? bytes(l.content_bytes) : "—"}</span> },
    { key: "depth", header: "Depth", align: "right", sortValue: (l) => l.depth, render: (l) => <span className="tabular-nums">{l.depth}</span> },
    { key: "fetched", header: "Fetched", sortValue: (l) => l.fetched_at ?? "", render: (l) => relativeTime(l.fetched_at) },
    {
      key: "actions", header: "", align: "right",
      render: (l) => (
        <div className="flex items-center justify-end gap-2">
          {l.state === "trained" && (
            <Button size="sm" variant="ghost" onClick={() => setPreview(l)}>Preview</Button>
          )}
          {l.state === "excluded" ? (
            <Button size="sm" variant="ghost" onClick={() => void setLinkState(l, "restore")} loading={linkBusy === l.id} disabled={linkBusy !== null && linkBusy !== l.id}>Include anyway</Button>
          ) : (
            <Button size="sm" variant="ghost" onClick={() => void setLinkState(l, "exclude")} loading={linkBusy === l.id} disabled={linkBusy !== null && linkBusy !== l.id}>Exclude</Button>
          )}
        </div>
      ),
    },
  ];

  /* ------------------------------------------------------------------- render */

  if (loading) return <ReviewSkeleton />;

  if (missing) {
    return (
      <div>
        <Breadcrumb items={[{ label: "Sources", to: "/knowledge" }, { label: "Not found" }]} />
        <EmptyState
          icon={<Icon.Alert size={22} />}
          title="That source doesn't exist"
          description="It may have been deleted."
          action={<Button onClick={() => nav("/knowledge")}>Back to sources</Button>}
        />
      </div>
    );
  }

  if (crawlerOff || !detail) {
    return (
      <div>
        <Breadcrumb items={[{ label: "Sources", to: "/knowledge" }, { label: "Source" }]} />
        <EmptyState
          icon={<Icon.Globe size={22} />}
          title="This screen isn't switched on yet"
          description="Website, snippet and Q&A sources need the sources API, which isn't live on this deployment. Files keep working from the Sources screen."
          action={<Button onClick={() => nav("/knowledge")}>Back to sources</Button>}
        />
      </div>
    );
  }

  const settingsFields = (
    <div className="flex flex-col gap-4">
      <Field label="Title">
        <Input value={title} onChange={(e) => setTitle(e.target.value)} placeholder={detail.title} />
      </Field>
      {isWebsite && (
        <>
          <Field label="Include paths" hint="Only crawl these. Space-separated. /blog covers /blog/anything; * matches anything. Leave empty for the whole site.">
            <Input value={includePaths} onChange={(e) => setIncludePaths(e.target.value)} placeholder="/docs /pricing" />
          </Field>
          <Field label="Exclude paths" hint="Never crawl these. Excludes always win.">
            <Input value={excludePaths} onChange={(e) => setExcludePaths(e.target.value)} placeholder="/blog/tag /archive" />
          </Field>
          <div className="grid sm:grid-cols-2 gap-4">
            <Field label="Page limit" hint="Leave empty for no limit.">
              <Input
                type="number"
                min={1}
                value={maxPages}
                onChange={(e) => setMaxPages(e.target.value)}
                placeholder="e.g. 500"
              />
            </Field>
            <Field label="Crawl depth" hint="How many clicks from the start page.">
              <Select value={String(maxDepth)} onChange={(e) => setMaxDepth(Number(e.target.value))}>
                {Array.from({ length: 10 }).map((_, i) => (
                  <option key={i + 1} value={i + 1}>{i + 1}</option>
                ))}
              </Select>
            </Field>
          </div>
          <ToggleRow
            label="Include subdomains"
            hint="blog.acme.com as well as acme.com."
            checked={includeSubdomains}
            onChange={setIncludeSubdomains}
          />
          <ToggleRow
            label="Include PDFs found on the site"
            hint="PDFs may need OCR, which counts against your OCR allowance."
            checked={includePdf}
            onChange={setIncludePdf}
          />
          <ToggleRow
            label="Review pages before indexing"
            hint="Read every page first and show you the list. Nothing is added to your assistant until you approve it."
            checked={reviewBeforeIndex}
            onChange={setReviewBeforeIndex}
          />
        </>
      )}
      <div className="grid sm:grid-cols-2 gap-4">
        <Field label="Tags (comma-separated)" hint="Inherited by every page from this source.">
          <Input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="e.g. help-centre, policy" />
        </Field>
        <Field label="Collection">
          <Input value={collection} onChange={(e) => setCollection(e.target.value)} placeholder="e.g. website" />
        </Field>
      </div>
      {!isSnippet && (
        <Field label="Check for changes" hint="Off means it only updates when you retrain.">
          <Select value={String(autoRetrainHours)} onChange={(e) => setAutoRetrainHours(Number(e.target.value))}>
            <option value="0">Never — I'll retrain myself</option>
            <option value="24">Every day</option>
            <option value="168">Every week</option>
            <option value="720">Every month</option>
          </Select>
        </Field>
      )}
    </div>
  );

  return (
    <div>
      <Breadcrumb items={[{ label: "Sources", to: "/knowledge" }, { label: heading }]} />

      <PageHeader
        title={heading}
        description={detail.seed_url ?? detail.title}
        actions={
          <div className="flex items-center gap-2">
            {isWebsite && (
              <Button variant="secondary" onClick={() => setAddOpen(true)}>
                <Icon.Plus size={16} /> Add pages
              </Button>
            )}
            {awaitingReview ? (
              <>
                {/* cancel is still reachable while parked: "no thanks" has to be a
                    possible answer to the gate, and cancel closes the fetched rows
                    off without indexing them */}
                <Button variant="secondary" onClick={() => void cancel()} loading={busy}>Discard these pages</Button>
                <Button variant="primary" onClick={() => setReviewingUrls(true)}>Review pages</Button>
              </>
            ) : crawling ? (
              <Button variant="secondary" onClick={() => void cancel()} loading={busy}>Cancel crawl</Button>
            ) : (
              <Button variant="primary" onClick={() => void retrain()} loading={busy}>Retrain</Button>
            )}
            <Button variant="danger" onClick={() => setConfirmDelete(true)}>Delete</Button>
          </div>
        }
      />

      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center gap-2 text-[13px] text-muted">
          <StatusBadge status={detail.crawl_state} reason={detail.error} crawl />
          {detail.pending_changes && <Badge tone="brand">Changes available</Badge>}
          <span>
            Last trained {relativeTime(detail.last_trained_at)} · checked {relativeTime(detail.last_checked_at)}
          </span>
          {detail.link_pending > 0 && (
            <span className="tabular-nums">· {detail.link_pending} still queued</span>
          )}
        </div>

        {detail.error && <Alert>{detail.error}</Alert>}
        {error && <Alert>{error}</Alert>}
        {note && <Alert tone="warn">{note}</Alert>}

        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <StatCard label="Pages trained" value={<span className="tabular-nums">{counts.trained}</span>} />
          <StatCard label="Not found" value={<span className="tabular-nums">{counts.not_found}</span>} tone={counts.not_found > 0 ? "warn" : undefined} />
          <StatCard label="Excluded" value={<span className="tabular-nums">{counts.excluded}</span>} />
          <StatCard label="Storage" value={<span className="tabular-nums">{bytes(detail.size_bytes)}</span>} />
        </div>

        {isSnippet && (
          <Card>
            <CardHeader
              title="Text"
              description="What your assistant reads. Markdown — headings and lists help it answer precisely."
              actions={
                <Button variant="primary" onClick={() => void saveSnippet()} loading={savingSnippet} disabled={snippet === null}>
                  Save and re-index
                </Button>
              }
            />
            <CardBody className="flex flex-col gap-4">
              {snippet === null ? (
                <Alert tone="warn">
                  The snippet body could not be loaded here. Title, tags and collection below still save.
                </Alert>
              ) : (
                <MarkdownEditor
                  value={snippetBody}
                  onChange={setSnippetBody}
                  placeholder="Opening hours are 9am to 6pm, Monday to Friday…"
                  minRows={14}
                />
              )}
              <Divider />
              {settingsFields}
              <div>
                <Button variant="secondary" onClick={() => void saveConfig(false)} loading={savingCfg}>
                  Save details
                </Button>
              </div>
            </CardBody>
          </Card>
        )}

        {isQa && (
          <>
            <Card>
              <CardHeader
                title="Question and answer pairs"
                description="These beat every other source when a question matches. Each row saves on its own."
              />
              <CardBody>
                {pairsOff ? (
                  <Alert tone="warn">
                    Q&amp;A pairs are not available here yet.
                  </Alert>
                ) : (
                  <QaPairEditor
                    mode="saved"
                    sourceId={sourceId}
                    pairs={pairs}
                    onChanged={() => { void loadPairs(); void loadDetail(); }}
                  />
                )}
              </CardBody>
            </Card>

            <Accordion title="Set details" subtitle="Title, tags and how often this is checked" defaultOpen={false}>
              <div className="flex flex-col gap-4">
                {settingsFields}
                <div className="flex items-center gap-2">
                  <Button variant="primary" onClick={() => void saveConfig(true)} loading={savingCfg}>Save and retrain</Button>
                  <Button variant="secondary" onClick={() => void saveConfig(false)} loading={savingCfg}>Save for later</Button>
                </div>
              </div>
            </Accordion>
          </>
        )}

        {isWebsite && (
          <>
            <Tabs
              value={group}
              onChange={(v) => setGroup(v as GroupKey)}
              items={(["trained", "not_found", "excluded"] as GroupKey[]).map((key) => ({
                value: key,
                label: <>{GROUP_LABEL[key]} <span className="tabular-nums opacity-60">{counts[key]}</span></>,
              }))}
            />

            <div className="flex flex-wrap items-center gap-2">
              <div className="flex-1 min-w-56">
                <SearchInput
                  placeholder="Search these pages"
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                />
              </div>
              <Select value={reasonFilter} onChange={(e) => setReasonFilter(e.target.value)} className="w-56">
                <option value="">Any reason</option>
                {reasonOptions.map((r) => (
                  <option key={r} value={r}>{readableCode(r)}</option>
                ))}
              </Select>
              {(q || reasonFilter) && (
                <Button variant="ghost" onClick={() => { setQ(""); setReasonFilter(""); }}>Clear</Button>
              )}
            </div>

            {selected.size > 0 && (
              <Card className="sticky bottom-4 shadow-pop z-10">
                <CardBody className="flex flex-wrap items-center gap-3">
                  <span className="text-sm font-medium tabular-nums">{selected.size} selected</span>
                  {selected.size > BULK_CAP && (
                    <span className="text-[12px] text-faint">Only the first {BULK_CAP} are sent — that's the API's limit for one action.</span>
                  )}
                  <div className="ml-auto flex items-center gap-2">
                    {group === "excluded" ? (
                      <Button size="sm" variant="primary" onClick={() => setConfirmBulk("restore")}>Restore selected</Button>
                    ) : (
                      <Button size="sm" variant="danger" onClick={() => setConfirmBulk("exclude")}>Exclude selected</Button>
                    )}
                    <Button size="sm" variant="ghost" onClick={() => setSelected(new Set())} aria-label="Clear selection">
                      <Icon.X size={14} />
                    </Button>
                  </div>
                </CardBody>
              </Card>
            )}

            {rows.length === 0 ? (
              <Card>
                <EmptyState
                  icon={group === "trained" ? <Icon.Check size={22} /> : <Icon.Link size={22} />}
                  title={
                    q || reasonFilter ? "Nothing matches those filters"
                      : group === "trained" ? "No pages trained yet"
                      : group === "not_found" ? "Nothing failed — every page was readable"
                      : "Nothing was excluded"
                  }
                  description={
                    q || reasonFilter ? "Clear the search or the reason filter."
                      : group === "trained" ? "Once the crawl finishes, every readable page lands here."
                      : "That is the good outcome."
                  }
                  action={(q || reasonFilter)
                    ? <Button variant="secondary" onClick={() => { setQ(""); setReasonFilter(""); }}>Clear filters</Button>
                    : undefined}
                />
              </Card>
            ) : (
              <Card>
                <DataTable
                  columns={linkColumns}
                  rows={rows}
                  selectable
                  getRowId={(l) => l.id}
                  selected={selected}
                  onSelectedChange={setSelected}
                  pageSize={25}
                  empty="No pages in this group."
                />
              </Card>
            )}

            {links !== null && linkTotal > PAGE_SIZE && (
              <div className="flex items-center gap-3">
                <span className="text-[13px] text-muted tabular-nums">
                  {offset + 1}–{Math.min(offset + PAGE_SIZE, linkTotal)} of {linkTotal}
                </span>
                <div className="ml-auto flex items-center gap-2">
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={offset === 0}
                    onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                  >
                    Previous
                  </Button>
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={offset + PAGE_SIZE >= linkTotal}
                    onClick={() => setOffset(offset + PAGE_SIZE)}
                  >
                    Next
                  </Button>
                </div>
              </div>
            )}

            <Divider />

            <Accordion
              title="Crawl settings"
              subtitle="Changing these doesn't re-read anything until you retrain"
              defaultOpen={false}
            >
              <div className="flex flex-col gap-4">
                {settingsFields}
                <div className="flex items-center gap-2">
                  <Button variant="primary" onClick={() => void saveConfig(true)} loading={savingCfg}>Save and retrain</Button>
                  <Button variant="secondary" onClick={() => void saveConfig(false)} loading={savingCfg}>Save for later</Button>
                </div>
              </div>
            </Accordion>
          </>
        )}
      </div>

      <Modal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        title="Add pages to this source"
        description="One URL per line. They're read with this source's settings and inherit its tags."
        size="md"
        footer={
          <>
            <Button variant="secondary" onClick={() => setAddOpen(false)} disabled={addingLinks}>Cancel</Button>
            <Button variant="primary" onClick={() => void addLinks()} loading={addingLinks} disabled={!addText.trim()}>
              Add pages
            </Button>
          </>
        }
      >
        <Textarea
          mono
          resizable
          value={addText}
          onChange={(e) => setAddText(e.target.value)}
          rows={8}
          placeholder={"https://acme.com/pricing\nhttps://acme.com/terms"}
        />
      </Modal>

      <Modal
        open={preview !== null}
        onClose={() => setPreview(null)}
        title={preview?.title || (preview ? pathOf(preview.url) : "")}
        description={preview ? (
          <span className="flex flex-wrap items-center gap-2">
            <a href={preview.url} target="_blank" rel="noreferrer noopener" className="text-brand hover:underline break-all">
              {preview.url}
            </a>
            <span className="tabular-nums">· {preview.content_bytes ? bytes(preview.content_bytes) : "size unknown"} · read {relativeTime(preview.fetched_at)}</span>
          </span>
        ) : undefined}
        size="lg"
        footer={preview ? (
          <>
            <Button variant="secondary" onClick={() => window.open(preview.url, "_blank", "noreferrer")}>Open live page</Button>
            {preview.document_id && (
              <Button variant="secondary" onClick={() => nav(`/knowledge/document/${preview.document_id}`)}>
                Open extracted page
              </Button>
            )}
            <Button variant="danger" onClick={() => { void setLinkState(preview, "exclude"); setPreview(null); }}>
              Exclude this page
            </Button>
          </>
        ) : undefined}
      >
        {/* There is no endpoint that returns one crawled page's markdown, so the
            preview degrades to the extracted document rather than inventing API
            surface. MarkdownPreview says so in its own empty state. */}
        <MarkdownPreview
          value={null}
          maxHeight={420}
          emptyLabel="The cleaned-up text for one page is not shown here. Open the extracted page to see exactly what your assistant kept."
        />
      </Modal>

      <ReviewGateModal
        open={reviewingUrls}
        sourceId={sourceId}
        sourceTitle={detail.title}
        onClose={() => setReviewingUrls(false)}
        onApproved={(next) => setDetail((prev) => (prev ? { ...prev, ...next } : prev))}
      />

      {confirmDelete && (
        <ConfirmDialog
          title="Delete source"
          message={`Remove "${detail.title}" and every page indexed from it. Your assistant stops answering from it immediately. This cannot be undone.`}
          confirmWord={detail.title}
          danger
          busy={deleting}
          onConfirm={() => void removeSource()}
          onCancel={() => { if (!deleting) setConfirmDelete(false); }}
        />
      )}

      {confirmBulk === "exclude" && (
        <ConfirmDialog
          title={`Exclude ${Math.min(selected.size, BULK_CAP)} pages`}
          message="Your assistant stops answering from these pages and a retrain will not bring them back. You can include them again from the Excluded tab."
          confirmWord="exclude"
          danger
          busy={bulkBusy}
          onConfirm={() => void bulk("exclude")}
          onCancel={() => { if (!bulkBusy) setConfirmBulk(null); }}
        />
      )}

      {confirmBulk === "restore" && (
        <ConfirmDialog
          title={`Restore ${Math.min(selected.size, BULK_CAP)} pages`}
          message="These pages go back in the queue to be read again, and a retrain will keep them."
          confirmWord="restore"
          busy={bulkBusy}
          onConfirm={() => void bulk("restore")}
          onCancel={() => { if (!bulkBusy) setConfirmBulk(null); }}
        />
      )}
    </div>
  );
}
