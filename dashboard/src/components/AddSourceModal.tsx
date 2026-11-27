// Add source. Five tiles in a Modal size="lg"; each tile opens its own
// SlideOver (§4.3), and the Website panel carries §7's
// SegmentedControl [Crawl site | Sitemap | Individual links] so the "Links"
// tile is just a deep link into that panel's third mode.
//
// Every panel ships degraded rather than blank: a server refusal renders inline
// next to the button that caused it. Nothing here waits on a backend that may
// not be deployed yet.
import { useRef, useState } from "react";
import { api, ApiError, bustGetCache } from "../lib/api";
import { tenantHeaders, tenantSession } from "../lib/auth";
import type {
  CrawlConfigIn,
  DocumentOut,
  QaImportResult,
  QaPairIn,
  SnippetIn,
  SourceCreate,
  SourceOut,
} from "../lib/types";
import { DocumentUpload } from "./DocumentUpload";
import { MarkdownEditor } from "./ui/MarkdownEditor";
import { QaPairEditor, emptyQaPair, qaPairsProblem } from "./QaPairEditor";
import {
  Alert,
  Badge,
  Button,
  Card,
  Divider,
  Field,
  Input,
  Modal,
  SegmentedControl,
  TagInput,
  Textarea,
  ToggleRow,
  cx,
} from "./ui";
import { Combobox } from "./ui/Combobox";
import { Icon } from "./ui/icons";
import { SlideOver } from "./ui/SlideOver";

/* ------------------------------------------------------------------- limits */

// Straight off the Pydantic models: CrawlConfigIn / SourceCreate / SnippetIn.
// Enforced here so the tenant is never handed a 422 the field could have caught.
export const CRAWL_PATH_MAX = 20;
export const SOURCE_TAG_MAX = 20;
export const SOURCE_URL_MAX = 2048;
export const SOURCE_TITLE_MAX = 200;
export const SOURCE_COLLECTION_MAX = 100;
export const LINKS_MAX = 100;
export const SNIPPET_BODY_MAX = 200000;
export const RETRAIN_WEEKLY_HOURS = 168;

export function defaultCrawlConfig(): CrawlConfigIn {
  // Whole-site crawl policy: these five mirror the server-side hardcode in
  // CrawlConfig.from_json (app/domain/crawl.py) — 2000 pages, depth 6,
  // subdomains, PDFs, review gate. There is no UI for them; they ride along
  // so the payload shape never changes.
  return {
    include_paths: [],
    exclude_paths: [],
    max_pages: 2000,
    max_depth: 6,
    include_subdomains: true,
    include_pdf: true,
    review_before_index: true,
  };
}

/** The contractual error codes, said in the tenant's language. */
export function explainSourceFailure(e: unknown, subject: string): string {
  if (!(e instanceof ApiError)) return `${subject} could not be saved. Check your connection and try again.`;
  switch (e.status) {
    case 403:
      return e.message || `${subject} is switched off for this workspace.`;
    case 409:
      return e.message || "This source is already being crawled. Wait for it to finish.";
    case 422:
      return e.message || "Some of those details are not valid.";
    case 429:
      return e.message || "Too many requests. Try again in a moment.";
    case 404:
      // Degraded-first: the endpoint may simply not be deployed yet.
      return "That is not available yet on this workspace.";
    default:
      return e.message || `${subject} could not be saved.`;
  }
}

/* -------------------------------------------------------- crawl config block */

/**
 * §7's "Advanced options" fields. Exported because SourceCard's "Edit settings"
 * edits exactly the same shape (PATCH /v1/sources/{id}) and the two must not
 * drift apart.
 */
export function CrawlConfigFields({
  value,
  onChange,
  disabled,
}: {
  value: CrawlConfigIn;
  onChange: (next: CrawlConfigIn) => void;
  disabled?: boolean;
}) {
  const set = <K extends keyof CrawlConfigIn>(key: K, next: CrawlConfigIn[K]) =>
    onChange({ ...value, [key]: next });

  const path = (entry: string): string | null =>
    entry.startsWith("/") || entry.startsWith("*") ? null : 'Paths start with "/", like /blog.';

  return (
    <div className="flex flex-col gap-3">
      <Field
        label="Include paths"
        hint="Only crawl these. /blog covers /blog/anything; * matches anything. Leave empty for the whole site."
      >
        <TagInput
          value={value.include_paths}
          onChange={(next) => set("include_paths", next)}
          placeholder="/blog"
          max={CRAWL_PATH_MAX}
          validate={path}
          disabled={disabled}
        />
      </Field>
      <Field label="Exclude paths" hint="Never crawl these. Excludes always win.">
        <TagInput
          value={value.exclude_paths}
          onChange={(next) => set("exclude_paths", next)}
          placeholder="/tag"
          tone="warn"
          max={CRAWL_PATH_MAX}
          validate={path}
          disabled={disabled}
        />
      </Field>
      {/* Whole-site policy note: page limit (2000), depth (6), subdomains, PDFs
          and the review gate are fixed server-side in CrawlConfig.from_json —
          there is no UI for them on purpose. */}
    </div>
  );
}

/* ---------------------------------------------------- tags / collection block */

/**
 * Tags + collection, shared by the add panels and SourceCard's "Edit settings"
 * so the two surfaces cannot drift.
 */
export function SourceMetaFields({
  tags,
  onTags,
  collection,
  onCollection,
  collections,
  disabled,
}: {
  tags: string[];
  onTags: (next: string[]) => void;
  collection: string | null;
  onCollection: (next: string | null) => void;
  collections: string[];
  disabled?: boolean;
}) {
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <Field label="Collection" hint="Inherited by every page in this source.">
        <Combobox
          options={collections.map((c) => ({ value: c, label: c }))}
          value={collection}
          onChange={onCollection}
          placeholder="No collection"
          emptyLabel="Type to create one"
          allowCreate
          onCreate={(label) => onCollection(label.slice(0, SOURCE_COLLECTION_MAX))}
          disabled={disabled}
        />
      </Field>
      <Field label="Tags" hint="Used to narrow what an agent may answer from.">
        <TagInput value={tags} onChange={onTags} placeholder="pricing" max={SOURCE_TAG_MAX} disabled={disabled} />
      </Field>
    </div>
  );
}

/* --------------------------------------------------------------- tile chooser */

type PanelKind = "website" | "files" | "snippet" | "qa";
type WebsiteMode = "crawl" | "sitemap" | "links";

interface Tile {
  key: string;
  panel: PanelKind;
  mode?: WebsiteMode;
  icon: typeof Icon.Doc;
  label: string;
  hint: string;
}

const TILES: Tile[] = [
  { key: "files", panel: "files", icon: Icon.Doc, label: "Files", hint: "PDF, DOCX, XLSX, slides" },
  { key: "website", panel: "website", mode: "crawl", icon: Icon.Globe, label: "Website", hint: "Crawl every page" },
  { key: "links", panel: "website", mode: "links", icon: Icon.Link, label: "Links", hint: `Up to ${LINKS_MAX} URLs` },
  { key: "snippet", panel: "snippet", icon: Icon.Book, label: "Text snippet", hint: "Paste it in" },
  { key: "qa", panel: "qa", icon: Icon.Chat, label: "Q&A pairs", hint: "Exact answers" },
];

export function AddSourceModal({
  open,
  onClose,
  agentId,
  onCreated,
  onImported,
  collections = [],
}: {
  open: boolean;
  onClose: () => void;
  /** Origin stamp for the created source. Absent on the shared Knowledge page —
   *  the source is created with no agent_id and every agent can answer from it. */
  agentId?: string;
  onCreated: (source: SourceOut) => void;
  /** The .xlsx path returns a row report rather than a source. */
  onImported?: (result: QaImportResult) => void;
  collections?: string[];
}) {
  const [panel, setPanel] = useState<PanelKind | null>(null);
  const [mode, setMode] = useState<WebsiteMode>("crawl");

  const openTile = (tile: Tile) => {
    if (tile.mode !== undefined) setMode(tile.mode);
    setPanel(tile.panel);
  };

  const closePanel = () => setPanel(null);

  // The chooser hides while a panel is open, so two focus traps are never armed
  // at once. Closing the panel hands the tenant back to the tiles.
  const created = (source: SourceOut) => {
    onCreated(source);
    setPanel(null);
    onClose();
  };

  const shared = { agentId, collections, onCreated: created, onClose: closePanel };

  return (
    <>
      <Modal
        open={open && panel === null}
        onClose={onClose}
        title="Add a source"
        description={agentId
          ? "Everything you add here becomes answerable by this assistant."
          : "Everything you add here becomes answerable by every assistant in this workspace."}
        size="lg"
      >
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          {TILES.map((tile) => {
            const Ico = tile.icon;
            return (
              <Card
                key={tile.key}
                role="button"
                tabIndex={0}
                aria-label={tile.label}
                onClick={() => openTile(tile)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    openTile(tile);
                  }
                }}
                className="cursor-pointer p-4 text-center outline-none transition-colors hover:border-brand hover:bg-brand-soft focus-visible:ring-4 focus-visible:ring-brand-soft"
              >
                <span className="mx-auto grid h-10 w-10 place-items-center rounded-xl border border-line bg-canvas text-faint">
                  <Ico size={20} />
                </span>
                <span className="mt-2 block text-sm font-semibold text-ink">{tile.label}</span>
                <span className="mt-0.5 block text-[11px] text-faint">{tile.hint}</span>
              </Card>
            );
          })}
        </div>
      </Modal>

      <WebsitePanel {...shared} open={open && panel === "website"} mode={mode} onMode={setMode} />
      <FilesPanel open={open && panel === "files"} onClose={closePanel} />
      <SnippetPanel {...shared} open={open && panel === "snippet"} />
      <QaPanel {...shared} open={open && panel === "qa"} onImported={onImported} />
    </>
  );
}

interface PanelShared {
  open: boolean;
  onClose: () => void;
  /** Absent means "no agent_id on the create call" — a shared source. */
  agentId?: string;
  collections: string[];
  onCreated: (source: SourceOut) => void;
}

/** The create URL: agent_id rides as a query param when the source is being
 *  stamped to one agent, and is omitted entirely on the shared Knowledge page —
 *  the API treats absence as "no agent". */
function sourceUrl(path: string, agentId?: string): string {
  return agentId ? `${path}?agent_id=${encodeURIComponent(agentId)}` : path;
}

/* -------------------------------------------------------------- website panel */

const MODE_COPY: Record<WebsiteMode, { title: string; description: string; cta: string }> = {
  crawl: {
    title: "Crawl a website",
    description: "We read every public page we can find from the address you give us.",
    cta: "Start crawl",
  },
  sitemap: {
    title: "Import a sitemap",
    description: "Point us at sitemap.xml and we read exactly the pages it lists.",
    cta: "Start import",
  },
  links: {
    title: "Add individual links",
    description: "One URL per line. We read exactly these pages and nothing else.",
    cta: "Add pages",
  },
};

/**
 * The config the simplified website / sitemap create flow sends. No path
 * filters; the rest mirrors the server-side whole-site hardcode in
 * CrawlConfig.from_json (2000 pages, depth 6, subdomains, PDFs, review gate)
 * so the payload and the policy that actually runs cannot disagree.
 */
const WEBSITE_DEFAULT_CONFIG: CrawlConfigIn = {
  include_paths: [],
  exclude_paths: [],
  max_pages: 2000,
  max_depth: 6,
  include_subdomains: true,
  include_pdf: true,
  review_before_index: true,
};

function WebsitePanel({
  open,
  onClose,
  agentId,
  collections,
  onCreated,
  mode,
  onMode,
}: PanelShared & { mode: WebsiteMode; onMode: (next: WebsiteMode) => void }) {
  const [url, setUrl] = useState("");
  const [linksText, setLinksText] = useState("");
  const [title, setTitle] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [collection, setCollection] = useState<string | null>(null);
  const [weekly, setWeekly] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const copy = MODE_COPY[mode];

  const urls = linksText
    .split(/[\r\n]+/)
    .map((line) => line.trim())
    .filter((line) => line !== "");
  const overLinkLimit = urls.length > LINKS_MAX;

  const problem = (): string | null => {
    if (mode === "links") {
      if (urls.length === 0) return "Add at least one URL.";
      // Hard stop: urls is max_length=100 server-side, so 101 must never be sent.
      if (overLinkLimit) return `That is ${urls.length} URLs. Remove ${urls.length - LINKS_MAX} to get to ${LINKS_MAX}.`;
      const bad = urls.find((u) => u.length > SOURCE_URL_MAX);
      if (bad !== undefined) return "One of those URLs is too long.";
      return null;
    }
    if (url.trim() === "") return mode === "sitemap" ? "Add your sitemap URL." : "Add your website address.";
    if (url.trim().length > SOURCE_URL_MAX) return "That URL is too long.";
    return null;
  };

  // A tenant types acme.com, not https://acme.com. Normalising here is kinder
  // than a 422 that says the URL is invalid.
  const withScheme = (raw: string): string => {
    const value = raw.trim();
    return /^https?:\/\//i.test(value) ? value : `https://${value}`;
  };

  async function submit() {
    const invalid = problem();
    if (invalid !== null) {
      setFailure(invalid);
      return;
    }
    setBusy(true);
    setFailure(null);
    const hours = weekly ? RETRAIN_WEEKLY_HOURS : 0;
    // Website / sitemap: URL, optional name, and the server's defaults for
    // everything else — full site up to the platform page cap, review gate on.
    // Tags, collection and retrain are omitted, so they land empty server side.
    // Links mode keeps its own fields.
    const body: SourceCreate =
      mode === "links"
        ? {
            mode,
            urls: urls.slice(0, LINKS_MAX).map(withScheme),
            title: title.trim() === "" ? null : title.trim(),
            tags,
            collection,
            auto_retrain_hours: hours,
          }
        : {
            mode,
            url: withScheme(url),
            title: title.trim() === "" ? null : title.trim(),
            config: WEBSITE_DEFAULT_CONFIG,
          };
    try {
      // SourceCreate is extra="forbid", so the agent cannot ride in the body.
      const source = await api.post<SourceOut>(
        sourceUrl("/v1/sources/website", agentId),
        body,
        tenantHeaders(),
      );
      onCreated(source);
      setUrl("");
      setLinksText("");
      setTitle("");
      setTags([]);
      setCollection(null);
    } catch (e) {
      setFailure(explainSourceFailure(e, "This website"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <SlideOver
      open={open}
      onClose={onClose}
      title={copy.title}
      description={copy.description}
      width={640}
      footer={
        <div className="flex items-center justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={busy} disabled={busy} onClick={() => void submit()}>
            {copy.cta}
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-4">
        <SegmentedControl
          items={[
            { value: "crawl", label: "Crawl site" },
            { value: "sitemap", label: "Sitemap" },
            { value: "links", label: "Individual links" },
          ]}
          value={mode}
          onChange={(next) => onMode(next as WebsiteMode)}
        />

        {mode === "links" ? (
          <Field label="URLs" hint={`One per line. Up to ${LINKS_MAX}.`}>
            <Textarea
              mono
              resizable
              rows={8}
              value={linksText}
              placeholder={"https://acme.com/pricing\nhttps://acme.com/refunds"}
              onChange={(e) => setLinksText(e.target.value)}
              disabled={busy}
            />
            <div className="mt-1 flex justify-end">
              <span className={cx("text-[11px] tabular-nums", overLinkLimit ? "text-danger" : "text-faint")}>
                {urls.length} of {LINKS_MAX}
              </span>
            </div>
          </Field>
        ) : (
          <Field
            label={mode === "sitemap" ? "Sitemap URL" : "Website URL"}
            hint={
              mode === "sitemap"
                ? "Usually acme.com/sitemap.xml."
                : "We'll find every public page from here."
            }
          >
            <Input
              type="url"
              value={url}
              maxLength={SOURCE_URL_MAX}
              placeholder={mode === "sitemap" ? "https://acme.com/sitemap.xml" : "https://acme.com"}
              onChange={(e) => setUrl(e.target.value)}
              disabled={busy}
            />
          </Field>
        )}

        <Field label="Name" hint="Optional. Defaults to the site's address.">
          <Input
            value={title}
            maxLength={SOURCE_TITLE_MAX}
            placeholder="Acme help centre"
            onChange={(e) => setTitle(e.target.value)}
            disabled={busy}
          />
        </Field>

        {/* Website / sitemap stay dead simple — just the URL. The review gate
            (config.review_before_index) is on by default, so the tenant prunes
            links there rather than through crawl options. Links mode keeps its
            tagging fields. */}
        {mode === "links" && (
          <>
            <SourceMetaFields
              tags={tags}
              onTags={setTags}
              collection={collection}
              onCollection={setCollection}
              collections={collections}
              disabled={busy}
            />

            <ToggleRow
              label="Retrain weekly"
              hint="We re-read the site every week and only re-index what changed."
              checked={weekly}
              disabled={busy}
              onChange={setWeekly}
            />
          </>
        )}

        {failure !== null && <Alert tone="danger">{failure}</Alert>}
      </div>
    </SlideOver>
  );
}

/* ---------------------------------------------------------------- files panel */

function FilesPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [added, setAdded] = useState<DocumentOut[]>([]);

  return (
    <SlideOver
      open={open}
      onClose={onClose}
      title="Add files"
      description="PDF, DOCX, XLSX, PPTX, HTML and plain text."
      width={640}
      footer={
        <div className="flex items-center justify-between gap-2">
          <span className="text-[12px] text-muted tabular-nums">
            {added.length > 0 ? `${added.length} added` : "Nothing added yet"}
          </span>
          <Button variant="primary" onClick={onClose}>
            Done
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-3">
        {/* The existing upload path, unchanged — drag-drop, per-file progress and
            the gate's rejection reasons are already solved in DocumentUpload. */}
        <DocumentUpload onUploaded={(doc, duplicate) => !duplicate && setAdded((prev) => [...prev, doc])} />
        {added.length > 0 && (
          <ul className="flex flex-col gap-1">
            {added.map((doc) => (
              <li key={doc.id} className="flex items-center gap-2 text-[13px] text-ink">
                <Icon.Check size={14} className="shrink-0 text-ok" />
                <span className="truncate">{doc.filename}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </SlideOver>
  );
}

/* -------------------------------------------------------------- snippet panel */

function SnippetPanel({ open, onClose, agentId, collections, onCreated }: PanelShared) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [collection, setCollection] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  async function submit() {
    if (title.trim() === "") {
      setFailure("Give this snippet a name so you can find it later.");
      return;
    }
    if (body.trim() === "") {
      setFailure("Add the text you want your assistant to answer from.");
      return;
    }
    if (body.length > SNIPPET_BODY_MAX) {
      setFailure(`That is ${body.length.toLocaleString()} characters. The limit is ${SNIPPET_BODY_MAX.toLocaleString()}.`);
      return;
    }
    setBusy(true);
    setFailure(null);
    const payload: SnippetIn = { title: title.trim(), body, tags, collection };
    try {
      const source = await api.post<SourceOut>(
        sourceUrl("/v1/sources/snippets", agentId),
        payload,
        tenantHeaders(),
      );
      onCreated(source);
      setTitle("");
      setBody("");
      setTags([]);
      setCollection(null);
    } catch (e) {
      setFailure(explainSourceFailure(e, "This snippet"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <SlideOver
      open={open}
      onClose={onClose}
      title="Add a text snippet"
      description="For the things that live in someone's head rather than in a document."
      width={720}
      footer={
        <div className="flex items-center justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={busy} disabled={busy} onClick={() => void submit()}>
            Add snippet
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label="Name" hint="Shown in your source list.">
          <Input
            value={title}
            maxLength={SOURCE_TITLE_MAX}
            placeholder="Opening hours and holidays"
            onChange={(e) => setTitle(e.target.value)}
            disabled={busy}
          />
        </Field>
        <Field label="Content" hint="Markdown. Headings and lists are kept, so structure survives into the answer.">
          <MarkdownEditor
            value={body}
            onChange={setBody}
            placeholder={"## Opening hours\n\nMonday to Friday, 9am to 6pm."}
            maxLength={SNIPPET_BODY_MAX}
            minRows={12}
            disabled={busy}
          />
        </Field>
        <SourceMetaFields
          tags={tags}
          onTags={setTags}
          collection={collection}
          onCollection={setCollection}
          collections={collections}
          disabled={busy}
        />
        {failure !== null && <Alert tone="danger">{failure}</Alert>}
      </div>
    </SlideOver>
  );
}

/* ------------------------------------------------------------------ Q&A panel */

// QA_IMPORT_MAX_BYTES on the server. Checked here so a 2 MB+ workbook is refused
// before it is uploaded rather than after.
const QA_IMPORT_MAX_BYTES = 2 * 1024 * 1024;

function QaPanel({
  open,
  onClose,
  agentId,
  onCreated,
  onImported,
}: PanelShared & { onImported?: (result: QaImportResult) => void }) {
  const [title, setTitle] = useState("");
  const [pairs, setPairs] = useState<QaPairIn[]>(() => [emptyQaPair()]);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [importing, setImporting] = useState(false);
  const [imported, setImported] = useState<QaImportResult | null>(null);
  const [drag, setDrag] = useState(false);
  const fileInput = useRef<HTMLInputElement | null>(null);

  async function submit() {
    if (title.trim() === "") {
      setFailure("Give this set a name, like \"Top 20 questions\".");
      return;
    }
    const invalid = qaPairsProblem(pairs);
    if (invalid !== null) {
      setFailure(invalid);
      return;
    }
    setBusy(true);
    setFailure(null);
    try {
      const source = await api.post<SourceOut>(
        sourceUrl("/v1/sources/qa", agentId),
        { title: title.trim(), pairs },
        tenantHeaders(),
      );
      onCreated(source);
      setTitle("");
      setPairs([emptyQaPair()]);
    } catch (e) {
      setFailure(explainSourceFailure(e, "This Q&A set"));
    } finally {
      setBusy(false);
    }
  }

  async function importFile(file: File) {
    if (!/\.xlsx$/i.test(file.name)) {
      setFailure("That needs to be an .xlsx workbook.");
      return;
    }
    if (file.size > QA_IMPORT_MAX_BYTES) {
      setFailure("That workbook is over 2 MB. Split it and import the parts.");
      return;
    }
    setImporting(true);
    setFailure(null);
    try {
      const result = await importQaWorkbook(file);
      setImported(result);
      onImported?.(result);
    } catch (e) {
      setFailure(explainSourceFailure(e, "That workbook"));
    } finally {
      setImporting(false);
    }
  }

  return (
    <SlideOver
      open={open}
      onClose={onClose}
      title="Add Q&A pairs"
      description="For the questions you already know the exact answer to."
      width={760}
      footer={
        <div className="flex items-center justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={busy} disabled={busy} onClick={() => void submit()}>
            Add Q&amp;A set
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label="Name" hint="Shown in your source list.">
          <Input
            value={title}
            maxLength={SOURCE_TITLE_MAX}
            placeholder="Top 20 questions"
            onChange={(e) => setTitle(e.target.value)}
            disabled={busy}
          />
        </Field>

        <QaPairEditor value={pairs} onChange={setPairs} disabled={busy} />

        <Divider label="or" />

        {/* Same drop-zone idiom as DocumentUpload, narrowed to one workbook. */}
        <div
          role="button"
          tabIndex={0}
          aria-label="Import a Q&A workbook"
          aria-disabled={importing}
          className={cx(
            "flex flex-col items-center gap-2 rounded-xl border-2 border-dashed px-6 py-8 text-center outline-none transition-all",
            importing ? "cursor-not-allowed opacity-60" : "cursor-pointer",
            "focus-visible:ring-4 focus-visible:ring-brand-soft focus-visible:border-brand",
            drag
              ? "border-brand bg-brand-soft text-brand"
              : "border-line-strong bg-canvas text-muted hover:border-brand hover:bg-brand-soft",
          )}
          onDragOver={(e) => {
            if (importing) return;
            e.preventDefault();
            setDrag(true);
          }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDrag(false);
            if (importing) return;
            const file = e.dataTransfer.files[0];
            if (file !== undefined) void importFile(file);
          }}
          onClick={() => {
            if (!importing) fileInput.current?.click();
          }}
          onKeyDown={(e) => {
            if (importing) return;
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              fileInput.current?.click();
            }
          }}
        >
          <Icon.Upload size={20} />
          <span className="text-sm font-semibold text-ink">
            {importing ? "Reading your workbook…" : "Import an .xlsx instead"}
          </span>
          <span className="text-xs text-faint">Columns: Title, Question, Answer. Up to 100 rows.</span>
          <input
            ref={fileInput}
            type="file"
            accept=".xlsx"
            hidden
            disabled={importing}
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file !== undefined) void importFile(file);
              e.target.value = "";
            }}
          />
        </div>

        {imported !== null && (
          <div className="rounded-lg border border-line bg-canvas p-3">
            <div className="flex items-center gap-2">
              <Badge tone={imported.created > 0 ? "ok" : "warn"} dot>
                {imported.created} created
              </Badge>
              {imported.errors.length > 0 && (
                <span className="text-[12px] text-muted tabular-nums">{imported.errors.length} rows skipped</span>
              )}
            </div>
            {imported.errors.length > 0 && (
              <ul className="mt-2 flex flex-col gap-0.5">
                {imported.errors.slice(0, 10).map((row, i) => (
                  <li key={i} className="text-[12px] text-muted">
                    {importRowLabel(row, i)}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {failure !== null && <Alert tone="danger">{failure}</Alert>}
      </div>
    </SlideOver>
  );
}

function importRowLabel(row: Record<string, unknown>, index: number): string {
  const line = typeof row.row === "number" ? row.row : index + 1;
  const detail =
    typeof row.message === "string"
      ? row.message
      : typeof row.error === "string"
        ? row.error
        : typeof row.status === "string"
          ? row.status
          : "could not be read";
  return `Row ${line}: ${detail}`;
}

/**
 * api.upload cannot thread the tenant header or a second form field (api.ts:80),
 * so the multipart POST is re-issued here exactly the way DocumentUpload does it.
 */
async function importQaWorkbook(file: File, sourceId?: string): Promise<QaImportResult> {
  const form = new FormData();
  form.append("file", file);
  if (sourceId !== undefined) form.append("source_id", sourceId);
  const token = tenantSession()?.token;
  const res = await fetch("/v1/sources/qa/import", {
    method: "POST",
    headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...tenantHeaders() },
    body: form,
  });
  const text = await res.text();
  const payload: unknown = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const shape = payload as { error?: { code?: string; message?: string } } | null;
    throw new ApiError(
      shape?.error?.code ?? "unknown",
      shape?.error?.message ?? "That workbook could not be imported.",
      res.status,
    );
  }
  // raw fetch above bypasses the client's mutation hooks — bust manually so a
  // revisited list screen does not serve the pre-import snapshot
  bustGetCache();
  return payload as QaImportResult;
}


