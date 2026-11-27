// What happened inside this workspace: who invited or removed a member, changed a role,
// issued or revoked an API key, edited an agent, added a source. Written by the actions
// it records and never from here — there is no UPDATE or DELETE path to the table, which
// is what makes the append-only claim at the top of the screen defensible.
//
// The endpoint accepts only `action` and `limit` (B12), so the three filters this screen
// offers are applied client-side over the most recent 200 rows. That is a deliberate v1
// trade: a compliance reader scanning the last 200 changes is the actual use case, and
// server-side person/date filtering would need endpoint work we have not done.
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../../lib/api";
import { tenantHeaders } from "../../lib/auth";
import type { AuditRow } from "../../lib/types";
import { auditSentence, actionTone, groupOf, targetLabel, AUDIT_GROUPS } from "../../lib/auditCopy";
import { downloadCsv, datedFilename } from "../../lib/csv";
import type { CsvCell } from "../../lib/csv";
import { cx, PageHeader, Card, CardBody, Button, Select, Field, Input, Badge, Alert, Skeleton, EmptyState, Tooltip } from "../../components/ui";
import { Icon } from "../../components/ui/icons";

const LIMIT = 200;

type WhenKey = "7" | "30" | "90" | "all" | "custom";

const WHEN_OPTIONS: { value: WhenKey; label: string }[] = [
  { value: "7", label: "Last 7 days" },
  { value: "30", label: "Last 30 days" },
  { value: "90", label: "Last 90 days" },
  { value: "all", label: "All loaded entries" },
  { value: "custom", label: "Custom range…" },
];

// Badge tone comes back as a literal union from auditCopy; the primitive's Tone type is
// not exported, so the map is typed locally against the same literals.
const GROUP_TONE: Record<string, "neutral" | "ok" | "warn" | "danger" | "brand" | "accent"> = {
  "People and roles": "brand",
  Agents: "accent",
  "API keys": "warn",
  Sources: "ok",
  Security: "neutral",
};

function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

// Day buckets are computed in the viewer's local zone. Grouping by UTC day would
// put "this morning" under yesterday's header for anyone west of Greenwich, which
// reads as a bug to every reader who is not an engineer.
function dayKey(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "unknown";
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function dayHeading(key: string): string {
  if (key === "unknown") return "Undated entries";
  const parts = key.split("-");
  const date = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
  const today = startOfDay(new Date());
  const diff = Math.round((today.getTime() - date.getTime()) / 86_400_000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return date.toLocaleDateString(undefined, { weekday: "long", year: "numeric", month: "long", day: "numeric" });
}

function formatStamp(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleString();
}

export function AuditLog() {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const [person, setPerson] = useState("");
  const [group, setGroup] = useState("");
  const [when, setWhen] = useState<WhenKey>("30");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [open, setOpen] = useState<Set<number>>(new Set());

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const audit = await api.get<AuditRow[]>(`/v1/audit?limit=${LIMIT}`, tenantHeaders());
      setRows(audit);
      setError("");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "could not load audit log");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  // Every actor the rows name — the audit trail is the only source, and a
  // since-removed member's entries stay filterable because their address is
  // still in the row.
  const people = useMemo(() => {
    const seen = new Map<string, string>();
    for (const r of rows) if (r.actor_email && !seen.has(r.actor_email)) seen.set(r.actor_email, r.actor_email);
    return [...seen.entries()].map(([email, label]) => ({ email, label })).sort((a, b) => a.label.localeCompare(b.label));
  }, [rows]);

  const filtered = useMemo(() => {
    let lo = -Infinity;
    let hi = Infinity;
    if (when === "custom") {
      if (from) lo = new Date(`${from}T00:00:00`).getTime();
      if (to) hi = new Date(`${to}T23:59:59.999`).getTime();
    } else if (when !== "all") {
      lo = startOfDay(new Date()).getTime() - (Number(when) - 1) * 86_400_000;
    }
    return rows.filter((r) => {
      if (person && r.actor_email !== person) return false;
      if (group && groupOf(r.action) !== group) return false;
      const t = new Date(r.ts).getTime();
      if (Number.isNaN(t)) return lo === -Infinity && hi === Infinity;
      return t >= lo && t <= hi;
    });
  }, [rows, person, group, when, from, to]);

  // Rows arrive newest-first; grouping with a Map preserves that order for both the day
  // headers and the entries inside each one.
  const days = useMemo(() => {
    const buckets = new Map<string, AuditRow[]>();
    for (const r of filtered) {
      const k = dayKey(r.ts);
      const list = buckets.get(k);
      if (list) list.push(r);
      else buckets.set(k, [r]);
    }
    return [...buckets.entries()];
  }, [filtered]);

  const active = Boolean(person || group || when !== "30" || from || to);

  const reset = useCallback(() => {
    setPerson("");
    setGroup("");
    setWhen("30");
    setFrom("");
    setTo("");
  }, []);

  const toggleRow = useCallback((id: number) => {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  // Export mirrors what is on screen, not what was fetched — a reviewer who filtered to
  // one person expects the file to contain that person. Raw details ride along so the
  // export is still forensically complete.
  const exportCsv = useCallback(() => {
    const header = ["timestamp", "actor email", "action", "description", "target type", "target id", "raw details"] as const;
    const body: CsvCell[][] = filtered.map((r) => [
      new Date(r.ts).toLocaleString(),
      r.actor_email ?? "",
      r.action,
      auditSentence(r),
      r.target_type ?? "",
      r.target_id ?? "",
      r.detail && Object.keys(r.detail).length ? JSON.stringify(r.detail) : "",
    ]);
    downloadCsv(datedFilename("audit-log"), header, body);
  }, [filtered]);

  return (
    <div>
      <PageHeader
        title="Audit log"
        description="A read-only trail of member, role, key, agent, and source changes in this workspace."
        actions={
          <div className="flex items-center gap-2">
            <Button variant="secondary" onClick={exportCsv} disabled={filtered.length === 0}>
              <Icon.Doc size={15} /> Export CSV
            </Button>
          </div>
        }
      />

      {/* The claim a reviewer is actually here to verify. Given a card of its own rather
          than a caption because it is the load-bearing statement on the screen. */}
      <Card className="mb-5">
        <CardBody className="bg-sunken rounded-xl">
          <div className="flex gap-3">
            <span className="text-brand shrink-0 mt-0.5"><Icon.Shield size={18} /></span>
            <p className="text-sm text-muted leading-relaxed">
              <strong className="text-ink font-semibold">This log cannot be edited or deleted, by anyone — including us.</strong>{" "}
              It records who changed what, and when: members and roles, API keys, agents and their instructions,
              and sources. It does not record the content of conversations. Entries are kept for the life of the
              workspace.
            </p>
          </div>
        </CardBody>
      </Card>

      <div className="flex flex-wrap items-end gap-3 mb-4">
        <div className="min-w-[200px]">
          <Field label="Person">
            <Select value={person} onChange={(e) => setPerson(e.target.value)}>
              <option value="">Anyone</option>
              {people.map((p) => <option key={p.email} value={p.email}>{p.label}</option>)}
            </Select>
          </Field>
        </div>
        <div className="min-w-[180px]">
          <Field label="What changed">
            <Select value={group} onChange={(e) => setGroup(e.target.value)}>
              <option value="">Everything</option>
              {AUDIT_GROUPS.map((g) => <option key={g} value={g}>{g}</option>)}
            </Select>
          </Field>
        </div>
        <div className="min-w-[170px]">
          <Field label="When">
            <Select value={when} onChange={(e) => setWhen(e.target.value as WhenKey)}>
              {WHEN_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </Select>
          </Field>
        </div>
        {when === "custom" && (
          <>
            <div className="min-w-[150px]">
              <Field label="From">
                <Input
                  type="date"
                  value={from}
                  onChange={(e) => setFrom(e.target.value)}
                />
              </Field>
            </div>
            <div className="min-w-[150px]">
              <Field label="To">
                <Input
                  type="date"
                  value={to}
                  onChange={(e) => setTo(e.target.value)}
                />
              </Field>
            </div>
          </>
        )}
        {active && (
          <Button variant="ghost" onClick={reset} className="mb-0.5">
            <Icon.X size={14} /> Reset filters
          </Button>
        )}
      </div>

      {error && <div className="mb-4"><Alert>{error}</Alert></div>}

      {!loading && rows.length > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-2 mb-3 text-xs text-muted">
          <span className="tabular-nums">
            {filtered.length === rows.length
              ? `${rows.length} changes`
              : `${filtered.length} of ${rows.length} changes`}
          </span>
          <span className="text-faint">Times shown in your local time zone</span>
        </div>
      )}

      <Card>
        {loading ? (
          <div className="p-5">
            <Skeleton className="h-4 w-24 mb-4" />
            {Array.from({ length: 7 }).map((_, r) => (
              <div key={r} className="flex items-start gap-4 py-3.5 border-b border-line last:border-0">
                <Skeleton className="h-4 w-36 shrink-0" />
                <div className="flex-1">
                  <Skeleton className="h-4 w-full max-w-md mb-2" />
                  <Skeleton className="h-3 w-40" />
                </div>
                <Skeleton className="h-5 w-24 rounded-full shrink-0" />
              </div>
            ))}
          </div>
        ) : rows.length === 0 ? (
          <EmptyState
            icon={<Icon.Shield size={22} />}
            title="No audit entries"
            description="Member, role, key, agent, and source changes will appear here as they happen."
          />
        ) : filtered.length === 0 ? (
          // A filtered-to-nothing view must never look like an empty log — that would
          // read as "nothing was ever recorded", which is the opposite of the truth.
          <EmptyState
            icon={<Icon.Search size={22} />}
            title="No changes match these filters"
            description={`${rows.length} recent changes are loaded, but none of them match. Widen the date range or clear the filters.`}
            action={<Button variant="secondary" onClick={reset}>Reset filters</Button>}
          />
        ) : (
          <div>
            {days.map(([key, entries]) => (
              <section key={key}>
                {/* Sticky because a compliance reader scans by date; losing the date
                    header mid-scroll makes every row below it ambiguous. */}
                <h3 className="sticky top-0 z-10 bg-sunken border-y border-line px-5 py-2 text-[11px] font-semibold uppercase tracking-wide text-muted">
                  {dayHeading(key)}
                  <span className="ml-2 font-normal normal-case tracking-normal text-faint tabular-nums">
                    {entries.length} {entries.length === 1 ? "change" : "changes"}
                  </span>
                </h3>
                <ul>
                  {entries.map((r) => {
                    const g = groupOf(r.action);
                    const hasDetail = Boolean(r.detail && Object.keys(r.detail).length);
                    const isOpen = open.has(r.id);
                    return (
                      <li key={r.id} className="border-b border-line last:border-0">
                        <div className="flex flex-wrap items-start gap-x-4 gap-y-2 px-5 py-3.5 hover:bg-canvas/60 transition-colors">
                          <Tooltip label={new Date(r.ts).toISOString()} className="shrink-0">
                            <span className="text-xs text-muted tabular-nums whitespace-nowrap">
                              {formatStamp(r.ts)}
                            </span>
                          </Tooltip>

                          <div className="flex-1 min-w-[240px]">
                            <p className="text-sm text-ink leading-snug">{auditSentence(r)}</p>
                            <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-faint">
                              <span>{targetLabel(r)}</span>
                              {hasDetail && (
                                <button
                                  type="button"
                                  onClick={() => toggleRow(r.id)}
                                  className="inline-flex items-center gap-1 text-brand hover:underline"
                                  aria-expanded={isOpen}
                                >
                                  <Icon.ChevronDown
                                    size={12}
                                    className={cx("transition-transform", isOpen && "rotate-180")}
                                  />
                                  {isOpen ? "Hide technical details" : "Show technical details"}
                                </button>
                              )}
                            </div>
                          </div>

                          <div className="flex shrink-0 items-center gap-2">
                            <Badge tone={GROUP_TONE[g] ?? "neutral"}>{g}</Badge>
                            {/* The raw action string stays one hover away so an engineer
                                reconciling against server logs loses nothing. */}
                            <Tooltip label={r.action}>
                              <Badge tone={actionTone(r.action)} dot>
                                {r.action.split(".").pop() ?? r.action}
                              </Badge>
                            </Tooltip>
                          </div>
                        </div>

                        {hasDetail && isOpen && (
                          <div className="mx-5 mb-4 rounded-lg bg-sunken border border-line p-3">
                            <pre className="text-[11px] font-mono text-muted whitespace-pre-wrap break-all">
                              {JSON.stringify(r.detail, null, 2)}
                            </pre>
                          </div>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </section>
            ))}
          </div>
        )}
      </Card>

      {/* Named honestly: the endpoint takes only `action` and `limit` (B12), so this
          screen is a window on the newest 200 rows, not the whole trail. */}
      <p className="mt-3 text-xs text-faint">Showing the most recent {LIMIT} changes.</p>
    </div>
  );
}
