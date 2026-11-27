// Sources — "everything your assistants can answer from" (crawler §7). The
// knowledge machinery is shared between two mounts (P2):
//
// - /knowledge mounts `Knowledge` (this file, exported below): the workspace
//   knowledge base. Every source in the tenant, all add flows, and sources
//   created there carry no agent_id — every agent answers from them, because
//   retrieval has always been tenant-wide.
// - /agents/:agentId/sources mounts `Sources`: a read-only view of what is
//   scoped to that agent. The add flows are gone from there (they are
//   universal now); the lists stay filtered to the agent's id.
//
// Two things about this file are deliberate.
//
// 1. It never blocks on the crawler backend. The /v1/sources family is landing
//    behind this screen, so every fetch treats 404/501/no-network as "not
//    switched on yet" and renders the screen anyway: Files keeps working, and
//    the website/snippet/Q&A tabs say plainly that they are not enabled. A
//    tenant who opens this before the crawler ships sees a working screen with
//    one honest sentence, not a spinner that never resolves.
//
// 2. The 2-second crawl poll lives here, not in SourceCard or LinkTicker. Those
//    are presentational; a fetch inside a card would multiply by the number of
//    cards and would outlive the screen. This owns one timer, aborts it on
//    unmount, stops it when every crawl settles, and stops it while the tab is
//    hidden — people leave a dashboard open all day.
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../../../lib/api";
import { tenantHeaders } from "../../../lib/auth";
import { bytes, relativeTime } from "../../../lib/format";
import type {
  CrawlProgressOut, DocumentOut, SourceOut, SourcesSummaryOut, UploadResponse,
} from "../../../lib/types";
import { DataTable, type Column } from "../../../components/DataTable";
import { DocumentUpload } from "../../../components/DocumentUpload";
import { StatusBadge } from "../../../components/StatusBadge";
import { ConfirmDialog } from "../../../components/ConfirmDialog";
import { SourceCard } from "../../../components/SourceCard";
import { AddSourceModal } from "../../../components/AddSourceModal";
import { SearchInput } from "../../../components/SearchInput";
import {
  Alert, Badge, Banner, Button, Card, CardBody, CardHeader, EmptyState, Input,
  PageHeader, ProgressBar, SegmentedControl, Skeleton, StatCard, Tabs,
} from "../../../components/ui";
import { Icon } from "../../../components/ui/icons";
import { useAgent } from "./AgentLayout";

/** `sources.crawl_state` still doing work (crawler §2 schema: idle | discovering
 *  | fetching | extracting | awaiting_review | ready | failed | cancelled). An
 *  allow-list, not a deny-list — an unrecognised state must not leave a poll
 *  running forever.
 *
 *  `awaiting_review` is deliberately not in here. Nothing is in flight for such a
 *  source — every page has been read and none indexed — so a poll would never
 *  end; it is waiting on a person to approve the URL list, not on the crawler. */
const LIVE_CRAWL = new Set(["discovering", "fetching", "extracting"]);
/** `documents.status` still indexing. Mirrors Documents.tsx:16. */
const PENDING_DOC = new Set(["queued", "processing"]);
/** Bulk endpoints cap at 500 ids; enforced client-side so the request is never
 *  sent knowing it will 422. */
const BULK_CAP = 500;

type TabKey = "files" | "website" | "text" | "qa";
type WebsiteFilter = "all" | "crawling" | "review" | "retrain";

/** A website source is any kind that is not a snippet or a Q&A set: the schema
 *  spells the crawl kinds three ways (`crawl` | `sitemap` | `links`) and a later
 *  revision collapses them to `website`, so classify by exclusion and both
 *  spellings land in the same tab. */
function isWebsite(s: SourceOut): boolean {
  return s.kind !== "snippet" && s.kind !== "qa";
}

/**
 * True when a failure means "this endpoint does not exist here yet" rather than
 * "something went wrong". 404/405/501 is an unmounted route; a non-ApiError is a
 * fetch-level TypeError, i.e. no server at all. Both degrade to a sentence.
 */
function notEnabled(e: unknown): boolean {
  if (e instanceof ApiError) return e.status === 404 || e.status === 405 || e.status === 501 || e.status === 0;
  return true;
}

function reason(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.message : fallback;
}

/** 403 is a disabled feature — an answer, not an error — so it gets the
 *  sentence instead of the raw message. */
function gateOrReason(e: unknown, fallback: string): string {
  if (e instanceof ApiError && e.status === 403) return `${e.message} — that feature is off for this workspace.`;
  return reason(e, fallback);
}

/**
 * Shaped like the real screen — header, four stats, tab bar, list — so the chunk
 * landing does not move anything. A generic PageSkeleton is a visible jump here
 * because the stat row is four wide, not three.
 */
export function SourcesSkeleton() {
  return (
    <div>
      <div className="flex items-start justify-between gap-4 mb-6">
        <div className="flex flex-col gap-2">
          <Skeleton className="h-6 w-32" />
          <Skeleton className="h-3.5 w-64" />
        </div>
        <Skeleton className="h-9 w-32 rounded-lg" />
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

      <div className="flex items-center gap-2 mt-6 mb-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-8 w-24 rounded-lg" />
        ))}
      </div>

      <Card className="overflow-hidden">
        {Array.from({ length: 5 }).map((_, i) => (
          <div key={i} className="flex items-center gap-4 px-4 py-3.5 border-b border-line last:border-0">
            <Skeleton className="h-4 w-56" />
            <Skeleton className="h-5 w-20 rounded-full" />
            <Skeleton className="h-4 w-16 ml-auto" />
            <Skeleton className="h-4 w-24" />
          </div>
        ))}
      </Card>
    </div>
  );
}

export function Sources() {
  const { agentId } = useAgent();
  return <SourcesScreen agentId={agentId} />;
}

/** The workspace mount at /knowledge: no agent scope, add flows on. */
export function Knowledge() {
  return <SourcesScreen />;
}

function SourcesScreen({ agentId }: { agentId?: string }) {
  const nav = useNavigate();
  // The workspace mount owns every add flow; the per-agent mount is a read-only
  // view of what is scoped to that agent (P2 — adding is universal now).
  const canAdd = agentId === undefined;

  const [tab, setTab] = useState<TabKey>("files");
  const [q, setQ] = useState("");
  const [websiteFilter, setWebsiteFilter] = useState<WebsiteFilter>("all");

  const [summary, setSummary] = useState<SourcesSummaryOut | null>(null);
  const [sources, setSources] = useState<SourceOut[]>([]);
  const [docs, setDocs] = useState<DocumentOut[]>([]);
  const [progress, setProgress] = useState<Record<string, CrawlProgressOut>>({});

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  /** Set when /v1/sources is not mounted yet. Drives the degraded copy, and is
   *  the difference between "we are broken" and "this is not on yet". */
  const [crawlerOff, setCrawlerOff] = useState(false);
  const [filesNote, setFilesNote] = useState("");
  const [retrainDismissed, setRetrainDismissed] = useState(false);
  const [busy, setBusy] = useState(false);

  const [addOpen, setAddOpen] = useState(false);
  const [confirmSource, setConfirmSource] = useState<SourceOut | null>(null);
  const [confirmDoc, setConfirmDoc] = useState<DocumentOut | null>(null);
  /** Inline error for the single-delete ConfirmDialogs — they stay open on a
   *  refusal so the reason belongs where the button is (mirrors AgentsIndex). */
  const [confirmError, setConfirmError] = useState("");
  const [confirmBulkDocs, setConfirmBulkDocs] = useState(false);
  const [confirmBulkSources, setConfirmBulkSources] = useState(false);
  const [bulkSourceTab, setBulkSourceTab] = useState<"text" | "qa">("text");

  /** The row currently retraining / reindexing, so that row's button spins. */
  const [busyId, setBusyId] = useState<string | null>(null);
  /** Single deletes keep their ConfirmDialog open while in flight, so Cancel
   *  stays locked and the Confirm button shows a spinner. */
  const [deletingSource, setDeletingSource] = useState(false);
  const [deletingDoc, setDeletingDoc] = useState(false);
  /** Bulk-loop progress. Non-null means a sequential delete is running; the
   *  sticky bulk bar swaps its controls for a ProgressBar while it does. */
  const [bulk, setBulk] = useState<{ total: number; done: number } | null>(null);

  const [selectedDocs, setSelectedDocs] = useState<Set<string>>(new Set());
  const [selectedText, setSelectedText] = useState<Set<string>>(new Set());
  const [selectedQa, setSelectedQa] = useState<Set<string>>(new Set());

  /** Degraded add-by-URL, per agents-UI §4.3: until the crawler ships, one page
   *  by URL through the existing POST /v1/documents/url is a truthful partial. */
  const [url, setUrl] = useState("");
  const [addingUrl, setAddingUrl] = useState(false);

  const [visible, setVisible] = useState(
    () => typeof document === "undefined" || document.visibilityState !== "hidden",
  );

  useEffect(() => {
    function onVisibility() { setVisible(document.visibilityState !== "hidden"); }
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  // The search box types faster than the network. `qQuery` trails `q` by 300ms
  // and is the only thing the sources request reads, so a five-letter search is
  // one call rather than five. The client-side filtering further down runs off
  // `q` directly, which is why the list still narrows on every keystroke.
  const [qQuery, setQQuery] = useState("");
  useEffect(() => {
    const t = window.setTimeout(() => setQQuery(q), 300);
    return () => clearTimeout(t);
  }, [q]);

  /* ----------------------------------------------------------------- fetches */

  const loadSummary = useCallback(async () => {
    // agent_id scopes the usage summary to this agent on the per-agent mount;
    // the /knowledge mount asks for the workspace-wide number. If the deployed
    // API rejects the param, fall back to the workspace-wide call rather than
    // losing it.
    const scoped = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : "";
    try {
      setSummary(await api.get<SourcesSummaryOut>(`/v1/sources/summary${scoped}`, tenantHeaders()));
    } catch (e) {
      if (e instanceof ApiError && (e.status === 422 || e.status === 400)) {
        try {
          setSummary(await api.get<SourcesSummaryOut>("/v1/sources/summary", tenantHeaders()));
          return;
        } catch (inner) {
          if (!notEnabled(inner)) setError(reason(inner, "could not read your usage"));
          setSummary(null);
          return;
        }
      }
      // The usage row degrades to counts derived from the lists below. Never an
      // error: a missing summary must not hide a working Files tab.
      if (!notEnabled(e)) setError(reason(e, "could not read your usage"));
      setSummary(null);
    }
  }, [agentId]);

  const loadSources = useCallback(async () => {
    // agent_id scopes this list to the agent on the per-agent mount; /knowledge
    // lists every source in the tenant. If the deployed API rejects the param,
    // fall back to the workspace-wide call rather than showing an empty screen.
    const params = new URLSearchParams({ limit: "100" });
    if (agentId) params.set("agent_id", agentId);
    if (qQuery.trim()) params.set("q", qQuery.trim());
    try {
      setSources(await api.get<SourceOut[]>(`/v1/sources?${params}`, tenantHeaders()));
      setCrawlerOff(false);
    } catch (e) {
      if (notEnabled(e)) { setCrawlerOff(true); setSources([]); return; }
      if (e instanceof ApiError && (e.status === 422 || e.status === 400)) {
        try {
          const fallback = new URLSearchParams({ limit: "100" });
          if (qQuery.trim()) fallback.set("q", qQuery.trim());
          setSources(await api.get<SourceOut[]>(`/v1/sources?${fallback}`, tenantHeaders()));
          setFilesNote("Showing every source in this workspace — per-agent filtering isn't available on this deployment yet.");
          setCrawlerOff(false);
          return;
        } catch (inner) {
          setError(reason(inner, "could not load your sources"));
          return;
        }
      }
      setError(reason(e, "could not load your sources"));
    }
  }, [qQuery, agentId]);

  const loadDocs = useCallback(async (opts?: { silent?: boolean }) => {
    // has_source=false keeps crawled pages out of the Files list; agent_id
    // scopes it to this agent on the per-agent mount. Both are new query
    // params — if the deployed API rejects them, fall back to today's call
    // rather than showing an empty Files tab.
    const params = new URLSearchParams({ limit: "200", has_source: "false" });
    if (agentId) params.set("agent_id", agentId);
    try {
      setDocs(await api.get<DocumentOut[]>(`/v1/documents?${params}`, tenantHeaders(), undefined, opts));
      setFilesNote("");
    } catch (e) {
      if (e instanceof ApiError && (e.status === 422 || e.status === 400)) {
        try {
          setDocs(await api.get<DocumentOut[]>("/v1/documents?limit=200", tenantHeaders(), undefined, opts));
          setFilesNote("Showing every file in this workspace — per-agent filtering isn't available on this deployment yet.");
          return;
        } catch (inner) {
          setError(reason(inner, "could not load your files"));
          return;
        }
      }
      setError(reason(e, "could not load your files"));
    }
  }, [agentId]);

  useEffect(() => {
    let live = true;
    // No setLoading(true) here on purpose: `loading` gates the first paint only.
    // A search or an agent switch refetches underneath the existing content
    // instead of blanking a working screen back to a skeleton.
    void (async () => {
      await Promise.all([loadSummary(), loadSources(), loadDocs()]);
      if (live) setLoading(false);
    })();
    return () => { live = false; };
  }, [loadSummary, loadSources, loadDocs]);

  /* -------------------------------------------------------------- crawl poll */

  // A string key, not the array: an id list that has not changed must not
  // restart the timer on every list refetch.
  const liveIds = useMemo(
    () => sources.filter((s) => LIVE_CRAWL.has(s.crawl_state)).map((s) => s.id).sort().join(","),
    [sources],
  );

  useEffect(() => {
    if (!liveIds || !visible) return;
    const ids = liveIds.split(",");
    const ctrl = new AbortController();
    let timer: number | undefined;
    let stopped = false;

    async function tick() {
      const readings = await Promise.all(ids.map(async (id) => {
        try {
          return [id, await api.get<CrawlProgressOut>(`/v1/sources/${id}/progress`, tenantHeaders(), ctrl.signal, { silent: true })] as const;
        } catch {
          // A single failed reading is not worth a visible error: the card keeps
          // its last known counters and the next tick corrects it.
          return [id, null] as const;
        }
      }));
      if (stopped) return;

      const landed = readings.filter((r): r is readonly [string, CrawlProgressOut] => r[1] !== null);
      if (landed.length) {
        setProgress((prev) => {
          const next = { ...prev };
          for (const [id, p] of landed) next[id] = p;
          return next;
        });
      }
      // Every crawl settled: refresh the real rows once so the cards show final
      // counters and a "Trained" badge, then let the timer die.
      if (landed.length === ids.length && landed.every(([, p]) => !LIVE_CRAWL.has(p.crawl_state))) {
        await Promise.all([loadSources(), loadSummary()]);
        return;
      }
      schedule();
    }

    function schedule() {
      if (!stopped) timer = window.setTimeout(() => { void tick(); }, 2000);
    }

    schedule();
    return () => {
      stopped = true;
      ctrl.abort();
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [liveIds, visible, loadSources, loadSummary]);

  // Files settle on their own clock. Same shape as Documents.tsx:53-57 — 3s, only
  // while something is still indexing, and a settled list is completely quiet.
  const docsSettling = docs.some((d) => PENDING_DOC.has(d.status));
  useEffect(() => {
    if (!docsSettling || !visible) return;
    const t = setInterval(() => { void loadDocs({ silent: true }); }, 3000);
    return () => clearInterval(t);
  }, [docsSettling, visible, loadDocs]);

  /* ---------------------------------------------------------------- mutation */

  function onUploaded(doc: DocumentOut, duplicate: boolean) {
    setDocs((prev) => [doc, ...prev.filter((d) => d.id !== doc.id)]);
    if (duplicate) setFilesNote("That file is already indexed — showing the existing copy.");
    void loadSummary();
  }

  async function addByUrl() {
    const target = url.trim();
    if (!target || addingUrl) return;
    setAddingUrl(true);
    try {
      const res = await api.post<UploadResponse>("/v1/documents/url", { url: target }, tenantHeaders());
      onUploaded(res.document, res.duplicate);
      setUrl("");
    } catch (e) {
      setError(gateOrReason(e, "could not fetch that URL"));
    } finally {
      setAddingUrl(false);
    }
  }

  function onCreated(created: SourceOut) {
    setSources((prev) => [created, ...prev.filter((s) => s.id !== created.id)]);
    setAddOpen(false);
    setTab(created.kind === "qa" ? "qa" : created.kind === "snippet" ? "text" : "website");
    void loadSummary();
  }

  function onSourceChanged(updated: SourceOut) {
    setSources((prev) => prev.map((s) => (s.id === updated.id ? updated : s)));
  }

  async function removeSource(source: SourceOut) {
    setDeletingSource(true);
    // "The source disappears from the list immediately" — optimistic, then the
    // next list read reconciles.
    setSources((prev) => prev.filter((s) => s.id !== source.id));
    try {
      await api.del(`/v1/sources/${source.id}`);
      setConfirmError("");
      setConfirmSource(null);
    } catch (e) {
      // Keep the dialog open with the refusal inline — closing it would hide the
      // one message the tenant needs (mirrors AgentsIndex).
      setConfirmError(reason(e, "could not delete that source"));
    } finally {
      setDeletingSource(false);
    }
    await Promise.all([loadSources(), loadSummary()]);
  }

  async function retrainSource(id: string) {
    setBusyId(id);
    try {
      await api.post(`/v1/sources/${id}/retrain`, undefined, tenantHeaders());
      await loadSources();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setError("That source is already being trained.");
      else if (e instanceof ApiError && e.status === 429) setError("Too many retrains just now — try again in a minute.");
      else setError(gateOrReason(e, "could not start that retrain"));
    } finally {
      setBusyId(null);
    }
  }

  async function retrainAll() {
    const stale = sources.filter((s) => s.pending_changes).slice(0, BULK_CAP);
    if (!stale.length || busy) return;
    setBusy(true);
    let failed = 0;
    for (const s of stale) {
      try {
        await api.post(`/v1/sources/${s.id}/retrain`, undefined, tenantHeaders());
      } catch {
        failed += 1;
      }
    }
    setBusy(false);
    if (failed) setError(`${failed} of ${stale.length} sources could not be retrained. Try them one at a time.`);
    setRetrainDismissed(true);
    await Promise.all([loadSources(), loadSummary()]);
  }

  async function reindexDoc(id: string) {
    setBusyId(id);
    try {
      await api.post(`/v1/documents/${id}/reindex`, undefined, tenantHeaders());
      await loadDocs();
    } catch (e) {
      setError(gateOrReason(e, "could not reindex that file"));
    } finally {
      setBusyId(null);
    }
  }

  async function removeDoc(doc: DocumentOut) {
    setDeletingDoc(true);
    setDocs((prev) => prev.filter((d) => d.id !== doc.id));
    try {
      await api.del(`/v1/documents/${doc.id}`);
      setConfirmError("");
      setConfirmDoc(null);
    } catch (e) {
      // Keep the dialog open with the refusal inline (mirrors AgentsIndex).
      setConfirmError(reason(e, "could not delete that file"));
    } finally {
      setDeletingDoc(false);
    }
    void loadSummary();
  }

  async function removeSelectedDocs() {
    const ids = [...selectedDocs].slice(0, BULK_CAP);
    // Optimistically remove only the ids actually sent — when the selection
    // overflows BULK_CAP, the overflow rows must stay on screen (they reappear
    // after the reload anyway, and a phantom removal would mislead the count).
    const idSet = new Set(ids);
    setDocs((prev) => prev.filter((d) => !idSet.has(d.id)));
    setBulk({ total: ids.length, done: 0 });
    let failed = 0;
    let done = 0;
    for (const id of ids) {
      try { await api.del(`/v1/documents/${id}`); } catch { failed += 1; }
      done += 1;
      setBulk((b) => (b === null ? b : { ...b, done }));
    }
    setBulk(null);
    setConfirmBulkDocs(false);
    setSelectedDocs(new Set());
    if (failed) setError(`${failed} of ${ids.length} files could not be deleted.`);
    await Promise.all([loadDocs(), loadSummary()]);
  }

  async function deleteSelectedSources(ids: Set<string>, clear: () => void) {
    const list = [...ids].slice(0, BULK_CAP);
    const listSet = new Set(list);
    setSources((prev) => prev.filter((s) => !listSet.has(s.id)));
    setBulk({ total: list.length, done: 0 });
    let failed = 0;
    let done = 0;
    for (const id of list) {
      try { await api.del(`/v1/sources/${id}`); } catch { failed += 1; }
      done += 1;
      setBulk((b) => (b === null ? b : { ...b, done }));
    }
    setBulk(null);
    clear();
    setConfirmBulkSources(false);
    if (failed) setError(`${failed} of ${list.length} sources could not be deleted.`);
    await Promise.all([loadSources(), loadSummary()]);
  }

  /* ------------------------------------------------------------------ derived */

  const websites = sources.filter(isWebsite);
  const snippets = sources.filter((s) => s.kind === "snippet");
  const qaSets = sources.filter((s) => s.kind === "qa");

  const needle = q.trim().toLowerCase();
  const matchesSource = (s: SourceOut) =>
    !needle
    || s.title.toLowerCase().includes(needle)
    || (s.seed_url ?? "").toLowerCase().includes(needle)
    || (s.origin ?? "").toLowerCase().includes(needle);

  const shownDocs = needle ? docs.filter((d) => d.filename.toLowerCase().includes(needle)) : docs;
  const shownWebsites = websites.filter((s) => {
    if (websiteFilter === "crawling" && !LIVE_CRAWL.has(s.crawl_state)) return false;
    if (websiteFilter === "review" && s.crawl_state !== "awaiting_review") return false;
    if (websiteFilter === "retrain" && !s.pending_changes) return false;
    return matchesSource(s);
  });
  const shownSnippets = snippets.filter(matchesSource);
  const shownQa = qaSets.filter(matchesSource);

  // Every stat prefers the server's number and falls back to what is on screen,
  // so the row is never four em dashes just because /summary is not mounted yet.
  const sourceCount = summary
    ? Object.values(summary.by_kind).reduce((n, k) => n + k.sources, 0)
    : sources.length + docs.length;
  const pagesIndexed = summary?.documents
    ?? docs.length + websites.reduce((n, s) => n + s.link_trained, 0);
  const storageBytes = summary?.storage_bytes
    ?? docs.reduce((n, d) => n + d.size_bytes, 0) + sources.reduce((n, s) => n + s.size_bytes, 0);
  const needsRetrain = summary?.needs_retrain ?? sources.filter((s) => s.pending_changes).length;
  const anyCancelled = websites.some((s) => s.crawl_state === "cancelled");
  const awaitingReviewCount = websites.filter((s) => s.crawl_state === "awaiting_review").length;

  // Collections already in use, so the add modal and the cards can offer them
  // instead of asking the tenant to retype a string that must match exactly.
  const collections = [...new Set(
    [...docs.map((d) => d.collection), ...sources.map((s) => s.collection)].filter((c): c is string => !!c),
  )].sort();

  /* ------------------------------------------------------------------ columns */

  const docColumns: Column<DocumentOut>[] = [
    {
      key: "filename", header: "File", sortValue: (d) => d.filename,
      render: (d) => (
        <a
          onClick={() => nav(`/knowledge/document/${d.id}`)}
          className="text-brand font-medium cursor-pointer hover:underline"
        >
          {d.filename}
        </a>
      ),
    },
    { key: "status", header: "Status", sortValue: (d) => d.status, render: (d) => <StatusBadge status={d.status} reason={d.error} /> },
    { key: "size", header: "Size", sortValue: (d) => d.size_bytes, render: (d) => <span className="tabular-nums">{bytes(d.size_bytes)}</span> },
    { key: "pages", header: "Pages", sortValue: (d) => d.page_count, render: (d) => <span className="tabular-nums">{d.page_count || "—"}</span> },
    { key: "chunks", header: "Passages", sortValue: (d) => d.chunk_count, render: (d) => <span className="tabular-nums">{d.chunk_count || "—"}</span> },
    { key: "collection", header: "Collection", sortValue: (d) => d.collection ?? "", render: (d) => d.collection || "—" },
    { key: "created", header: "Added", sortValue: (d) => d.created_at ?? "", render: (d) => relativeTime(d.created_at) },
    {
      key: "actions", header: "", align: "right", render: (d) => (
        <div className="flex items-center justify-end gap-2">
          {(d.status === "failed" || d.status === "ready") && (
            <Button size="sm" variant="ghost" loading={busyId === d.id} disabled={busyId !== null} onClick={() => void reindexDoc(d.id)}>
              {d.status === "failed" ? "Retry" : "Reindex"}
            </Button>
          )}
          <Button size="sm" variant="danger" disabled={busyId !== null} onClick={() => { setConfirmError(""); setConfirmDoc(d); }}>Delete</Button>
        </div>
      ),
    },
  ];

  function sourceActions(s: SourceOut) {
    return (
      <div className="flex items-center justify-end gap-2">
        <Button size="sm" variant="ghost" onClick={() => nav(`/knowledge/sources/${s.id}`)}>Edit</Button>
        <Button
          size="sm"
          variant="ghost"
          loading={busyId === s.id}
          disabled={LIVE_CRAWL.has(s.crawl_state) || busyId !== null}
          onClick={() => void retrainSource(s.id)}
        >
          Retrain
        </Button>
        <Button size="sm" variant="danger" disabled={busyId !== null} onClick={() => { setConfirmError(""); setConfirmSource(s); }}>Delete</Button>
      </div>
    );
  }

  const textColumns: Column<SourceOut>[] = [
    {
      key: "title", header: "Title", sortValue: (s) => s.title,
      render: (s) => (
        <a
          onClick={() => nav(`/knowledge/sources/${s.id}`)}
          className="text-brand font-medium cursor-pointer hover:underline"
        >
          {s.title || "Untitled snippet"}
        </a>
      ),
    },
    {
      key: "status", header: "Status", sortValue: (s) => s.crawl_state,
      render: (s) => (
        <div className="flex items-center gap-1.5">
          <StatusBadge status={s.crawl_state} reason={s.error} crawl />
          {s.pending_changes && <Badge tone="brand">Changes available</Badge>}
        </div>
      ),
    },
    { key: "size", header: "Size", sortValue: (s) => s.size_bytes, render: (s) => <span className="tabular-nums">{bytes(s.size_bytes)}</span> },
    { key: "chunks", header: "Passages", sortValue: (s) => s.chunk_count, render: (s) => <span className="tabular-nums">{s.chunk_count || "—"}</span> },
    { key: "updated", header: "Updated", sortValue: (s) => s.last_trained_at ?? s.created_at ?? "", render: (s) => relativeTime(s.last_trained_at ?? s.created_at) },
    { key: "actions", header: "", align: "right", render: sourceActions },
  ];

  // §7 asks for Times asked / Last asked here. Those live on QaPairOut, one row
  // per pair, and the list endpoint returns the parent source — so the count of
  // pairs stands in and the per-pair stats are on the review screen.
  const qaColumns: Column<SourceOut>[] = [
    {
      key: "title", header: "Title", sortValue: (s) => s.title,
      render: (s) => (
        <a
          onClick={() => nav(`/knowledge/sources/${s.id}`)}
          className="text-brand font-medium cursor-pointer hover:underline"
        >
          {s.title || "Untitled Q&A set"}
        </a>
      ),
    },
    { key: "pairs", header: "Pairs", sortValue: (s) => s.link_total, render: (s) => <span className="tabular-nums">{s.link_total || s.chunk_count || "—"}</span> },
    {
      key: "status", header: "Status", sortValue: (s) => s.crawl_state,
      render: (s) => (
        <div className="flex items-center gap-1.5">
          <StatusBadge status={s.crawl_state} reason={s.error} crawl />
          {s.pending_changes && <Badge tone="brand">Changes available</Badge>}
        </div>
      ),
    },
    { key: "size", header: "Size", sortValue: (s) => s.size_bytes, render: (s) => <span className="tabular-nums">{bytes(s.size_bytes)}</span> },
    { key: "updated", header: "Updated", sortValue: (s) => s.last_trained_at ?? s.created_at ?? "", render: (s) => relativeTime(s.last_trained_at ?? s.created_at) },
    { key: "actions", header: "", align: "right", render: sourceActions },
  ];

  /* ------------------------------------------------------------------- render */

  if (loading) return <SourcesSkeleton />;

  const searchBox = (
    <div className="flex-1">
      <SearchInput
        placeholder="Search sources…"
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />
    </div>
  );

  /** The truthful degradation from agents-UI §4.3: one page by URL, through an
   *  endpoint that exists today, while the crawler is still landing. */
  const addByUrlRow = (
    <div className="flex items-center gap-2">
      <div className="relative flex-1">
        <span className="absolute left-3 top-1/2 -translate-y-1/2 text-faint"><Icon.Link size={16} /></span>
        <Input
          type="url"
          placeholder="Add a single page by URL (https://…)"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") void addByUrl(); }}
          className="pl-9"
          disabled={addingUrl}
        />
      </div>
      <Button variant="primary" onClick={() => void addByUrl()} loading={addingUrl} disabled={addingUrl || !url.trim()}>
        {addingUrl ? "Fetching…" : "Add page"}
      </Button>
    </div>
  );

  const crawlerOffNotice = (
    <Card>
      <EmptyState
        icon={<Icon.Globe size={22} />}
        title="Website crawling isn't switched on yet"
        description="It's arriving shortly. In the meantime you can add one page at a time by URL, and everything you upload under Files is answerable straight away."
        footer={canAdd ? <div className="w-full max-w-md">{addByUrlRow}</div> : undefined}
      />
    </Card>
  );

  function bulkBar(count: number, clear: () => void, onDelete: () => void, extra?: React.ReactNode) {
    if (count === 0 && bulk === null) return null;
    return (
      <Card className="sticky bottom-4 shadow-pop">
        <CardBody className="flex flex-col gap-3">
          {bulk !== null ? (
            <ProgressBar value={bulk.done} max={bulk.total} sub={`Deleting ${bulk.done} of ${bulk.total}`} />
          ) : (
            <div className="flex items-center gap-3">
              <span className="text-sm font-medium tabular-nums">{count} selected</span>
              {count >= BULK_CAP && <span className="text-[12px] text-faint">Capped at {BULK_CAP} per action.</span>}
              <div className="ml-auto flex items-center gap-2">
                {extra}
                <Button size="sm" variant="danger" onClick={onDelete}>Delete</Button>
                <Button size="sm" variant="ghost" onClick={clear} aria-label="Clear selection"><Icon.X size={14} /></Button>
              </div>
            </div>
          )}
        </CardBody>
      </Card>
    );
  }

  return (
    <div>
      <PageHeader
        title={canAdd ? "Knowledge" : "Sources"}
        description={canAdd
          ? "Everything every assistant in this workspace can answer from."
          : "What this assistant was given. Adding is workspace-wide now — new sources go to Knowledge."}
        actions={canAdd ? (
          <Button variant="primary" onClick={() => setAddOpen(true)}>
            <Icon.Plus size={16} /> Add source
          </Button>
        ) : undefined}
      />

      <div className="flex flex-col gap-4">
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <StatCard label="Sources" value={<span className="tabular-nums">{sourceCount}</span>} />
          <StatCard label="Pages indexed" value={<span className="tabular-nums">{pagesIndexed}</span>} />
          <StatCard label="Storage" value={<span className="tabular-nums">{bytes(storageBytes)}</span>} />
          <StatCard
            label="Needs retrain"
            value={<span className="tabular-nums">{needsRetrain}</span>}
            tone={needsRetrain > 0 ? "warn" : undefined}
            sub={needsRetrain > 0 ? "Changed since last trained" : "Everything is current"}
          />
        </div>

        {needsRetrain > 0 && !retrainDismissed && (
          <Banner
            tone="warn"
            icon={<Icon.Alert size={16} />}
            action={<Button size="sm" onClick={() => void retrainAll()} loading={busy}>Retrain all</Button>}
            onDismiss={() => setRetrainDismissed(true)}
          >
            {needsRetrain} {needsRetrain === 1 ? "source has" : "sources have"} changed since they were last trained.
          </Banner>
        )}

        {error && <Alert>{error}</Alert>}

        <Tabs
          value={tab}
          onChange={(v) => setTab(v as TabKey)}
          items={[
            { value: "files", label: <>Files <span className="tabular-nums opacity-60">{docs.length}</span></>, icon: <Icon.Doc size={15} /> },
            { value: "website", label: <>Website <span className="tabular-nums opacity-60">{websites.length}</span></>, icon: <Icon.Globe size={15} /> },
            { value: "text", label: <>Text <span className="tabular-nums opacity-60">{snippets.length}</span></>, icon: <Icon.Book size={15} /> },
            { value: "qa", label: <>Q&amp;A <span className="tabular-nums opacity-60">{qaSets.length}</span></>, icon: <Icon.Chat size={15} /> },
          ]}
        />

        {tab === "files" && (
          <>
            {canAdd && (
              <Card>
                <CardHeader title="Add files" description="Drop documents to index, or pull one in from a URL." />
                <CardBody className="flex flex-col gap-4">
                  <DocumentUpload onUploaded={onUploaded} />
                  {addByUrlRow}
                </CardBody>
              </Card>
            )}

            {filesNote && <Alert tone="warn">{filesNote}</Alert>}

            <div className="flex items-center gap-2">{searchBox}</div>

            {shownDocs.length === 0 ? (
              <Card>
                <EmptyState
                  icon={<Icon.Doc size={22} />}
                  title={needle ? "No files match that search" : "No files yet"}
                  description={needle
                    ? "Try a shorter search, or clear it to see everything."
                    : canAdd
                      ? "Upload a file or add a page by URL to start building what your assistants know."
                      : "Nothing has been added for this assistant yet. New sources are added from Knowledge."}
                  action={needle ? <Button variant="secondary" onClick={() => setQ("")}>Clear search</Button> : undefined}
                />
              </Card>
            ) : (
              <Card>
                <DataTable
                  columns={docColumns}
                  rows={shownDocs}
                  selectable
                  getRowId={(d) => d.id}
                  selected={selectedDocs}
                  onSelectedChange={setSelectedDocs}
                  empty="No files yet."
                />
              </Card>
            )}

            {bulkBar(selectedDocs.size, () => setSelectedDocs(new Set()), () => setConfirmBulkDocs(true))}
          </>
        )}

        {tab === "website" && (
          <>
            {crawlerOff ? crawlerOffNotice : (
              <>
                <div className="flex items-center gap-2">
                  {searchBox}
                  <SegmentedControl
                    value={websiteFilter}
                    onChange={(v) => setWebsiteFilter(v as WebsiteFilter)}
                    items={[
                      { value: "all", label: "All" },
                      { value: "crawling", label: "Crawling" },
                      { value: "review", label: "Needs review" },
                      { value: "retrain", label: "Needs retrain" },
                    ]}
                  />
                </div>

                {awaitingReviewCount > 0 && (
                  <Alert tone="warn">
                    {awaitingReviewCount === 1
                      ? "One website has pages waiting for your review."
                      : `${awaitingReviewCount} websites have pages waiting for your review.`}
                    {" "}Nothing from them is answerable until you open the list and index it.
                  </Alert>
                )}

                {anyCancelled && (
                  <Alert tone="warn">
                    A cancelled crawl keeps everything it had already trained — those pages stay answerable.
                    Retrain when you want the rest of the site.
                  </Alert>
                )}

                {shownWebsites.length === 0 ? (
                  <Card>
                    <EmptyState
                      icon={<Icon.Link size={22} />}
                      title={websites.length === 0 ? "No websites yet" : "Nothing matches those filters"}
                      description={websites.length === 0
                        ? "Point us at your site and we'll read every public page, then keep them current."
                        : "Clear the search or switch back to All."}
                      action={websites.length === 0
                        ? (canAdd
                          ? <Button variant="primary" onClick={() => setAddOpen(true)}>Add your website</Button>
                          : undefined)
                        : <Button variant="secondary" onClick={() => { setQ(""); setWebsiteFilter("all"); }}>Clear filters</Button>}
                    />
                  </Card>
                ) : (
                  <div className="flex flex-col gap-3">
                    {shownWebsites.map((s) => (
                      <SourceCard
                        key={s.id}
                        source={s}
                        progress={progress[s.id] ?? null}
                        pollingPaused={!visible}
                        collections={collections}
                        onChanged={onSourceChanged}
                        onDeleted={() => { void removeSource(s); }}
                      />
                    ))}
                  </div>
                )}
              </>
            )}
          </>
        )}

        {tab === "text" && (
          crawlerOff ? (
            <Card>
              <EmptyState
                icon={<Icon.Book size={22} />}
                title="Text snippets aren't switched on yet"
                description="They're arriving shortly. Until then, put short answers in a file under Files, or write them straight into Instructions."
              />
            </Card>
          ) : (
            <>
              <div className="flex items-center gap-2">{searchBox}</div>
              {shownSnippets.length === 0 ? (
                <Card>
                  <EmptyState
                    icon={<Icon.Book size={22} />}
                    title={snippets.length === 0 ? "No text snippets yet" : "Nothing matches that search"}
                    description={snippets.length === 0
                      ? "A snippet is the fastest way to teach one fact — opening hours, a return window, a price."
                      : "Try a shorter search."}
                    action={snippets.length === 0
                      ? (canAdd
                        ? <Button variant="primary" onClick={() => setAddOpen(true)}>Write a snippet</Button>
                        : undefined)
                      : <Button variant="secondary" onClick={() => setQ("")}>Clear search</Button>}
                  />
                </Card>
              ) : (
                <Card>
                  <DataTable
                    columns={textColumns}
                    rows={shownSnippets}
                    selectable
                    getRowId={(s) => s.id}
                    selected={selectedText}
                    onSelectedChange={setSelectedText}
                    empty="No text snippets yet."
                  />
                </Card>
              )}
              {bulkBar(
                selectedText.size,
                () => setSelectedText(new Set()),
                () => { setBulkSourceTab("text"); setConfirmBulkSources(true); },
              )}
            </>
          )
        )}

        {tab === "qa" && (
          crawlerOff ? (
            <Card>
              <EmptyState
                icon={<Icon.Chat size={22} />}
                title="Q&A pairs aren't switched on yet"
                description="They're arriving shortly. Until then, a file of questions and answers under Files does the same job less precisely."
              />
            </Card>
          ) : (
            <>
              <div className="flex items-center gap-2">{searchBox}</div>
              {shownQa.length === 0 ? (
                <Card>
                  <EmptyState
                    icon={<Icon.Chat size={22} />}
                    title={qaSets.length === 0 ? "No Q&A sets yet" : "Nothing matches that search"}
                    description={qaSets.length === 0
                      ? "Write the twenty questions you get most and the exact answers you want given. These beat every other source when they match."
                      : "Try a shorter search."}
                    action={qaSets.length === 0
                      ? (canAdd
                        ? <Button variant="primary" onClick={() => setAddOpen(true)}>Add Q&amp;A pairs</Button>
                        : undefined)
                      : <Button variant="secondary" onClick={() => setQ("")}>Clear search</Button>}
                  />
                </Card>
              ) : (
                <Card>
                  <DataTable
                    columns={qaColumns}
                    rows={shownQa}
                    selectable
                    getRowId={(s) => s.id}
                    selected={selectedQa}
                    onSelectedChange={setSelectedQa}
                    empty="No Q&A sets yet."
                  />
                </Card>
              )}
              {bulkBar(
                selectedQa.size,
                () => setSelectedQa(new Set()),
                () => { setBulkSourceTab("qa"); setConfirmBulkSources(true); },
              )}
            </>
          )
        )}
      </div>

      {canAdd && (
        <AddSourceModal
          open={addOpen}
          onClose={() => setAddOpen(false)}
          onCreated={onCreated}
          collections={collections}
          onImported={(result) => {
            // The .xlsx path returns a row report, not a source: land on the Q&A
            // tab so the created pairs are visible, and say what was skipped.
            setAddOpen(false);
            setTab("qa");
            if (result.errors.length) {
              setError(`${result.created} pairs imported, ${result.errors.length} rows skipped. The columns must be Title, Question, Answer.`);
            }
            void loadSources();
            void loadSummary();
          }}
        />
      )}

      {confirmSource && (
        <ConfirmDialog
          title="Delete source"
          message={`Remove "${confirmSource.title}" and everything indexed from it. Your assistant stops answering from it immediately. This cannot be undone.`}
          confirmWord={confirmSource.title}
          danger
          busy={deletingSource}
          error={confirmError || undefined}
          onConfirm={() => void removeSource(confirmSource)}
          onCancel={() => { setConfirmError(""); setConfirmSource(null); }}
        />
      )}

      {confirmDoc && (
        <ConfirmDialog
          title="Delete file"
          message={`Remove "${confirmDoc.filename}" and its passages. This cannot be undone.`}
          confirmWord={confirmDoc.filename}
          danger
          busy={deletingDoc}
          error={confirmError || undefined}
          onConfirm={() => void removeDoc(confirmDoc)}
          onCancel={() => { setConfirmError(""); setConfirmDoc(null); }}
        />
      )}

      {confirmBulkDocs && (
        <ConfirmDialog
          title={`Delete ${Math.min(selectedDocs.size, BULK_CAP)} files`}
          message={`Remove ${Math.min(selectedDocs.size, BULK_CAP)} files and their passages. Your assistant stops answering from them immediately. This cannot be undone.`}
          confirmWord="delete"
          danger
          busy={bulk !== null}
          onConfirm={() => void removeSelectedDocs()}
          onCancel={() => setConfirmBulkDocs(false)}
        />
      )}

      {confirmBulkSources && (
        <ConfirmDialog
          title={`Delete ${Math.min(bulkSourceTab === "text" ? selectedText.size : selectedQa.size, BULK_CAP)} sources`}
          message={`Remove ${Math.min(bulkSourceTab === "text" ? selectedText.size : selectedQa.size, BULK_CAP)} sources and everything indexed from them. Your assistant stops answering from them immediately. This cannot be undone.`}
          confirmWord="delete"
          danger
          busy={bulk !== null}
          onConfirm={() => {
            if (bulkSourceTab === "text") void deleteSelectedSources(selectedText, () => setSelectedText(new Set()));
            else void deleteSelectedSources(selectedQa, () => setSelectedQa(new Set()));
          }}
          onCancel={() => setConfirmBulkSources(false)}
        />
      )}
    </div>
  );
}
