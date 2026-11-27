// The handoff block (§7.3), mounted inside Instructions rather than given a route
// of its own — part2 §8.1 Wave 8 lists it as a panel, and it belongs directly
// under "Safety and boundaries" because the fallback message it sits beside is
// what a person sees when the agent gives up.
//
// Two halves, and they still degrade independently:
//
// 1. The numbers. `GET /v1/escalations/stats` reports per-day, per-trigger counts
//    over the whole workspace. `summariseEscalations` turns those into the count
//    and the rate this band shows, with the answered-question denominator read
//    from `GET /v1/analytics/performance` over the same window. If either read
//    fails the band says so in words with no number attached — it never borrows
//    the fallback-rate proxy for these labels, because shipping a wrong number
//    under a right name is worse than shipping none.
//
// 2. The settings. These are the real `config.escalation` block, read from the
//    agent row and written through `PATCH /v1/agents/{id}` like every other
//    section. Nothing is parked in the browser any more: the block exists on the
//    server, so a saved choice is a saved choice.
//
// The server's config merge is shallow at the top level, so every save sends the
// COMPLETE escalation block — the stored copy with the form's changes on top.
// Sending a partial block would reset the fields this screen does not show,
// including the two the email card on Channels owns.
//
// The word "deflection" appears in no string on this screen.
import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, api, escalationsApi, summariseEscalations } from "../../../lib/api";
import { AgentVersionConflict } from "../../../lib/agentConfig";
import { tenantHeaders } from "../../../lib/auth";
import { percent } from "../../../lib/format";
import type {
  EscalationConfig, EscalationStats, EscalationStatsOut, EscalationTrigger, Performance,
} from "../../../lib/types";
import {
  Alert, Badge, Button, Card, CardBody, CardHeader, Field, Select, Skeleton, Slider,
  StatCard, TagInput, Textarea, ToggleRow, Tooltip,
} from "../../../components/ui";
import { Icon } from "../../../components/ui/icons";
import { BarChart } from "../../../components/Chart";
import { useToast } from "../../../components/ui/Toast";
import { useAgent } from "./AgentLayout";

/** Settled in `EscalationTrigger`'s doc comment. These are the only strings shown. */
const TRIGGER_LABEL: Record<EscalationTrigger, string> = {
  low_confidence: "Wasn't sure enough",
  ungrounded: "Answer wasn't backed by your sources",
  user_requested: "Customer asked for a person",
  negative_feedback: "Thumbs down",
  crisis: "Urgent or upsetting message",
  blocklist: "Blocked phrase",
  model_requested: "The agent asked for a person",
  email_undeliverable: "A reply could not be delivered",
  email_thread_loop: "An email thread went in circles",
  draft: "Drafted for a human to send",
};

/**
 * One line each, for the ones a tenant can switch on. `draft` is absent from
 * `TRIGGER_ORDER` below and therefore never rendered: the email channel's draft
 * mode is its switch, and the server records it without consulting the trigger
 * list, so a checkbox for it would promise a toggle that does nothing.
 */
const TRIGGER_HINT: Record<EscalationTrigger, string> = {  low_confidence: "The answer scored below this agent's confidence floor.",
  ungrounded: "The agent wrote an answer your sources do not support.",
  user_requested: "Someone asked for a human in as many words.",
  negative_feedback: "A customer left a thumbs down on an answer.",
  crisis: "The message reads as urgent, distressed or a safety matter.",
  blocklist: "One of your blocked phrases came up.",
  model_requested: "The agent decided itself that a person should take this.",
  email_undeliverable: "A reply bounced, so the customer never got it.",
  email_thread_loop: "The same email thread came back too many times.",
  draft: "Draft mode wrote the reply and left it for a person to send.",
};

const TRIGGER_ORDER: EscalationTrigger[] = [
  "low_confidence",
  "ungrounded",
  "user_requested",
  "negative_feedback",
  "crisis",
  "blocklist",
  "model_requested",
  "email_undeliverable",
  "email_thread_loop",
];

/** Mirrors `DEFAULT_ESCALATION_TRIGGERS` on the server. */
const DEFAULT_TRIGGERS: EscalationTrigger[] = [
  "low_confidence",
  "user_requested",
  "crisis",
  "model_requested",
  "email_undeliverable",
  "email_thread_loop",
];

const DEFAULTS: EscalationConfig = {
  enabled: false,
  triggers: DEFAULT_TRIGGERS,
  destination_emails: [],
  slack_webhook_secret_ref: null,
  cooldown_seconds: 900,
  user_message: "I've passed this to a teammate — someone will get back to you shortly.",
  collect_contact: true,
  collect_contact_required: false,
  transcript_turns: 6,
  email_reply_mode: "draft",
  email_auto_reply_min_confidence: 0.65,
  email_require_grounded: true,
  email_require_citation: true,
  email_signature: "",
};

const HANDED_TIP =
  "Handoffs ÷ questions the agent answered. A conversation can be handed over once even if the agent answered several questions in it.";
const TOO_FEW = "Not enough answers yet to be meaningful.";
// The read is tenant-wide, like every other analytics read. Saying so is cheaper
// than a number the tenant will misread as this agent's alone.
const SCOPE_NOTE = "Counted across every agent in this workspace.";

/** 60–86400 on the server. Offered as a list so an invalid value cannot be typed. */
const COOLDOWNS: Array<{ value: number; label: string }> = [
  { value: 300, label: "5 minutes" },
  { value: 900, label: "15 minutes" },
  { value: 1800, label: "30 minutes" },
  { value: 3600, label: "1 hour" },
  { value: 14_400, label: "4 hours" },
  { value: 86_400, label: "24 hours" },
];

/** Server-side gates a `suppressed_reason` in plain words. */
const SUPPRESSED_WORDS: Record<string, string> = {
  disabled: "handoffs are switched off for this workspace",
  trigger_off: "that trigger is not one of the ones you switched on",
  no_destination: "there is nowhere to send it yet",
  no_contact: "the test had no way to reach anyone",
  rate_limited: "this workspace has hit its hourly handoff limit",
  cooldown: "an identical handoff went out recently and this one was deduped",
};

/** Only the shape of an address is checked here; the API owns who is allowed. */
function emailError(entry: string): string | null {
  if (!/^[^@\s]+@[^@\s.]+\.[^@\s]+$/.test(entry)) return "That does not look like an email address.";
  return null;
}

/** Every field, stored value first, so a save can send the block complete. */
function normalise(stored: EscalationConfig | undefined): EscalationConfig {
  if (!stored) return { ...DEFAULTS, triggers: [...DEFAULT_TRIGGERS] };
  // Checked against every name the server knows, not just the ones offered as a
  // checkbox, so a value this screen does not render — `draft` — survives a save
  // instead of being quietly stripped out of the stored list.
  const triggers = Array.isArray(stored.triggers)
    ? stored.triggers.filter((t) => t in TRIGGER_LABEL)
    : [...DEFAULT_TRIGGERS];
  return {
    enabled: stored.enabled ?? DEFAULTS.enabled,
    triggers,
    destination_emails: Array.isArray(stored.destination_emails) ? [...stored.destination_emails] : [],
    slack_webhook_secret_ref: stored.slack_webhook_secret_ref ?? null,
    cooldown_seconds: stored.cooldown_seconds ?? DEFAULTS.cooldown_seconds,
    user_message: stored.user_message ?? DEFAULTS.user_message,
    collect_contact: stored.collect_contact ?? DEFAULTS.collect_contact,
    collect_contact_required: stored.collect_contact_required ?? DEFAULTS.collect_contact_required,
    transcript_turns: stored.transcript_turns ?? DEFAULTS.transcript_turns,
    email_reply_mode: stored.email_reply_mode ?? DEFAULTS.email_reply_mode,
    email_auto_reply_min_confidence:
      stored.email_auto_reply_min_confidence ?? DEFAULTS.email_auto_reply_min_confidence,
    email_require_grounded: stored.email_require_grounded ?? DEFAULTS.email_require_grounded,
    email_require_citation: stored.email_require_citation ?? DEFAULTS.email_require_citation,
    email_signature: stored.email_signature ?? DEFAULTS.email_signature,
  };
}

function sameBlock(a: EscalationConfig, b: EscalationConfig): boolean {
  return a.enabled === b.enabled
    && a.cooldown_seconds === b.cooldown_seconds
    && a.user_message === b.user_message
    && a.collect_contact === b.collect_contact
    && a.collect_contact_required === b.collect_contact_required
    && a.transcript_turns === b.transcript_turns
    && a.triggers.length === b.triggers.length
    && a.triggers.every((t) => b.triggers.includes(t))
    && a.destination_emails.length === b.destination_emails.length
    && a.destination_emails.every((e, i) => b.destination_emails[i] === e);
}

/**
 * §7.3's handoff block: the numbers, and the settings behind them.
 *
 * `agentId` is passed rather than taken from `useAgent()` alone because it keys
 * the checkbox ids, so two panels on one page could never share a control. The
 * config itself comes from the agent shell, which owns the single write path.
 */
export function EscalationPanel({ agentId }: { agentId: string }) {
  const { agent, patch, reload } = useAgent();
  const toast = useToast();

  const stored = useMemo(() => normalise(agent?.config?.escalation), [agent]);
  const [draft, setDraft] = useState<EscalationConfig>(stored);
  // A save returns the saved row, which re-runs this and lands the form on what
  // the server actually stored rather than on what the form hoped it stored.
  useEffect(() => { if (agent) setDraft(stored); }, [agent, stored]);

  const [saving, setSaving] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [failure, setFailure] = useState("");
  const [conflict, setConflict] = useState(false);

  const [raw, setRaw] = useState<EscalationStatsOut | null>(null);
  const [answered, setAnswered] = useState<number | null>(null);
  const [statsLoaded, setStatsLoaded] = useState(false);
  const [statsGated, setStatsGated] = useState(false);
  const [statsError, setStatsError] = useState("");
  const [testing, setTesting] = useState(false);

  const loadStats = useCallback((signal?: AbortSignal) => {
    const headers = tenantHeaders();
    Promise.all([
      escalationsApi.stats({ days: 30 }, headers, signal),
      // The denominator. It degrades to "no rate" on its own rather than taking
      // the count down with it.
      api.get<Performance>("/v1/analytics/performance?days=30", headers, signal).catch(() => null),
    ])
      .then(([s, p]) => {
        setRaw(s);
        setAnswered(p ? p.answers : null);
        setStatsGated(false);
        setStatsError("");
      })
      .catch((e: unknown) => {
        if (e instanceof DOMException && e.name === "AbortError") return;
        // A missing endpoint is a degraded state, not an error page.
        if (e instanceof ApiError && e.status === 404) setStatsGated(true);
        else setStatsError(e instanceof ApiError ? e.message : "could not load the handoff numbers");
      })
      .finally(() => setStatsLoaded(true));
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    loadStats(ctrl.signal);
    return () => ctrl.abort();
  }, [loadStats]);

  const stats: EscalationStats | null = useMemo(
    () => summariseEscalations(raw, answered),
    [raw, answered],
  );

  const dirty = !sameBlock(draft, stored);
  const noWhereToSend = draft.destination_emails.length === 0
    && draft.slack_webhook_secret_ref == null;

  const set = <K extends keyof EscalationConfig>(key: K, value: EscalationConfig[K]) => {
    setDraft((prev) => ({ ...prev, [key]: value }));
    setFailure("");
  };

  const toggleTrigger = (trigger: EscalationTrigger) => {
    setDraft((prev) => ({
      ...prev,
      triggers: prev.triggers.includes(trigger)
        ? prev.triggers.filter((t) => t !== trigger)
        : [...prev.triggers, trigger],
    }));
    setFailure("");
  };

  const save = async () => {
    if (draft.enabled && noWhereToSend) {
      setFailure("Add at least one address before switching handoffs on, or there is nowhere for one to go.");
      return;
    }
    if (draft.enabled && draft.triggers.length === 0) {
      setFailure("Choose at least one thing that should hand a conversation over.");
      return;
    }
    setSaving(true);
    setFailure("");
    try {
      // The complete block, every time: the server's config merge is shallow, so
      // a partial one would reset the email fields Channels owns.
      await patch({ escalation: draft });
      toast.success(draft.enabled ? "Saved. Handoffs are live." : "Saved.");
    } catch (e) {
      if (e instanceof AgentVersionConflict) setConflict(true);
      else if (e instanceof ApiError && e.status === 400) {
        setFailure(e.message || "One of these addresses was refused. Destinations have to be workspace members or on a domain you have verified.");
      } else if (e instanceof ApiError && e.status === 403) {
        setFailure("Your role cannot change this agent's settings.");
      } else {
        setFailure(e instanceof ApiError ? e.message : "the handoff settings could not be saved");
      }
    } finally {
      setSaving(false);
    }
  };

  const sendTest = async () => {
    setTesting(true);
    try {
      const result = await escalationsApi.test(tenantHeaders());
      if (result.escalated) toast.success("The handoff went out. Check the inbox you set.");
      else {
        const reason = result.suppressed_reason ?? "";
        toast.error(`Nothing was sent: ${SUPPRESSED_WORDS[reason] ?? (reason || "a gate stopped it")}.`);
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) toast.error("Only an owner or an admin can send a test handoff.");
      else if (e instanceof ApiError && e.status === 404) toast.error("Test handoffs are not available here yet. Real handoffs are unaffected.");
      else if (e instanceof ApiError && e.status === 429) toast.error("Too many tests. Wait a moment and try again.");
      else toast.error(e instanceof ApiError ? e.message : "the test handoff could not be sent");
    } finally {
      setTesting(false);
    }
  };

  const rate = stats?.escalation_rate ?? null;

  const reloadAgent = useCallback(async () => {
    setConflict(false);
    setReloading(true);
    try {
      await reload();
    } finally {
      setReloading(false);
    }
  }, [reload]);

  return (
    <Card>
      <CardHeader
        title="When it needs a person"
        description="What happens when the agent cannot answer, and how often that has happened."
        actions={
          <Tooltip
            label={
              <span className="block max-w-xs whitespace-normal">
                {stored.enabled
                  ? "Runs one real handoff through these settings, so you know the wiring works before a customer depends on it."
                  : "Switch handoffs on and save first — there is nothing to fire a test through until then."}
              </span>
            }
          >
            <span className="inline-flex">
              <Button
                variant="secondary"
                size="sm"
                loading={testing}
                disabled={!stored.enabled}
                onClick={() => void sendTest()}
              >
                Send a test handoff
              </Button>
            </span>
          </Tooltip>
        }
      />
      <CardBody className="flex flex-col gap-5">
        {statsError !== "" && <Alert>{statsError}</Alert>}
        {!statsLoaded && <Skeleton className="h-[104px] w-full" />}

        {statsLoaded && statsGated && (
          <Alert tone="warn">
            Handoffs are not being recorded yet, so there is no count to show here. Once they are, this panel
            reports how often a conversation was passed on and what sent it there.
          </Alert>
        )}

        {statsLoaded && stats && (
          <>
            <div className="grid gap-3 sm:grid-cols-2">
              <StatCard
                label={<TipLabel text="Handed to a person" tip={`${HANDED_TIP} ${SCOPE_NOTE}`} />}
                value={stats.handed_to_a_person.toLocaleString()}
                sub={rate != null && stats.answered_questions != null
                  ? `${percent(rate)} of ${stats.answered_questions.toLocaleString()} answers`
                  : TOO_FEW}
                tone={rate != null ? handoffTone(rate) : "neutral"}
              />
              <StatCard
                label={<TipLabel text="Handoffs a day" tip={`The same count spread over the window. ${SCOPE_NOTE}`} />}
                value={perDay(stats.handed_to_a_person, stats.days)}
                sub={`Over the last ${stats.days} days`}
              />
            </div>

            {stats.by_trigger.length > 0 && (
              <div>
                <p className="m-0 mb-2 text-[11px] font-semibold uppercase tracking-wide text-faint">
                  Why a conversation gets passed on
                </p>
                <BarChart
                  data={stats.by_trigger.map((t) => ({ label: TRIGGER_LABEL[t.trigger], value: t.count }))}
                />
              </div>
            )}
          </>
        )}

        {conflict && (
          <Alert tone="warn">
            Someone else saved this agent while this form was open, so nothing was written. Reload to pick up their
            version, then set this again.
            <span className="mt-2 block">
              <Button variant="secondary" size="sm" loading={reloading} onClick={() => void reloadAgent()}>
                Reload this agent
              </Button>
            </span>
          </Alert>
        )}
        {failure !== "" && <Alert>{failure}</Alert>}

        <div className="flex flex-col gap-4 border-t border-line pt-4">
          <ToggleRow
            label="Hand a conversation over to a person"
            hint="Off means the agent says it cannot help and offers nothing further. On means it says so, collects a way to reach the customer, and tells your team."
            checked={draft.enabled}
            onChange={(v) => set("enabled", v)}
          />

          <Field
            label="Send handoffs to"
            hint={draft.destination_emails.length >= 5
              ? "Five addresses is the limit."
              : "Up to five addresses. Each has to be a member of this workspace or on a domain you have verified — anything else is refused when you save."}
          >
            <TagInput
              value={draft.destination_emails}
              onChange={(v) => set("destination_emails", v)}
              placeholder="support@yourcompany.com"
              validate={emailError}
              max={5}
              disabled={!draft.enabled}
            />
          </Field>

          {draft.slack_webhook_secret_ref != null && (
            <p className="m-0 text-[12px] leading-relaxed text-muted">
              Handoffs are also posted to the Slack webhook stored for this workspace. The webhook URL is a
              credential, so it is held as a secret and is not shown or edited here.
            </p>
          )}

          <div>
            <p className="m-0 text-sm font-semibold tracking-tight text-ink">What should hand a conversation over?</p>
            <p className="m-0 mt-0.5 text-[12px] leading-relaxed text-muted">
              Anything switched on here pages your team. Anything switched off is still recorded, so you can turn it
              on later once you know how often it happens.
            </p>
            <div className="mt-2 flex flex-col gap-2">
              {TRIGGER_ORDER.map((trigger) => {
                const on = draft.triggers.includes(trigger);
                return (
                  <label key={trigger} htmlFor={`esc-${agentId}-${trigger}`} className={cxRow(on, !draft.enabled)}>
                    <input
                      id={`esc-${agentId}-${trigger}`}
                      type="checkbox"
                      className="mt-0.5 h-4 w-4 shrink-0 cursor-pointer accent-[var(--color-brand)]"
                      checked={on}
                      disabled={!draft.enabled}
                      onChange={() => toggleTrigger(trigger)}
                    />
                    <span className="min-w-0">
                      <span className="block text-[13px] font-semibold text-ink">{TRIGGER_LABEL[trigger]}</span>
                      <span className="mt-0.5 block text-[12px] leading-relaxed text-muted">{TRIGGER_HINT[trigger]}</span>
                    </span>
                  </label>
                );
              })}
            </div>
          </div>

          <Field
            label="What the customer is told"
            hint="Shown once, in the conversation, at the moment it is passed on. Leave it empty to say nothing extra."
          >
            <Textarea
              rows={2}
              value={draft.user_message}
              placeholder={DEFAULTS.user_message}
              onChange={(e) => set("user_message", e.target.value)}
              disabled={!draft.enabled}
            />
          </Field>

          <ToggleRow
            label="Ask for a way to reach them first"
            hint="On channels where we do not already know it. On WhatsApp and email we do, so this changes nothing there."
            checked={draft.collect_contact}
            onChange={(v) => set("collect_contact", v)}
            disabled={!draft.enabled}
          />
          <ToggleRow
            label="Do not hand over without it"
            hint="A customer who will not leave an address is not passed on at all. Off is usually right: your team can still see the question."
            checked={draft.collect_contact_required}
            onChange={(v) => set("collect_contact_required", v)}
            disabled={!draft.enabled || !draft.collect_contact}
          />

          <Field
            label="How much of the conversation to include"
            hint="The turns sent with the handoff. None sends the question on its own."
          >
            <Slider
              value={draft.transcript_turns}
              onChange={(v) => set("transcript_turns", Math.round(v))}
              min={0}
              max={50}
              step={1}
              minLabel="The question only"
              maxLabel="The whole thread"
              valueLabel={draft.transcript_turns === 0 ? "The question only" : `${draft.transcript_turns} turns`}
              disabled={!draft.enabled}
            />
          </Field>

          <Field
            label="Wait before passing the same conversation on again"
            hint="One customer rephrasing the same question five times should reach your team once."
          >
            <Select
              value={String(draft.cooldown_seconds)}
              onChange={(e) => set("cooldown_seconds", Number(e.target.value))}
              disabled={!draft.enabled}
            >
              {COOLDOWNS.map((c) => (
                <option key={c.value} value={c.value}>{c.label}</option>
              ))}
              {!COOLDOWNS.some((c) => c.value === draft.cooldown_seconds) && (
                <option value={draft.cooldown_seconds}>{`${draft.cooldown_seconds} seconds`}</option>
              )}
            </Select>
          </Field>

          <p className="m-0 text-[12px] leading-relaxed text-muted">
            How this agent answers email — reply mode, and how sure it has to be before it writes back on its own —
            is part of the same settings, and is edited on Channels beside the mailbox it governs.
          </p>

          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" loading={saving} disabled={!dirty} onClick={() => void save()}>
              Save handoff settings
            </Button>
            {!dirty && agent != null && (
              <Badge tone={stored.enabled ? "ok" : "neutral"} dot>
                {stored.enabled ? "Live" : "Off"}
              </Badge>
            )}
            {dirty && <span className="text-[12px] text-muted">Not saved yet.</span>}
          </div>
        </div>
      </CardBody>
    </Card>
  );
}

// The tooltip bubble is whitespace-nowrap, so a sentence this long has to be
// re-wrapped inside it or it runs off the right edge of the viewport.
function TipLabel({ text, tip }: { text: string; tip: string }) {
  return (
    <Tooltip label={<span className="block max-w-xs whitespace-normal">{tip}</span>}>
      <span className="inline-flex items-center gap-1">
        {text}<Icon.Info size={12} className="text-faint" />
      </span>
    </Tooltip>
  );
}

function cxRow(active: boolean, disabled: boolean): string {
  return [
    "flex items-start gap-2.5 rounded-lg border px-3 py-2.5 transition-colors",
    disabled ? "cursor-not-allowed opacity-60" : "cursor-pointer",
    active ? "border-brand bg-brand-soft/40" : "border-line bg-canvas",
    active || disabled ? "" : "hover:border-line-strong",
  ].join(" ");
}

// §7.3 states the cutoffs on the share handled without a person: green above
// 70%, neutral 50–70%, and below half is a problem the tenant needs framed as
// one. This card is the same ratio read from the other side, so it is the
// complement of those numbers rather than a second set.
function handoffTone(rate: number): "ok" | "neutral" | "danger" {
  if (rate < 0.3) return "ok";
  if (rate <= 0.5) return "neutral";
  return "danger";
}

// One decimal, because a workspace with three handoffs in a month is not having
// "0 a day" and should not be told it is.
function perDay(total: number, days: number): string {
  if (days <= 0) return "—";
  const value = total / days;
  return value >= 10 ? Math.round(value).toLocaleString() : value.toFixed(1);
}

