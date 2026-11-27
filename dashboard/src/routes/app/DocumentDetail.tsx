// Extracted-markdown preview per document — the trust feature for OCR quality and
// the fastest way a tenant self-diagnoses a bad answer. Shows per-tier page counts.
import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../../lib/api";
import { tenantHeaders } from "../../lib/auth";
import type { DocumentOut } from "../../lib/types";
import { bytes, relativeTime } from "../../lib/format";
import { StatusBadge } from "../../components/StatusBadge";
import { PageHeader, Card, CardHeader, CardBody, StatCard, Button, Input, Textarea, Field, Alert, Tooltip, EmptyState, Skeleton, Spinner } from "../../components/ui";
import { Icon } from "../../components/ui/icons";

export function DocumentDetail({ backTo: backToProp }: { backTo?: string } = {}) {
  const { id, agentId } = useParams<{ id: string; agentId?: string }>();
  const nav = useNavigate();
  // This screen mounts at /knowledge/document/:id (the agent-scoped mount is
  // gone — P2 made document detail workspace-level). agentId is therefore never
  // present on the live route and the shim is the only caller that could pass
  // one; going "back" lands on Knowledge either way. R8: a caller that already
  // knows where the user came from can override the derived route.
  const backTo = backToProp ?? (agentId ? `/agents/${agentId}/sources` : "/knowledge");
  const [doc, setDoc] = useState<DocumentOut | null>(null);
  const [error, setError] = useState("");
  const [opening, setOpening] = useState(false);
  // editable retrieval facets, buffered locally until Save
  const [tags, setTags] = useState("");
  const [collection, setCollection] = useState("");
  const [metadata, setMetadata] = useState("");
  const [savingFacets, setSavingFacets] = useState(false);
  const [facetError, setFacetError] = useState("");

  useEffect(() => {
    if (!id) return;
    api.get<DocumentOut>(`/v1/documents/${id}`, tenantHeaders())
      .then((d) => { setDoc(d); resetFacets(d); })
      .catch((e) => setError(e instanceof ApiError ? e.message : "could not load document"));
  }, [id]);

  function resetFacets(d: DocumentOut) {
    setTags((d.tags ?? []).join(", "));
    setCollection(d.collection ?? "");
    setMetadata(JSON.stringify(d.metadata ?? {}, null, 2));
  }

  async function saveFacets() {
    if (!id || savingFacets) return;
    let parsed: Record<string, unknown>;
    try {
      parsed = metadata.trim() ? JSON.parse(metadata) : {};
    } catch {
      setFacetError("Metadata must be valid JSON.");
      return;
    }
    setSavingFacets(true);
    setFacetError("");
    try {
      const updated = await api.patch<DocumentOut>(`/v1/documents/${id}`, {
        tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
        collection: collection.trim() || null,
        metadata: parsed,
      });
      setDoc(updated);
      resetFacets(updated);
    } catch (e) {
      setFacetError(e instanceof ApiError ? e.message : "could not save");
    } finally {
      setSavingFacets(false);
    }
  }

  async function openSource() {
    if (!id) return;
    setOpening(true);
    try {
      const res = await api.get<{ url: string }>(`/v1/documents/${id}/source`, tenantHeaders());
      window.open(res.url, "_blank", "noopener");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "could not open source");
    } finally {
      setOpening(false);
    }
  }

  if (error) return (
    <div>
      <button onClick={() => nav(backTo)} className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-ink mb-4 bg-transparent border-0 cursor-pointer">
        <Icon.Chevron size={16} className="rotate-90" /> Sources
      </button>
      <Alert>{error}</Alert>
    </div>
  );
  if (!doc) return (
    <div>
      <button onClick={() => nav(backTo)} className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-ink mb-4 bg-transparent border-0 cursor-pointer">
        <Icon.Chevron size={16} className="rotate-90" /> Sources
      </button>
      <Skeleton className="h-8 w-72 mb-3" />
      <Skeleton className="h-4 w-56 mb-6" />
      <div className="grid gap-4 mb-6" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))" }}>
        <Skeleton className="h-20" />
        <Skeleton className="h-20" />
        <Skeleton className="h-20" />
        <Skeleton className="h-20" />
      </div>
      <div className="flex flex-col gap-6">
        <Skeleton className="h-48 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    </div>
  );

  const tiers = Object.entries(doc.tier_counts ?? {});
  // B10 blocks the extracted-markdown preview, so tier_counts is the only
  // extraction-quality fact available today. It gets said once, in plain words,
  // at the top of the screen — a tenant diagnosing a wrong answer should not
  // have to decode tier numbers in a table further down to learn the page was a
  // scan. When the full preview lands this line becomes its summary, not a
  // thing that disappears.
  const read = describeRead(tiers);

  return (
    <div>
      <button onClick={() => nav(backTo)} className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-ink mb-4 bg-transparent border-0 cursor-pointer">
        <Icon.Chevron size={16} className="rotate-90" /> Sources
      </button>

      <PageHeader
        title={doc.filename}
        actions={<Button variant="secondary" onClick={openSource} loading={opening}><Icon.External size={16} /> Open original</Button>}
      />
      <div className="flex items-center gap-3 -mt-3 mb-6">
        <StatusBadge status={doc.status} reason={doc.error} />
        <span className="text-sm text-muted">
          Added {relativeTime(doc.created_at)}{doc.indexed_at ? ` · indexed ${relativeTime(doc.indexed_at)}` : ""}
        </span>
      </div>

      {read && (
        <div className="mb-6">
          <Alert tone={read.tone}>{read.text}</Alert>
        </div>
      )}

      {doc.error && <div className="mb-6"><Alert>{doc.error}</Alert></div>}

      <div className="grid gap-4 mb-6" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))" }}>
        <StatCard label="Pages" value={doc.page_count} />
        <StatCard label="Chunks indexed" value={doc.chunk_count} />
        <StatCard label="Size" value={bytes(doc.size_bytes)} />
        <StatCard label="Type" value={<span className="text-base">{doc.mime}</span>} />
      </div>

      <div className="flex flex-col gap-6">
        <Card>
          <CardHeader
            title="How each page was read"
            description="A page read straight from a text layer is exact. A page that had to be scanned with OCR or read with AI vision can come back with wrong characters — worth checking if answers from this document look off."
          />
          <CardBody>
            {tiers.length === 0 ? (
              <EmptyState title="No per-page tier data." />
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-[11px] uppercase tracking-wide font-semibold text-faint border-b border-line">
                    <th className="py-2">How it was read</th><th className="py-2">Pages</th>
                  </tr>
                </thead>
                <tbody>
                  {tiers.map(([tier, count]) => (
                    <tr key={tier} className="border-b border-line last:border-0">
                      {/* §7.2: plain language, no tier numbers — but the raw key
                          stays one hover away so an engineer reading a support
                          ticket over the tenant's shoulder loses nothing. */}
                      <td className="py-2 font-medium">
                        <Tooltip label={`tier_counts key: ${tier}`}>
                          <span>{tierLabel(tier)}</span>
                        </Tooltip>
                      </td>
                      <td className="py-2 tabular-nums">{count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </CardBody>
        </Card>

        <Card>
          <CardHeader
            title="Retrieval facets"
            description="Tags, collection, and metadata scope which documents a query can draw from. Editing them is a metadata-only write — it never re-indexes the document."
          />
          <CardBody className="flex flex-col gap-4">
            <Field label="Tags (comma-separated)">
              <Input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="e.g. tax, 2024, circular" />
            </Field>
            <Field label="Collection">
              <Input value={collection} onChange={(e) => setCollection(e.target.value)} placeholder="e.g. income-tax" />
            </Field>
            <Field label="Metadata (JSON — keys are merged, write null to clear a key)">
              <Textarea mono resizable className="min-h-36" value={metadata} onChange={(e) => setMetadata(e.target.value)} />
            </Field>
            {facetError && <Alert>{facetError}</Alert>}
            <div className="flex items-center gap-2">
              <Button variant="primary" onClick={() => void saveFacets()} disabled={savingFacets}>
                {savingFacets ? <><Spinner /> Saving…</> : "Save"}
              </Button>
              <Button variant="secondary" onClick={() => resetFacets(doc)} disabled={savingFacets}>Reset</Button>
            </div>
          </CardBody>
        </Card>
      </div>
    </div>
  );
}

type ReadTone = "ok" | "warn" | "danger";

// The backend owns these keys and this screen has never read that code, so the
// mapping takes every shape they plausibly arrive in — bare numbers, tier_N, and
// words — with higher numbers meaning more degraded. Anything unrecognised falls
// through to itself rather than being guessed at or dropped: a page missing from
// the count is a lie about how much of the file the agent can actually see.
const TIER_TEXT = new Set(["1", "tier_1", "tier1", "text", "text_layer", "textlayer", "native", "digital"]);
const TIER_OCR = new Set(["2", "tier_2", "tier2", "ocr", "ocr_text", "scanned_ocr", "scan_ocr"]);
const TIER_VISION = new Set(["3", "tier_3", "tier3", "vision", "ai_vision", "vlm", "llm_vision"]);
const TIER_FAILED = new Set(["4", "tier_4", "tier4", "failed", "fail", "failure", "error", "unreadable", "skipped"]);

function tierKind(key: string): "text" | "ocr" | "vision" | "failed" | "other" {
  const k = key.trim().toLowerCase();
  if (TIER_TEXT.has(k)) return "text";
  if (TIER_OCR.has(k)) return "ocr";
  if (TIER_VISION.has(k)) return "vision";
  if (TIER_FAILED.has(k)) return "failed";
  return "other";
}

function tierLabel(key: string): string {
  switch (tierKind(key)) {
    case "text": return "Text layer";
    case "ocr": return "Scanned — read with OCR";
    case "vision": return "Scanned — read with AI vision";
    case "failed": return "Couldn't read";
    default: return key;
  }
}

function plural(n: number, word: string): string { return n === 1 ? word : `${word}s`; }
function verb(n: number): string { return n === 1 ? "was" : "were"; }

// One sentence per thing that actually happened to this file, in the order a
// tenant cares about: how much came through clean, what had to be worked around,
// and what was lost. tier_counts is a count per tier and not a per-page list, so
// this can never name WHICH page failed — "1 page couldn't be read" is true,
// "Page 4 couldn't be read" would be invented.
function describeRead(tiers: [string, number][]): { text: string; tone: ReadTone } | null {
  let textPages = 0, ocrPages = 0, visionPages = 0, failedPages = 0;
  const other: Array<[string, number]> = [];
  for (const [key, raw] of tiers) {
    const n = Number.isFinite(raw) ? Math.max(0, Math.trunc(raw)) : 0;
    if (n === 0) continue;
    switch (tierKind(key)) {
      case "text": textPages += n; break;
      case "ocr": ocrPages += n; break;
      case "vision": visionPages += n; break;
      case "failed": failedPages += n; break;
      default: other.push([key, n]);
    }
  }
  const total = textPages + ocrPages + visionPages + failedPages + other.reduce((s, [, n]) => s + n, 0);
  // Zero pages: the tiers card below already says there is no per-page data.
  // Composing a sentence about nothing would read as a claim we cannot support.
  if (total === 0) return null;

  const parts: string[] = [];
  if (textPages > 0) {
    parts.push(textPages === total
      ? (total === 1
          ? "The one page in this file was read straight from its text layer, so the text your agent sees is exact."
          : `All ${total} pages were read straight from their text layer, so the text your agent sees is exact.`)
      : `${textPages} of ${total} ${plural(total, "page")} ${verb(textPages)} read as text.`);
  }
  if (ocrPages > 0) {
    parts.push(`${ocrPages} ${plural(ocrPages, "page")} ${verb(ocrPages)} a scan, so we read ${ocrPages === 1 ? "it" : "them"} with OCR — check the wording if an answer looks off.`);
  }
  if (visionPages > 0) {
    parts.push(`${visionPages} ${plural(visionPages, "page")} ${verb(visionPages)} a scan we could only read with AI vision, so a few words may not be exact.`);
  }
  // an unrecognised key is reported verbatim; see the mapping note above
  for (const [key, n] of other) {
    parts.push(`${n} ${plural(n, "page")} ${verb(n)} recorded as "${key}".`);
  }
  if (failedPages > 0) {
    parts.push(`${failedPages} ${plural(failedPages, "page")} couldn't be read at all. Try uploading a clearer copy.`);
  }

  // Unrecognised keys count as warn, not ok — the clean-run reassurance is only
  // earned when every page is confirmed to have come from a text layer.
  const tone: ReadTone = failedPages > 0
    ? "danger"
    : (ocrPages > 0 || visionPages > 0 || other.length > 0) ? "warn" : "ok";
  return { text: parts.join(" "), tone };
}
