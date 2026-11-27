// Approvals: everything waiting on the owner's decision, soonest deadline first.
//
// This screen is the panel destination of the same fan-out that reaches Telegram,
// WhatsApp and Slack — the notification's buttons deep-link here rather than at a
// decision endpoint, because a GET that spends money is a URL a mail scanner or a
// link-preview fetcher will eventually press on the owner's behalf.
//
// An expired batch is *held*, not sent. That is the single most important thing
// this screen has to communicate, so the deadline is rendered as time remaining
// and the copy says what silence means.
import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { ApiError, growthApi } from "../../../lib/api";
import { tenantHeaders } from "../../../lib/auth";
import type { GrowthApproval } from "../../../lib/types";
import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  Modal,
  PageHeader,
  Skeleton,
  Spinner,
} from "../../../components/ui";
import { Icon } from "../../../components/ui/icons";
import { JsonEditor } from "../../../components/ui/JsonEditor";
import { useToast } from "../../../components/ui/Toast";

const FEATURE_LABEL: Record<string, string> = {
  outbound_batch: "Cold outreach",
  campaign: "Campaign",
  review_ask: "Review request",
};

const LEVEL_LABEL: Record<number, string> = {
  0: "Automatic",
  1: "Per batch",
  2: "Every message",
};

/** How long is left, in the words a person would use. */
function timeLeft(iso: string | null): { text: string; urgent: boolean } {
  if (!iso) return { text: "no deadline", urgent: false };
  const ms = new Date(iso).getTime() - Date.now();
  if (Number.isNaN(ms)) return { text: "no deadline", urgent: false };
  if (ms <= 0) return { text: "expired", urgent: true };
  const hours = Math.floor(ms / 3_600_000);
  if (hours < 1) return { text: `${Math.max(1, Math.round(ms / 60_000))} min left`, urgent: true };
  if (hours < 24) return { text: `${hours}h left`, urgent: hours <= 6 };
  return { text: `${Math.floor(hours / 24)}d left`, urgent: false };
}

/** The preview lines, however the agent shaped them. */
function itemLines(approval: GrowthApproval): string[] {
  const items = approval.payload?.items;
  if (!Array.isArray(items)) return [];
  return items.map((item) =>
    typeof item === "string"
      ? item
      : String((item as Record<string, unknown>)?.label ?? JSON.stringify(item)),
  );
}

export function GrowthApprovals() {
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const focused = params.get("id");

  const [rows, setRows] = useState<GrowthApproval[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  // The edit-before-approve path: the owner rewrites the payload the agent
  // proposed, and that edited payload — not the original — is what is released.
  const [editing, setEditing] = useState<GrowthApproval | null>(null);
  const [draft, setDraft] = useState("");
  const [draftValue, setDraftValue] = useState<Record<string, unknown> | null>(null);

  const load = useCallback(() => {
    const controller = new AbortController();
    growthApi
      .approvals(tenantHeaders(), controller.signal)
      .then(setRows)
      .catch((e) => {
        if (controller.signal.aborted) return;
        setError(e instanceof ApiError ? e.message : "could not load your approvals");
      });
    return () => controller.abort();
  }, []);

  useEffect(load, [load]);

  // Poll while the tab is visible. The row this screen shows can be decided from
  // a phone in the middle of reading it, and a stale Approve button that 422s is
  // a worse experience than a list that refreshes itself.
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, 30_000);
    return () => window.clearInterval(timer);
  }, [load]);

  // A deep link from a notification names one row; float it to the top so the
  // owner lands on the thing they were pinged about rather than hunting for it.
  const ordered = useMemo(() => {
    if (!rows) return null;
    if (!focused) return rows;
    return [...rows].sort((a, b) => (a.id === focused ? -1 : b.id === focused ? 1 : 0));
  }, [rows, focused]);

  async function decide(
    row: GrowthApproval,
    decision: "approved" | "rejected" | "edited",
    payload?: Record<string, unknown>,
  ) {
    setBusy(row.id);
    setError("");
    try {
      await growthApi.decide(row.id, { decision, ...(payload ? { payload } : {}) }, tenantHeaders());
      setRows((current) => (current ?? []).filter((r) => r.id !== row.id));
      setEditing(null);
      if (focused === row.id) {
        params.delete("id");
        setParams(params, { replace: true });
      }
      toast.success(
        decision === "rejected"
          ? "Rejected. Nothing sent."
          : decision === "edited"
            ? "Approved with your edits — sending those."
            : "Approved — sending now.",
      );
    } catch (e) {
      // 422 here almost always means somebody decided it elsewhere, so reload
      // rather than leaving a button that will fail again
      setError(e instanceof ApiError ? e.message : "could not record your decision");
      load();
    } finally {
      setBusy(null);
    }
  }

  if (error && !rows) {
    return (
      <div className="max-w-3xl">
        <PageHeader title="Approvals" />
        <Alert>{error}</Alert>
      </div>
    );
  }

  if (!ordered) {
    return (
      <div className="max-w-3xl">
        <PageHeader title="Approvals" />
        <div className="flex flex-col gap-4">
          {Array.from({ length: 2 }).map((_, i) => (
            <Card key={i}>
              <CardBody className="flex flex-col gap-3">
                <Skeleton className="h-4 w-48" />
                <Skeleton className="h-3 w-full" />
                <Skeleton className="h-3 w-2/3" />
                <Skeleton className="h-9 w-40" />
              </CardBody>
            </Card>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-3xl">
      <PageHeader
        title="Approvals"
        description="Nothing here has been sent. A request nobody decides on expires and is held."
      />
      {error && (
        <div className="mb-4">
          <Alert>{error}</Alert>
        </div>
      )}

      {ordered.length === 0 ? (
        <Card>
          <EmptyState
            icon={<Icon.Check size={22} />}
            title="Nothing waiting on you"
            description="When an agent wants to spend money or contact someone new, it will ask here — and on whichever channels you connected."
            tone="ok"
          />
        </Card>
      ) : (
        <div className="flex flex-col gap-4">
          {ordered.map((row) => {
            const left = timeLeft(row.expires_at);
            const lines = itemLines(row);
            const summary = String(row.payload?.summary ?? "");
            return (
              <Card
                key={row.id}
                className={row.id === focused ? "ring-2 ring-brand" : undefined}
              >
                <CardHeader
                  title={FEATURE_LABEL[row.feature] ?? row.feature}
                  description={summary || undefined}
                  actions={
                    <div className="flex items-center gap-2">
                      <Badge tone="neutral">{LEVEL_LABEL[row.level] ?? `level ${row.level}`}</Badge>
                      <Badge tone={left.urgent ? "danger" : "warn"} dot>
                        {left.text}
                      </Badge>
                    </div>
                  }
                />
                <CardBody className="flex flex-col gap-4">
                  {lines.length > 0 && (
                    <ul className="flex flex-col gap-1.5 text-[13px] text-ink">
                      {lines.slice(0, 8).map((line, i) => (
                        <li key={i} className="flex gap-2">
                          <span className="text-faint">•</span>
                          <span className="min-w-0 break-words">{line}</span>
                        </li>
                      ))}
                      {lines.length > 8 && (
                        <li className="text-muted">…and {lines.length - 8} more</li>
                      )}
                    </ul>
                  )}
                  <div className="flex gap-2">
                    <Button
                      variant="primary"
                      onClick={() => void decide(row, "approved")}
                      disabled={busy !== null}
                    >
                      {busy === row.id ? (
                        <>
                          <Spinner /> Working…
                        </>
                      ) : (
                        <>
                          <Icon.Check size={16} /> Approve
                        </>
                      )}
                    </Button>
                    <Button
                      onClick={() => {
                        setEditing(row);
                        setDraft(JSON.stringify(row.payload ?? {}, null, 2));
                        setDraftValue(row.payload ?? {});
                      }}
                      disabled={busy !== null}
                    >
                      Edit
                    </Button>
                    <Button
                      variant="danger"
                      onClick={() => void decide(row, "rejected")}
                      disabled={busy !== null}
                    >
                      Reject
                    </Button>
                  </div>
                </CardBody>
              </Card>
            );
          })}
        </div>
      )}

      {/* An edit is an approval, not a third outcome: the server releases the
          batch from the payload sent here instead of the one the agent wrote. */}
      <Modal
        open={editing !== null}
        onClose={() => {
          if (busy === null) setEditing(null);
        }}
        title="Edit before approving"
        description="What goes out is exactly what you leave here. Approving without changes is the same as pressing Approve."
        size="lg"
        footer={
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setEditing(null)} disabled={busy !== null}>
              Cancel
            </Button>
            <Button
              variant="primary"
              disabled={busy !== null || draftValue === null}
              onClick={() => {
                if (editing && draftValue) void decide(editing, "edited", draftValue);
              }}
            >
              {busy !== null ? (
                <>
                  <Spinner /> Working…
                </>
              ) : (
                "Approve these edits"
              )}
            </Button>
          </div>
        }
      >
        <JsonEditor
          value={draft}
          onChange={setDraft}
          onValidChange={(parsed) =>
            setDraftValue(
              parsed !== undefined && typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
                ? (parsed as Record<string, unknown>)
                : null,
            )
          }
          minHeight={260}
        />
      </Modal>
    </div>
  );
}
