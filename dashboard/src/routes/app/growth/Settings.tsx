// Growth settings: approval levels, send caps and working hours, follow-up
// schedule, offer range, languages, groundedness — and the two destructive
// compliance affordances.
//
// Each card saves its own block. That is not a stylistic choice: the API takes a
// partial edit per block and leaves untouched blocks alone, so a per-card save
// means an owner editing send caps can never clobber an approval level someone
// else changed in another tab a second earlier.
import { useEffect, useState } from "react";
import { ApiError, growthApi } from "../../../lib/api";
import { tenantHeaders } from "../../../lib/auth";
import type {
  GrowthFollowUp,
  GrowthGrounding,
  GrowthHitlLevels,
  GrowthLanguages,
  GrowthOfferRange,
  GrowthSendCaps,
  GrowthSettings as Settings,
  HitlLevel,
} from "../../../lib/types";
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardHeader,
  Divider,
  Field,
  Input,
  PageHeader,
  SegmentedControl,
  Select,
  Skeleton,
  Spinner,
  TagInput,
  ToggleRow,
} from "../../../components/ui";
import { useToast } from "../../../components/ui/Toast";

const LEVELS: { value: string; label: string }[] = [
  { value: "0", label: "Automatic" },
  { value: "1", label: "Per batch" },
  { value: "2", label: "Every message" },
];

const LEVEL_HINT: Record<string, string> = {
  "0": "The agent acts and tells you afterwards. Nothing waits on you.",
  "1": "You approve one batch at a time. The default, and the one most people want.",
  "2": "You approve every single message before it goes out.",
};

const FEATURES: { key: keyof GrowthHitlLevels; label: string; hint: string }[] = [
  { key: "outbound_batch", label: "Cold outreach", hint: "Mail to people who have not contacted you." },
  { key: "campaign", label: "Campaigns", hint: "Offers and broadcasts to your own customers." },
  { key: "review_ask", label: "Review requests", hint: "Asking a customer to leave a review." },
];

// Monday-first, matching `Date.getDay() - 1` as the backend stores it.
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

const GROUNDED_AGENTS: { key: string; label: string }[] = [
  { key: "growth_research", label: "Research" },
  { key: "growth_compose", label: "Cold mail" },
  { key: "growth_replies", label: "Replies" },
  { key: "growth_report", label: "Reports" },
  { key: "growth_campaign", label: "Campaigns" },
  { key: "growth_insights", label: "Insights" },
];

export function GrowthSettings() {
  const toast = useToast();

  const [settings, setSettings] = useState<Settings | null>(null);
  const [error, setError] = useState("");
  const [busyBlock, setBusyBlock] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    growthApi
      .settings(tenantHeaders(), controller.signal)
      .then(setSettings)
      .catch((e) => {
        if (controller.signal.aborted) return;
        setError(e instanceof ApiError ? e.message : "could not load your settings");
      });
    return () => controller.abort();
  }, []);

  /** Optimistic local edit. Nothing reaches the server until the card is saved. */
  function edit<K extends keyof Settings>(block: K, value: Partial<Settings[K]>) {
    setSettings((s) => (s ? { ...s, [block]: { ...s[block], ...value } } : s));
  }

  async function saveBlock(block: keyof Settings, label: string) {
    if (!settings) return;
    setBusyBlock(block);
    setError("");
    try {
      const result = await growthApi.saveSettings(
        { [block]: settings[block] } as never,
        tenantHeaders(),
      );
      // re-seed from the response rather than keeping the draft: the server
      // normalises (English is added to the language list, day lists are sorted)
      // and the screen should show what was actually stored
      setSettings(result);
      toast.success(`${label} saved.`);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : `could not save ${label.toLowerCase()}`);
    } finally {
      setBusyBlock(null);
    }
  }

  if (error && !settings) {
    return (
      <div className="max-w-3xl">
        <PageHeader title="Growth settings" />
        <Alert>{error}</Alert>
      </div>
    );
  }

  if (!settings) {
    return (
      <div className="max-w-3xl">
        <PageHeader title="Growth settings" />
        <div className="flex flex-col gap-6">
          {Array.from({ length: 3 }).map((_, i) => (
            <Card key={i}>
              <CardBody className="flex flex-col gap-3">
                <Skeleton className="h-4 w-40" />
                <Skeleton className="h-9 w-full" />
                <Skeleton className="h-9 w-2/3" />
              </CardBody>
            </Card>
          ))}
        </div>
      </div>
    );
  }

  const caps: GrowthSendCaps = settings.send_caps;
  const follow: GrowthFollowUp = settings.follow_up;
  const offer: GrowthOfferRange = settings.offer_range;
  const languages: GrowthLanguages = settings.languages;
  const grounding: GrowthGrounding = settings.grounding;

  function SaveButton({ block, label }: { block: keyof Settings; label: string }) {
    return (
      <Button
        variant="primary"
        size="sm"
        onClick={() => saveBlock(block, label)}
        disabled={busyBlock !== null}
      >
        {busyBlock === block ? (
          <>
            <Spinner /> Saving…
          </>
        ) : (
          "Save"
        )}
      </Button>
    );
  }

  return (
    <div className="max-w-3xl">
      <PageHeader
        title="Growth settings"
        description="What the agents may do on their own, how fast they may do it, and in whose voice."
      />
      {error && (
        <div className="mb-4">
          <Alert>{error}</Alert>
        </div>
      )}

      <Card className="mb-6">
        <CardHeader
          title="Approvals"
          description="Nothing that costs money or contacts a stranger happens without the approval you choose here."
          actions={<SaveButton block="hitl_levels" label="Approval levels" />}
        />
        <CardBody className="flex flex-col gap-6">
          {FEATURES.map((f) => (
            <Field key={f.key} label={f.label} hint={f.hint}>
              <div className="flex flex-col gap-2">
                <SegmentedControl
                  items={LEVELS}
                  value={String(settings.hitl_levels[f.key])}
                  onChange={(v) =>
                    edit("hitl_levels", { [f.key]: Number(v) as HitlLevel } as never)
                  }
                />
                <div className="text-[13px] text-muted">
                  {LEVEL_HINT[String(settings.hitl_levels[f.key])]}
                </div>
              </div>
            </Field>
          ))}
        </CardBody>
      </Card>

      <Card className="mb-6">
        <CardHeader
          title="Sending"
          description="Volume and timing. These protect your sending reputation more than anything else on this page."
          actions={<SaveButton block="send_caps" label="Send caps" />}
        />
        <CardBody className="flex flex-col gap-5">
          <div className="grid sm:grid-cols-2 gap-4">
            <Field label="Daily cap" hint="Messages per day, at most.">
              <Input
                type="number"
                min={0}
                max={5000}
                value={caps.daily_cap}
                onChange={(e) => edit("send_caps", { daily_cap: Number(e.target.value) })}
              />
            </Field>
            <Field
              label="Warmup day"
              hint="0 for an established domain. Otherwise volume ramps from 10/day."
            >
              <Input
                type="number"
                min={0}
                max={365}
                value={caps.warmup_day}
                onChange={(e) => edit("send_caps", { warmup_day: Number(e.target.value) })}
              />
            </Field>
          </div>

          <div className="grid sm:grid-cols-3 gap-4">
            <Field label="From">
              <Select
                value={String(caps.start_hour)}
                onChange={(e) => edit("send_caps", { start_hour: Number(e.target.value) })}
              >
                {Array.from({ length: 24 }).map((_, h) => (
                  <option key={h} value={h}>{`${String(h).padStart(2, "0")}:00`}</option>
                ))}
              </Select>
            </Field>
            <Field label="Until">
              <Select
                value={String(caps.end_hour)}
                onChange={(e) => edit("send_caps", { end_hour: Number(e.target.value) })}
              >
                {Array.from({ length: 24 }).map((_, h) => (
                  <option key={h + 1} value={h + 1}>{`${String(h + 1).padStart(2, "0")}:00`}</option>
                ))}
              </Select>
            </Field>
            <Field label="Timezone" hint="Your customers' clock, not ours.">
              <Input
                value={caps.timezone}
                onChange={(e) => edit("send_caps", { timezone: e.target.value })}
              />
            </Field>
          </div>

          <Field label="Working days">
            <div className="flex flex-wrap gap-2">
              {DAYS.map((label, index) => {
                const on = caps.days.includes(index);
                return (
                  <button
                    key={label}
                    type="button"
                    onClick={() =>
                      edit("send_caps", {
                        days: on
                          ? caps.days.filter((d) => d !== index)
                          : [...caps.days, index].sort((a, b) => a - b),
                      })
                    }
                    className={
                      "px-3 py-1.5 rounded-md text-[13px] font-medium border transition-colors " +
                      (on
                        ? "bg-brand-soft text-brand border-brand"
                        : "bg-surface text-muted border-line hover:text-ink")
                    }
                    aria-pressed={on}
                  >
                    {label}
                  </button>
                );
              })}
            </div>
          </Field>
        </CardBody>
      </Card>

      <Card className="mb-6">
        <CardHeader
          title="Follow-ups"
          description="Days after the first message. Three at most, and each has to add something new."
          actions={<SaveButton block="follow_up" label="Follow-ups" />}
        />
        <CardBody className="flex flex-col gap-5">
          <Field label="Days after the first message">
            <TagInput
              value={follow.days.map(String)}
              onChange={(v) =>
                edit("follow_up", {
                  days: v.map(Number).filter((n) => Number.isFinite(n) && n >= 1),
                })
              }
              validate={(v) =>
                /^\d+$/.test(v) && Number(v) >= 1 && Number(v) <= 90
                  ? null
                  : "a number of days between 1 and 90"
              }
              placeholder="3, 7, 14…"
            />
          </Field>
          <Field label="Maximum follow-ups" hint="0 turns follow-ups off entirely.">
            <Input
              type="number"
              min={0}
              max={5}
              value={follow.max_follow_ups}
              onChange={(e) => edit("follow_up", { max_follow_ups: Number(e.target.value) })}
            />
          </Field>
        </CardBody>
      </Card>

      <Card className="mb-6">
        <CardHeader
          title="Offers"
          description="The band an agent may propose inside. Outside it, a human decides."
          actions={<SaveButton block="offer_range" label="Offer range" />}
        />
        <CardBody className="grid sm:grid-cols-3 gap-4">
          <Field label="Currency">
            <Input
              value={offer.currency}
              onChange={(e) => edit("offer_range", { currency: e.target.value })}
            />
          </Field>
          <Field label="Minimum discount %">
            <Input
              type="number"
              min={0}
              max={100}
              value={offer.discount_min_pct}
              onChange={(e) => edit("offer_range", { discount_min_pct: Number(e.target.value) })}
            />
          </Field>
          <Field label="Maximum discount %">
            <Input
              type="number"
              min={0}
              max={100}
              value={offer.discount_max_pct}
              onChange={(e) => edit("offer_range", { discount_max_pct: Number(e.target.value) })}
            />
          </Field>
        </CardBody>
      </Card>

      <Card className="mb-6">
        <CardHeader
          title="Languages"
          description="Replies follow the customer's own language. English is always available as the floor."
          actions={<SaveButton block="languages" label="Languages" />}
        />
        <CardBody>
          <Field label="Allowed languages" hint="Two-letter codes, or 'hinglish'.">
            <TagInput
              value={languages.allowed}
              onChange={(v) => edit("languages", { allowed: v })}
              placeholder="en, hi, hinglish…"
            />
          </Field>
        </CardBody>
      </Card>

      <Card className="mb-6">
        <CardHeader
          title="Groundedness"
          description="A second model checks each deliverable against what we actually know before it ships."
          actions={<SaveButton block="grounding" label="Groundedness" />}
        />
        <CardBody className="flex flex-col gap-5">
          <Field
            label="When a claim is not grounded"
            hint="Advisory drops the claim. Block stops the message and asks a human."
          >
            <SegmentedControl
              items={[
                { value: "advisory", label: "Advisory" },
                { value: "block", label: "Block" },
              ]}
              value={grounding.mode}
              onChange={(v) => edit("grounding", { mode: v as "advisory" | "block" })}
            />
          </Field>
          <Divider />
          <div className="flex flex-col gap-1">
            {GROUNDED_AGENTS.map((a) => (
              <ToggleRow
                key={a.key}
                label={a.label}
                checked={grounding.agents[a.key] ?? true}
                onChange={(v) =>
                  edit("grounding", { agents: { ...grounding.agents, [a.key]: v } })
                }
              />
            ))}
          </div>
        </CardBody>
      </Card>
    </div>
  );
}
