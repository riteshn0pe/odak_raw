// Document and integration status pill. A failure carries its reason on the
// title attribute, because a bare "failed" just sends the tenant to support.
import { Badge, cx } from "./ui";
import { crawlStateLabel } from "./LinkTicker";

type Tone = "neutral" | "ok" | "warn" | "danger" | "brand";
const TONE: Record<string, Tone> = {
  ready: "ok", active: "ok", processing: "brand", queued: "neutral",
  failed: "danger", error: "danger", disconnected: "neutral", deleted: "neutral",
  awaiting_review: "warn",
};

// Human labels for the shared status vocabulary — document file states, job
// steps, channel states, tenant states. Unknown values fall
// through to the raw string. Crawl vocabulary ("Trained", "Reading pages") is
// opt-in via the `crawl` prop, because it only applies to sources' crawl_state.
const LABEL: Record<string, string> = {
  awaiting_review: "Waiting for review",
};

// while a document is still indexing the pill breathes, so a settling list reads
// as "work in progress" at a glance rather than a static label
const PULSING = new Set(["processing", "queued"]);

export function StatusBadge({ status, reason, crawl }: { status: string; reason?: string | null; crawl?: boolean }) {
  const tone = TONE[status] ?? "neutral";
  const label = crawl ? crawlStateLabel(status) : (LABEL[status] ?? status);
  return (
    // The pulse is decoration on top of a label that already says "processing",
    // so under prefers-reduced-motion it simply stops — no information is lost
    // and nothing has to be substituted (§6.5).
    <span title={reason ?? undefined} className={cx("inline-flex", PULSING.has(status) && "animate-pulse motion-reduce:animate-none")}>
      <Badge tone={tone} dot>{label}</Badge>
    </span>
  );
}
