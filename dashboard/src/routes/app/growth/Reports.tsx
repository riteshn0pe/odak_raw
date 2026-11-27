// Reports: every weekly/monthly/on-demand report, newest first.
//
// A queued row is a promise, not an absence — the numbers JSON rides on the
// row itself, so the screen renders the fallback (summary tiles from the
// numbers) while the PDF is still rendering, and the download appears when
// the job finishes. "Run now" enqueues an on-demand run; pressing it twice
// regenerates the same row rather than duplicating it.
import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ApiError, growthApi } from "../../../lib/api";
import { tenantHeaders } from "../../../lib/auth";
import type { GrowthReport } from "../../../lib/types";
import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  PageHeader,
  Skeleton,
} from "../../../components/ui";
import { Icon } from "../../../components/ui/icons";
import { useToast } from "../../../components/ui/Toast";

const PERIOD_LABEL: Record<string, string> = {
  weekly: "Weekly",
  monthly: "Monthly",
  daily: "Daily",
  on_demand: "On demand",
};

const STATUS_TONE: Record<string, "ok" | "warn" | "neutral"> = {
  ready: "ok",
  queued: "warn",
  failed: "neutral",
};

function tiles(report: GrowthReport): { label: string; value: string }[] {
  const numbers = report.numbers ?? {};
  const funnel = (numbers.funnel ?? {}) as Record<string, unknown>;
  const outbound = (funnel.outbound ?? []) as Array<Record<string, unknown>>;
  // Same definition the PDF tile and the two-line summary use: a mail still
  // queued, or one that bounced, was not sent. Counting every status here
  // would make this screen disagree with the report it is describing.
  const mailed = outbound
    .filter((i) => ["sent", "delivered", "opened"].includes(String(i.status)))
    .reduce((n, i) => n + Number(i.count ?? 0), 0);
  const deltas = (numbers.deltas ?? {}) as Record<string, number>;
  const delta = (key: string) => {
    const d = deltas[key];
    if (typeof d !== "number") return "";
    return ` (${d >= 0 ? "+" : ""}${d} vs last)`;
  };
  return [
    { label: "New leads", value: `${Number(funnel.leads_created ?? 0)}${delta("leads")}` },
    { label: "Mailed", value: `${mailed}${delta("mailed")}` },
    { label: "Replies", value: `${Number(funnel.inbound ?? 0)}${delta("replies")}` },
  ];
}

function summaryLines(report: GrowthReport): string[] {
  return report.summary.split("\n").map((l) => l.trim()).filter(Boolean);
}

export function GrowthReports() {
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const focused = params.get("id");

  const [rows, setRows] = useState<GrowthReport[] | null>(null);
  const [deepRow, setDeepRow] = useState<GrowthReport | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    const controller = new AbortController();
    growthApi
      .reports({ limit: 50 }, tenantHeaders(), controller.signal)
      .then(setRows)
      .catch((e) => {
        if ((e as Error)?.name !== "AbortError") {
          setError(e instanceof ApiError ? e.message : "Could not load reports.");
        }
      });
    return () => controller.abort();
  }, []);

  useEffect(load, [load]);

  // A link out of a report notification names one row, and it may be older than
  // the page we load. Fetch that one rather than showing an empty screen.
  useEffect(() => {
    if (!focused || rows === null) return undefined;
    if (rows.some((r) => r.id === focused)) {
      setDeepRow(null);
      return undefined;
    }
    const controller = new AbortController();
    growthApi
      .report(focused, tenantHeaders(), controller.signal)
      .then(setDeepRow)
      .catch((e) => {
        if ((e as Error)?.name !== "AbortError") {
          setError(e instanceof ApiError ? e.message : "Could not open that report.");
        }
      });
    return () => controller.abort();
  }, [focused, rows]);

  const runNow = useCallback(() => {
    setBusy(true);
    growthApi
      .runReport(tenantHeaders())
      .then((res) => {
        if (res.status === "ready") {
          toast.show("That report is already ready — opening it.");
          setParams({ id: String(res.report_id) });
        } else {
          toast.show("Report queued — it lands here and on WhatsApp + Telegram.");
        }
        load();
      })
      .catch((e) => {
        toast.error(e instanceof ApiError ? e.message : "Could not queue the report.");
      })
      .finally(() => setBusy(false));
  }, [load, setParams, toast]);

  const visible = rows === null ? null : deepRow ? [deepRow, ...rows] : rows;
  const open = visible?.find((r) => r.id === focused) ?? null;

  return (
    <div>
      <PageHeader
        title="Reports"
        description="Monday mornings and the 1st, plus any time you ask."
        actions={
          <Button onClick={runNow} disabled={busy}>
            <Icon.Spark size={15} /> Run now
          </Button>
        }
      />
      {error && (
        <div className="mb-4">
          <Alert>{error}</Alert>
        </div>
      )}
      {visible === null ? (
        <div>
          <Skeleton className="h-4 w-48" />
          <Skeleton className="h-3 w-full" />
        </div>
      ) : visible.length === 0 ? (
        <EmptyState
          title="No reports yet"
          description="Your first weekly report arrives Monday after 9am. Press Run now for one today."
          action={<Button onClick={runNow} disabled={busy}>Run now</Button>}
        />
      ) : (
        <div style={{ display: "grid", gap: 12 }}>
          {visible.map((report) => (
            <Card key={report.id}>
              <CardHeader
                title={`${PERIOD_LABEL[report.period] ?? report.period} · ${report.period_start ?? ""}`}
                actions={<Badge tone={STATUS_TONE[report.status] ?? "neutral"}>{report.status}</Badge>}
              />
              <CardBody>
                {summaryLines(report).map((line, i) => (
                  <p key={i} style={{ margin: "4px 0" }}>{line}</p>
                ))}
                {report.status === "ready" && (
                  <div style={{ display: "flex", gap: 16, marginTop: 8 }}>
                    {tiles(report).map((t) => (
                      <span key={t.label} style={{ fontSize: 13 }}>
                        <strong>{t.value}</strong>{" "}{t.label.toLowerCase()}
                      </span>
                    ))}
                  </div>
                )}
                {report.status === "queued" && (
                  <p style={{ color: "var(--muted)", fontSize: 13 }}>
                    Still rendering — the numbers appear here when it lands.
                  </p>
                )}
                <div style={{ marginTop: 8 }}>
                  <Button
                    variant="ghost"
                    onClick={() => setParams(open?.id === report.id ? {} : { id: report.id })}
                  >
                    {open?.id === report.id ? "Hide details" : "Details"}
                  </Button>
                </div>
                {open?.id === report.id && <ReportDetail report={open} />}
              </CardBody>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}

function ReportDetail({ report }: { report: GrowthReport }) {
  const toast = useToast();
  const [downloading, setDownloading] = useState(false);
  const numbers = report.numbers ?? {};
  const funnel = (numbers.funnel ?? {}) as Record<string, unknown>;
  const bySource = (funnel.by_source ?? []) as Array<Record<string, unknown>>;
  const hot = (numbers.hot_leads ?? []) as Array<Record<string, unknown>>;
  const usage = (numbers.usage ?? {}) as Record<string, unknown>;
  const byFeature = (usage.by_feature ?? []) as Array<Record<string, unknown>>;
  return (
    <div style={{ marginTop: 8, fontSize: 13 }}>
      {bySource.length > 0 && (
        <p>Sources: {bySource.map((s) => `${String(s.source)} ${Number(s.count)}`).join(" · ")}</p>
      )}
      {hot.length > 0 && (
        <p>Hot leads: {hot.map((h) => String(h.company || h.email || "?")).join(", ")}</p>
      )}
      {byFeature.length > 0 && (
        <p>
          Cost:{" "}
          {byFeature.map((f) => `${String(f.feature)} ₹${(Number(f.micros) / 1_000_000).toFixed(2)}`).join(" · ")}
        </p>
      )}
      {report.pdf_path ? (
        <div style={{ marginTop: 8 }}>
          <Button
            variant="ghost"
            disabled={downloading}
            onClick={() => {
              setDownloading(true);
              growthApi
                .reportDownload(report.id, tenantHeaders())
                .then((res) => {
                  // The signed link is short-lived, so it is opened straight
                  // away rather than parked in component state.
                  window.open(res.url, "_blank", "noopener,noreferrer");
                })
                .catch((e) => {
                  toast.error(
                    e instanceof ApiError ? e.message : "Could not open the PDF.",
                  );
                })
                .finally(() => setDownloading(false));
            }}
          >
            <Icon.Download size={15} /> {downloading ? "Opening…" : "Download PDF"}
          </Button>
        </div>
      ) : (
        report.status === "queued" && (
          <p style={{ color: "var(--muted)" }}>PDF still rendering.</p>
        )
      )}
    </div>
  );
}
