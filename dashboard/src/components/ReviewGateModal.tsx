/**
 * The approval gate: every URL a crawl is about to index, grouped by host and
 * path, every one of them checked, with one button that starts the indexing and
 * no way to start it by accident.
 *
 * Why it exists. A crawl used to run straight through — discover, fetch, extract,
 * embed — and the tenant saw the URL list only afterwards. By then a careers page,
 * an Instagram mirror and four hundred archived posts were already chunked and
 * retrievable, and every one of them is a chance for the assistant to answer from
 * the wrong page. Removing them at that point is a deletion. Removing them here
 * costs nothing: the pages are fetched, but nothing has been embedded.
 *
 * Two things follow from that, and both are deliberate:
 *
 *   * The list arrives complete. The crawl already worked out what is on the site,
 *     so unchecking is the only action — nobody re-types the paths they want.
 *     Unchecking is free until the last button, because it is only local state.
 *   * Nothing here indexes anything until "Index …" is pressed. Closing the window
 *     leaves the crawl exactly where it was, waiting, and nothing is excluded.
 *
 * The tree comes from the server already grouped (`crawl.group_urls`), so the
 * counts on a section header and the counts the crawl acts on cannot disagree.
 */
import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "../lib/api";
import { tenantHeaders } from "../lib/auth";
import type { SourceOut, SourceReviewOut } from "../lib/types";
import { ConfirmDialog } from "./ConfirmDialog";
import { LinkTree } from "./LinkTree";
import { Alert, Banner, Button, Modal, Spinner } from "./ui";

// the server excludes in one statement, up to BULK_MAX_LINKS ids per request, so a
// selection larger than this is removed in several — see POST /links/bulk
const BULK_CAP = 500;

function chunk<T>(items: T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

export function ReviewGateModal({
  open,
  sourceId,
  sourceTitle,
  onClose,
  onApproved,
}: {
  open: boolean;
  sourceId: string;
  sourceTitle?: string | null;
  onClose: () => void;
  /**
   * Called after the crawl has been released, with the source as the server left
   * it — already `extracting`, or `failed` if the queue refused the wave. The
   * caller applies it rather than guessing, the same way Retrain does.
   */
  onApproved?: (next: SourceOut) => void;
}) {
  const [data, setData] = useState<SourceReviewOut | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const [asking, setAsking] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(
        await api.get<SourceReviewOut>(`/v1/sources/${sourceId}/review`, tenantHeaders()),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "could not load the page list");
    } finally {
      setLoading(false);
    }
  }, [sourceId]);

  // re-read on every open rather than caching: the crawl may have finished
  // fetching, or another tab may have removed pages, since this was last seen
  useEffect(() => {
    if (!open) return;
    setExcluded(new Set());
    setAsking(false);
    void load();
  }, [open, load]);

  // url → link id, because the tree carries URLs and the exclude endpoint takes ids
  const idByUrl = useMemo(() => {
    const map = new Map<string, string>();
    for (const link of data?.links ?? []) map.set(link.url, link.id);
    return map;
  }, [data]);

  const total = data?.total ?? 0;
  const included = total - excluded.size;
  const waiting = data?.crawl_state === "awaiting_review";

  /**
   * Exclude what was unchecked, then release the crawl. In that order, and both in
   * one action: an exclusion applied without the approval that follows it would
   * leave the tenant having dropped pages from a crawl they then abandoned.
   */
  async function confirm() {
    setAsking(false);
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const ids = [...excluded]
        .map((url) => idByUrl.get(url))
        .filter((id): id is string => !!id);
      for (const batch of chunk(ids, BULK_CAP)) {
        await api.post<{ updated: number }>(
          `/v1/sources/${sourceId}/links/bulk`,
          // `review_excluded` rather than the default `manual`: same decision, but
          // the review list can then say the page was dropped before indexing
          { link_ids: batch, action: "exclude", reason: "review_excluded" },
          tenantHeaders(),
        );
      }
      // the response carries the whole source, plus `status`/`source_id`, exactly
      // like Retrain — so the caller can update its row without a second request
      const next = await api.post<SourceOut>(
        `/v1/sources/${sourceId}/approve`,
        undefined,
        tenantHeaders(),
      );
      onApproved?.(next);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "could not start indexing");
      setBusy(false);
    }
  }

  return (
    <>
      <Modal
        open={open}
        onClose={onClose}
        size="lg"
        title={sourceTitle ? `Review pages from ${sourceTitle}` : "Here are the links we found"}
        description={
          waiting
            ? "These pages have been read but not added to your assistant. Uncheck anything that does not belong, then index the rest."
            : "The pages this source has read. There is nothing waiting to be approved right now."
        }
        footer={
          <>
            <Button variant="secondary" onClick={onClose} disabled={busy}>
              {waiting ? "Decide later" : "Close"}
            </Button>
            {waiting && (
              <Button onClick={() => setAsking(true)} disabled={busy || loading || included === 0}>
                {busy ? <Spinner /> : `Index ${plural(included, "page", "pages")}`}
              </Button>
            )}
          </>
        }
      >
        {loading && !data ? (
          <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted">
            <Spinner /> Loading the page list…
          </div>
        ) : (
          <div className="space-y-3">
            {error && <Alert>{error}</Alert>}
            {data?.truncated && (
              <Banner tone="warn">
                This list is longer than one screen. You are seeing the first {total} pages;
                indexing still covers every page that was read.
              </Banner>
            )}
            {data && !data.review_before_index && (
              <Banner tone="neutral">
                This source is not set to ask for review. Turn on <b>Review pages before
                indexing</b> in its settings to be asked next time.
              </Banner>
            )}
            {(data?.excluded ?? 0) > 0 && (
              <p className="m-0 text-xs text-muted">
                {data?.excluded} page{(data?.excluded ?? 0) === 1 ? " was" : "s were"} already
                excluded earlier and are not shown.
              </p>
            )}

            <LinkTree
              groups={data?.groups ?? []}
              links={data?.links}
              excluded={excluded}
              onExcludedChange={setExcluded}
              disabled={busy}
              emptyMessage="No pages are waiting. Nothing was read, or everything was removed."
            />
          </div>
        )}
      </Modal>

      {/* Rendered outside the Modal so its own overlay paints above it, and a click
          on this overlay dismisses only the question it asked. */}
      {asking && (
        <ConfirmDialog
          title={`Index ${plural(included, "page", "pages")}?`}
          message={
            excluded.size > 0
              ? `These pages are added to your assistant and start answering questions. The ${plural(
                  excluded.size,
                  "page",
                  "pages",
                )} you unchecked will be skipped now and on future crawls, and can be put back from the pages list.`
              : "Every page in this list is added to your assistant and starts answering questions."
          }
          onConfirm={() => void confirm()}
          onCancel={() => setAsking(false)}
        />
      )}
    </>
  );
}
