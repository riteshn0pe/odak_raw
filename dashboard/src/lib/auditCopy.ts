// Turns audit rows into sentences a compliance reader can read without an engineer
// present. Pure and dependency-free on purpose: the audit trail is evidence, so the
// rendering of it must be deterministic and testable by inspection, never coupled to
// React state or to the shape of any particular screen.
import type { AuditRow } from "./types";

export type AuditTone = "neutral" | "ok" | "warn" | "danger" | "brand" | "accent";

// The five buckets the "What changed" filter offers. Ordered as the spec lists them;
// `Security` is deliberately last because it is the catch-all, so a brand-new backend
// event lands somewhere visible rather than disappearing from every filter.
export const AUDIT_GROUPS = [
  "People and roles",
  "Agents",
  "API keys",
  "Sources",
  "Security",
] as const;

export type AuditGroup = (typeof AUDIT_GROUPS)[number];

export function groupOf(action: string): AuditGroup {
  if (action.startsWith("team.")) return "People and roles";
  if (action.startsWith("agent.")) return "Agents";
  if (action.startsWith("apikey.")) return "API keys";
  if (action.startsWith("document.") || action.startsWith("source.")) return "Sources";
  return "Security";
}

// Colour the entry by intent so a scan of the column reads at a glance. Destructive
// first: an audit reader cares far more about a revoke than about a create, so
// `danger` wins any action whose name matches both sides (e.g. "delete_updated").
export function actionTone(action: string): AuditTone {
  if (/remove|revoke|delete|disable|suspend/.test(action)) return "danger";
  if (/create|invite|issue|add|enable|upload/.test(action)) return "ok";
  if (/role|change|update|edit|rotate/.test(action)) return "warn";
  return "neutral";
}

// `detail` is whatever the backend wrote at the time — an old row may predate a field
// that today's writer always sets. Every read goes through these so a missing key can
// never surface as `undefined` or `[object Object]` in front of an auditor.
function str(detail: Record<string, unknown>, ...keys: string[]): string | null {
  for (const k of keys) {
    const v = detail[k];
    if (typeof v === "string" && v.trim()) return v.trim();
    if (typeof v === "number" || typeof v === "boolean") return String(v);
  }
  return null;
}

// Field lists arrive either as a JSON array or as an already-joined string depending on
// which backend path wrote the row; both must read as prose.
function list(detail: Record<string, unknown>, ...keys: string[]): string | null {
  for (const k of keys) {
    const v = detail[k];
    if (Array.isArray(v)) {
      const parts = v.map((x) => (typeof x === "string" ? x : JSON.stringify(x))).filter(Boolean);
      if (parts.length === 1) return parts[0] ?? null;
      if (parts.length === 2) return `${parts[0]} and ${parts[1]}`;
      if (parts.length > 2) return `${parts.slice(0, -1).join(", ")}, and ${parts[parts.length - 1]}`;
    }
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  return null;
}

export function actorName(row: AuditRow): string {
  return row.actor_email?.trim() || "A workspace administrator";
}

// The target of the action, as a human would name it. Falls back through the detail
// payload, then to the target_id column, then to a generic noun — so the sentence stays
// grammatical even for a row whose detail blob was never populated.
function targetName(row: AuditRow, noun: string): string {
  const d = row.detail ?? {};
  const named = str(d, "name", "title", "label", "email", "key_name");
  if (named) return named;
  if (row.target_id) return `${noun} ${row.target_id.slice(0, 8)}`;
  return `an unnamed ${noun}`;
}

function personName(row: AuditRow): string {
  const d = row.detail ?? {};
  return str(d, "email", "target_email", "member_email", "user_email", "invitee") ?? row.target_id ?? "a member";
}

// Sentence-case a raw dotted action so an event this build has never heard of still
// reads as English: `account.suspended` -> `Account suspended`.
export function humaniseAction(action: string): string {
  const words = action.split(".").join(" ").split("_").join(" ").trim();
  if (!words) return "made an unrecorded change";
  return words.charAt(0).toUpperCase() + words.slice(1);
}

// The eight mandated actions, plus the degradation path. Returns a full sentence
// including the trailing period so callers never have to punctuate.
export function auditSentence(row: AuditRow): string {
  const d = row.detail ?? {};
  const actor = actorName(row);

  switch (row.action) {
    case "team.member_invited": {
      const role = str(d, "role", "to", "new_role") ?? "a member";
      return `${actor} invited ${personName(row)} as ${role}.`;
    }
    case "team.member_removed":
      return `${actor} removed ${personName(row)} from the workspace.`;
    case "team.role_changed": {
      const from = str(d, "from", "old_role", "previous_role");
      const to = str(d, "to", "new_role", "role");
      if (from && to) return `${actor} changed ${personName(row)}'s role from ${from} to ${to}.`;
      if (to) return `${actor} changed ${personName(row)}'s role to ${to}.`;
      return `${actor} changed ${personName(row)}'s role.`;
    }
    case "apikey.created":
      return `${actor} created the API key "${targetName(row, "key")}".`;
    case "apikey.revoked":
      return `${actor} revoked the API key "${targetName(row, "key")}".`;
    case "agent.created":
      return `${actor} created the agent "${targetName(row, "agent")}".`;
    case "agent.updated": {
      const fields = list(d, "fields", "changed", "changed_fields", "keys");
      const name = targetName(row, "agent");
      return fields ? `${actor} changed ${fields} on "${name}".` : `${actor} changed settings on "${name}".`;
    }
    case "agent.deleted":
      return `${actor} deleted the agent "${targetName(row, "agent")}".`;
    default:
      // Spec's shape for an unrecognised event: actor, em dash, the action made readable.
      return `${actor} — ${humaniseAction(row.action)}.`;
  }
}

// One short noun phrase naming what the entry acted on, for the Target column and the
// CSV. Kept separate from the sentence so a filtered CSV stays machine-groupable.
export function targetLabel(row: AuditRow): string {
  const type = row.target_type?.trim();
  const id = row.target_id?.trim();
  const named = str(row.detail ?? {}, "name", "title", "email", "key_name");
  if (type && named) return `${type} · ${named}`;
  if (type && id) return `${type} · ${id.slice(0, 8)}`;
  return type || named || id || "—";
}
