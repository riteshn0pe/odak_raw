// Channels (§4.7): where people reach this assistant. A card grid, not a tab bar
// — one card each for WhatsApp, Email, Slack and API.
// Every card states what it is, whether it is live, and the one action that
// changes that. `Manage` opens a SlideOver holding the per-channel form.
//
// B17 is the whole shape of this screen: channel integrations are queried per
// TENANT, never per agent, so a card-per-channel grid that asked the server for
// this agent's channels would N+1 or need endpoints that do not exist. Instead the
// two tenant-wide lists are fetched ONCE here and filtered client-side to this
// agentId, and the filtering is stated on screen rather than hidden. No per-agent
// endpoint is invented.
//
// Two cards are honestly dark: Help page has no backend at all and Slack does not
// exist. Each renders with its real copy, a disabled action, and the reason in
// plain words — a card that lies about being available is worse than a card that
// says "not yet". Email is live as of Wave 8 and reads the tenant's mailboxes like
// the other two lists; if that endpoint is not deployed yet it 404s and the card
// falls back to the same honest dark state instead of failing the screen.
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, ApiError } from "../../../lib/api";
import { AgentVersionConflict } from "../../../lib/agentConfig";
import { tenantHeaders } from "../../../lib/auth";
import type {
  EmailChannelIn, EmailChannelOut, EmailChannelState, EmailVerifyResult, EscalationConfig,
  WhatsAppIntegration,
} from "../../../lib/types";
import {
  Alert, Badge, Banner, Button, Card, CardBody, Field, Input, PageHeader, SegmentedControl,
  Skeleton, Slider, TagInput, ToggleRow, cx,
} from "../../../components/ui";
import { Icon } from "../../../components/ui/icons";
import { SlideOver } from "../../../components/ui/SlideOver";
import { useToast } from "../../../components/ui/Toast";
import { useAgent } from "./AgentLayout";
import { WhatsAppPanel } from "./channels/WhatsAppPanel";

// Badge's own Tone union is module-private in components/ui, so the subset the
// cards use is spelled out here rather than imported.
type CardTone = "neutral" | "ok" | "warn" | "danger" | "brand" | "accent";

type PanelKey = "whatsapp" | "email";

type ReplyMode = "off" | "draft" | "auto";

const REPLY_MODES = [
  { value: "off", label: "Off" },
  { value: "draft", label: "Draft only" },
  { value: "auto", label: "Reply automatically" },
];

// The server default: `draft` never writes to the customer, because the failure
// mode of `auto` is silent, external and permanent. Kept here as the fallback for
// an agent row that predates the block.
const DEFAULT_REPLY_MODE: ReplyMode = "draft";
const DEFAULT_FLOOR = 0.65;

// Reply mode and the confidence floor are two fields of the agent's `escalation`
// block, saved through the same `PATCH /v1/agents/{id}` as every other section —
// they are edited here rather than on the handoff panel because this is where the
// mailbox they govern is configured. The mailbox row itself is a separate write,
// so the note says both landed rather than leaving the tenant guessing. The note
// is explicit that the mode is the agent's own, not the mailbox's: it must read
// correctly on an agent that does not own the mailbox at all.
const REPLY_NOTE =
  "Reply mode is this agent's own setting, separate from the mailbox. It decides how this agent answers email, and it is the same field as the one under Handoff — editing it here edits it there. Nothing is sent to a customer until the sending domain below is verified.";

/**
 * The email channel form. One mailbox per tenant: we provision the address, the
 * customer forwards their support mail to it, and the agent files what arrives
 * against a conversation.
 *
 * The address is deliberately not editable. The local part is the inbound routing
 * key, so a tenant-chosen one either collides with another workspace or resolves
 * to somebody else's mail; the server provisions it on connect and rotates it
 * only on request. `state` carries the deployment's inbound domain and the
 * server-written forwarding paragraph, both of which the form prints verbatim.
 *
 * `escalation` is the stored handoff block, and `onSaveReply` writes the two
 * fields this form owns back into it — the parent holds the complete block,
 * because the server's config merge is shallow and a partial one would reset the
 * rest of it.
 */
function EmailPanel({ agentId, agentName, state, channel, escalation, onSaveReply, onChanged, onClose }: {
  agentId: string;
  agentName: string;
  state: EmailChannelState | null;
  channel: EmailChannelOut | null;
  escalation: EscalationConfig | undefined;
  onSaveReply: (mode: ReplyMode, floor: number) => Promise<void>;
  onChanged: () => Promise<void> | void;
  onClose: () => void;
}) {
  const toast = useToast();
  const [forwardFrom, setForwardFrom] = useState(channel?.forward_from ?? "");
  const [displayName, setDisplayName] = useState(channel?.display_name ?? agentName);
  const [forwarders, setForwarders] = useState<string[]>(channel?.allowed_forwarders ?? []);
  const [attachments, setAttachments] = useState(channel?.ingest_attachments ?? false);
  const [linkSources, setLinkSources] = useState(channel?.link_sources ?? false);

  const [mode, setMode] = useState<ReplyMode>(escalation?.email_reply_mode ?? DEFAULT_REPLY_MODE);
  const [floor, setFloor] = useState(escalation?.email_auto_reply_min_confidence ?? DEFAULT_FLOOR);

  const [saving, setSaving] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);
  const [failure, setFailure] = useState("");

  const forwardError = forwardFrom.trim() !== "" && !/^[^@\s]+@[^@\s.]+\.[^@\s]+$/.test(forwardFrom.trim())
    ? "That does not look like an address we can attribute mail to."
    : null;

  const save = async () => {
    if (forwardError) {
      setFailure(forwardError);
      return;
    }
    setSaving(true);
    setFailure("");
    // No `address`: the server provisions it and rejects the field outright. On
    // the first write `agent_id` is what attaches the new mailbox to this agent.
    const body: EmailChannelIn = {
      agent_id: agentId,
      forward_from: forwardFrom.trim() === "" ? null : forwardFrom.trim(),
      display_name: displayName.trim() === "" ? null : displayName.trim(),
      allowed_forwarders: forwarders,
      ingest_attachments: attachments,
      link_sources: linkSources,
    };
    try {
      // The PUT answers with the provisioned mailbox, address included — the
      // one thing the tenant cannot find anywhere else on first connect, so the
      // toast names it instead of pointing at an "it" they have never seen.
      const saved = await api.put<EmailChannelOut>("/v1/integrations/email", body, tenantHeaders());
      await onSaveReply(mode, floor);
      await onChanged();
      toast.success(
        channel
          ? "Saved. Forward a message to it and watch Activity."
          : `Connected. Forward your mail to ${saved.address} — add that forwarding rule in your mail provider now.`,
      );
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        setFailure("Email is not available here yet, so the mailbox could not be saved. Reply mode is stored with the agent and was saved.");
        await onSaveReply(mode, floor).catch(() => undefined);
      } else if (e instanceof ApiError && e.status === 409) {
        setFailure("Another agent in this workspace already watches that address.");
      } else {
        setFailure(e instanceof ApiError ? e.message : "the mailbox could not be saved");
      }
    } finally {
      setSaving(false);
    }
  };

  const verify = async () => {
    setVerifying(true);
    setFailure("");
    try {
      const result = await api.post<EmailVerifyResult>("/v1/integrations/email/verify", {}, tenantHeaders());
      await onChanged();
      // The provider refusing is reported on the row rather than raised, so a
      // 200 with `sent: false` is a real failure and has to be surfaced here.
      if (result.sent) {
        toast.success(`Test message sent to ${result.probe_to}. If it arrived, sending works.`);
      } else {
        setFailure(result.error ?? "the test message could not be sent");
      }
    } catch (e) {
      setFailure(e instanceof ApiError ? e.message : "the mailbox could not be checked");
    } finally {
      setVerifying(false);
    }
  };

  const disconnect = async () => {
    setDisconnecting(true);
    setFailure("");
    try {
      await api.del<void>("/v1/integrations/email");
      await onChanged();
      onClose();
      toast.success("Disconnected. Mail forwarded here from now on is dropped.");
    } catch (e) {
      setFailure(e instanceof ApiError ? e.message : "the mailbox could not be disconnected");
    } finally {
      setDisconnecting(false);
    }
  };

  const verified = channel?.sending_domain_verified === true;
  // The route answered, so the channel is built and mounted — it is this
  // deployment that has no mail provider behind it. `PUT /v1/integrations/email`
  // refuses with exactly that reason, so saying it before the form is filled in
  // is kinder than a rejection after.
  const needsServer = state != null && (state.backend === "none" || state.inbound_domain === "");

  return (
    <div className="flex flex-col gap-5">
      {failure !== "" && <Alert>{failure}</Alert>}

      {needsServer && (
        <Alert tone="warn">
          <span className="font-semibold">Email needs a one-time server setup.</span>{" "}
          Everything on this screen is built and running; a mailbox can exist once four settings are in
          place on the server:{" "}
          <code>EMAIL_BACKEND=resend</code>, <code>RESEND_API_KEY</code>,{" "}
          <code>RESEND_WEBHOOK_SECRET</code> and <code>EMAIL_INBOUND_DOMAIN</code> (plus{" "}
          <code>EMAIL_FROM_ADDRESS</code> on the same domain). Set them, restart the API, and
          Connect turns on here with nothing else to do.
        </Alert>
      )}

      {channel && channel.error_reason && (
        <Alert tone="warn">{channel.error_reason}</Alert>
      )}

      <div className="flex flex-col gap-4">
        {channel ? (
          <Field
            label="Forward your mail to this address"
            hint={state?.forwarding_instructions !== "" ? state?.forwarding_instructions : undefined}
          >
            <Input value={channel.address} readOnly />
          </Field>
        ) : needsServer ? null : (
          <Alert tone="neutral">
            {state?.inbound_domain
              ? `Press Connect and we will provision an address on ${state.inbound_domain} for you. You then add a forwarding rule in your own mail provider pointing at it — your customers keep writing to your address, not ours.`
              : "Press Connect and we will provision the address you forward your support mail to."}
          </Alert>
        )}

        <Field
          label="Forwarded from"
          hint={forwardError ?? "Optional. Your own support mailbox, the one whose forwarding rule points here. Used to attribute what arrives."}
        >
          <Input
            value={forwardFrom}
            placeholder="help@yourcompany.com"
            onChange={(e) => setForwardFrom(e.target.value)}
            aria-invalid={forwardError ? true : undefined}
          />
        </Field>

        <Field label="Name on the reply" hint="What the customer sees in their inbox.">
          <Input value={displayName} placeholder={agentName} onChange={(e) => setDisplayName(e.target.value)} />
        </Field>

        <Field
          label="Who may forward to us"
          hint="Optional allowlist. Mail forwarded by anyone else is dropped, which is what stops a stranger feeding your agent."
        >
          <TagInput value={forwarders} onChange={setForwarders} placeholder="mail.yourcompany.com" max={10} />
        </Field>

        <ToggleRow
          label="Keep attachments"
          hint="Files that arrive are stored with the conversation. Off by default, because most support mail does not need them kept."
          checked={attachments}
          onChange={setAttachments}
        />
        <ToggleRow
          label="Turn attachments into sources"
          hint="A forwarded PDF becomes a document this agent can answer from. Only switch this on if you trust everyone who can forward mail here."
          checked={linkSources}
          onChange={setLinkSources}
          disabled={!attachments}
        />
      </div>

      {channel && (
        <div className="rounded-lg border border-line bg-canvas px-4 py-3">
          <p className="m-0 flex flex-wrap items-center gap-2 text-[13px] font-semibold text-ink">
            Sending
            <Badge tone={verified ? "ok" : "warn"} dot>{verified ? "Verified" : "Not verified"}</Badge>
          </p>
          <p className="m-0 mt-1 text-[12px] leading-relaxed text-muted">
            {channel.sending_domain
              ? verified
                ? `Replies send from ${channel.sending_domain}.`
                : `${channel.sending_domain} is not verified yet. Send a test message to yourself to prove sending works — until it does, the agent can read mail but not answer it.`
              : "Send a test message to yourself to prove sending works. Until it does, the agent can read mail but cannot answer it."}
          </p>
          {channel.audit_sends_remaining > 0 && (
            <p className="m-0 mt-1 text-[12px] leading-relaxed text-faint">
              The next {channel.audit_sends_remaining} automatic {channel.audit_sends_remaining === 1 ? "reply" : "replies"} {channel.audit_sends_remaining === 1 ? "is" : "are"} also copied to your handoff destinations, so a person reads what this mailbox sends first.
            </p>
          )}
          <div className="mt-2">
            <Button variant="secondary" size="sm" loading={verifying} onClick={() => void verify()}>
              Send a test message
            </Button>
          </div>
        </div>
      )}

      <div className="flex flex-col gap-3 border-t border-line pt-4">
        <div>
          <p className="m-0 text-[13px] font-semibold text-ink">Reply mode</p>
          <p className="m-0 mt-0.5 text-[12px] leading-relaxed text-muted">
            Off files the mail and does nothing. Draft only writes the reply and sends it to you. Reply automatically
            answers the customer without anyone reading it first.
          </p>
        </div>
        <SegmentedControl items={REPLY_MODES} value={mode} onChange={(v) => setMode(v as ReplyMode)} />

        {mode === "auto" && (
          <Field
            label="Only reply automatically when it is this sure"
            hint="Below this the mail waits for a person. Higher than chat's floor on purpose: nobody is watching an email arrive, and no follow-up turn can correct it."
          >
            <Slider
              value={floor}
              onChange={setFloor}
              min={0.4}
              max={0.95}
              step={0.05}
              minLabel="Answer more often"
              maxLabel="Only when certain"
              valueLabel={`${Math.round(floor * 100)}%`}
            />
          </Field>
        )}

        <Alert tone="neutral">{REPLY_NOTE}</Alert>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line pt-4">
        {channel ? (
          <Button variant="ghost" size="sm" loading={disconnecting} onClick={() => void disconnect()}>
            Disconnect this mailbox
          </Button>
        ) : <span />}
        <Button loading={saving} disabled={needsServer && !channel} onClick={() => void save()}>
          {channel ? "Save" : "Connect"}
        </Button>
      </div>
    </div>
  );
}

/**
 * One channel. `plate` is the coloured illustration ground: a 44px square beside
 * the name. `note` is the honest reason an action is disabled and always
 * renders on screen.
 */
function ChannelCard({ plate, icon, name, description, status, tone, action, note }: {
  plate: string;
  icon: ReactNode;
  name: string;
  description: string;
  status: string;
  tone: CardTone;
  action: ReactNode;
  note?: ReactNode;
}) {
  return (
    <Card className="flex flex-col overflow-hidden">
      <CardBody className="flex flex-1 flex-col gap-3">
        <div className="flex items-start gap-3">
          <span className={cx("grid h-11 w-11 shrink-0 place-items-center rounded-lg", plate)} aria-hidden="true">
            {icon}
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="m-0 font-semibold tracking-tight text-ink text-sm">{name}</h3>
              <Badge tone={tone} dot>{status}</Badge>
            </div>
            <p className="m-0 mt-1.5 text-[13px] leading-relaxed text-muted">{description}</p>
          </div>
        </div>
        {note && <p className="m-0 text-[12px] leading-relaxed text-faint">{note}</p>}
        <div className="mt-auto flex items-center gap-2 pt-1">{action}</div>
      </CardBody>
    </Card>
  );
}

function ChannelsSkeleton() {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <Skeleton className="h-7 w-36" />
        <Skeleton className="h-4 w-72" />
      </div>
      <Skeleton className="h-14 w-full" />
      <div className="grid gap-4 sm:grid-cols-3">
        {[0, 1, 2, 3].map((i) => (
          <Card key={i}>
            <CardBody className="flex flex-col gap-3">
              <div className="flex items-start gap-3">
                <Skeleton className="h-11 w-11 rounded-lg" />
                <div className="flex flex-1 flex-col gap-2">
                  <Skeleton className="h-4 w-24" />
                  <Skeleton className="h-3.5 w-full" />
                </div>
              </div>
              <Skeleton className="h-9 w-24" />
            </CardBody>
          </Card>
        ))}
      </div>
    </div>
  );
}

export function Channels() {
  const { agentId, agent, loading, error, patch } = useAgent();
  const toast = useToast();

  // The tenant-wide list, read once (B17).
  const [whatsapp, setWhatsapp] = useState<WhatsAppIntegration[]>([]);
  // Email is one mailbox per tenant, not a list: `GET /v1/integrations/email`
  // answers with the card's whole state — the deployment's backend and inbound
  // domain, plus the row when there is one. On 404 the card stays honest instead
  // of taking the screen down, which is the §8.0 degraded state rather than an
  // error.
  const [email, setEmail] = useState<EmailChannelState | null>(null);
  // Two different reasons the mailbox cannot be connected, and they read very
  // differently to whoever is looking at the card. `emailGated` is "this build
  // serves no email route at all", which nobody using the dashboard can act on.
  // `emailNeedsServer` is "the route is here, this deployment has no provider
  // wired to it" — one env block away from working, so the card says so and the
  // panel names the variables instead of pretending the feature is absent.
  const [emailGated, setEmailGated] = useState(false);
  const [emailNeedsServer, setEmailNeedsServer] = useState(false);
  const [listError, setListError] = useState("");
  const [listLoaded, setListLoaded] = useState(false);
  const [panel, setPanel] = useState<PanelKey | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const h = tenantHeaders();
      const [wa, em] = await Promise.all([
        api.get<WhatsAppIntegration[]>("/v1/integrations/whatsapp", h, signal),
        api.get<EmailChannelState>("/v1/integrations/email", h, signal).catch((e: unknown) => {
          if (e instanceof ApiError && e.status === 404) return null;
          throw e;
        }),
      ]);
      setWhatsapp(wa);
      setEmail(em);
      setEmailGated(em == null);
      setEmailNeedsServer(em != null && (em.backend === "none" || em.inbound_domain === ""));
      setListError("");
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      setListError(e instanceof ApiError ? e.message : "could not load this workspace's channels");
    } finally {
      setListLoaded(true);
    }
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    void load(ctrl.signal);
    return () => ctrl.abort();
  }, [load]);

  /**
   * Write the email card's two fields back into the agent's handoff block.
   *
   * The whole block goes with them, stored values first: the server merges config
   * shallowly at the top level, so a block sent with only these two fields would
   * reset the destinations, the triggers and the cooldown to their defaults. It
   * reports its own failure rather than throwing, because the mailbox itself has
   * already been written by the time this runs and the two are separate rows.
   */
  const saveReplyMode = useCallback(async (mode: ReplyMode, floor: number) => {
    const stored = agent?.config?.escalation;
    try {
      await patch({
        escalation: {
          ...(stored ?? {}),
          email_reply_mode: mode,
          email_auto_reply_min_confidence: floor,
        },
      });
    } catch (e) {
      if (e instanceof AgentVersionConflict) {
        toast.error("Someone else saved this agent while this was open, so reply mode was not changed. Reopen this panel and set it again.");
      } else {
        toast.error(e instanceof ApiError ? e.message : "reply mode could not be saved");
      }
    }
  }, [agent, patch, toast]);

  // Client-side scoping. Memoised so the panels, which re-sync their drafts when
  // their slice changes, are not handed a brand-new array on every render.
  const myWhatsapp = useMemo(() => whatsapp.filter((w) => w.agent_id === agentId), [whatsapp, agentId]);
  const otherWhatsapp = whatsapp.length - myWhatsapp.length;
  // One mailbox per tenant, attached to exactly one agent. It belongs to the open
  // agent or it does not; there is no list to slice.
  const myEmail = useMemo(
    () => (email?.channel && email.channel.agent_id === agentId ? email.channel : null),
    [email, agentId],
  );
  const otherEmail = email?.channel && !myEmail ? 1 : 0;

  const agentName = agent?.name ?? "this agent";

  if (error && !agent) {
    return (
      <div className="flex flex-col gap-6">
        <PageHeader title="Channels" description="Where people reach this assistant." />
        <Alert>{error}</Alert>
      </div>
    );
  }

  if (!listLoaded || (loading && !agent)) return <ChannelsSkeleton />;

  if (listError) {
    return (
      <div className="flex flex-col gap-6">
        <PageHeader title="Channels" description="Where people reach this assistant." />
        <Alert tone="danger">{listError}</Alert>
        <div>
          <Button variant="secondary" onClick={() => void load()}>Try again</Button>
        </div>
      </div>
    );
  }

  const waLive = myWhatsapp.length > 0;
  const waTokenMissing = myWhatsapp.some((w) => !w.token_configured);

  return (
    <div className="flex flex-col gap-6">
      <PageHeader title="Channels" description="Where people reach this assistant." />

      <Banner tone="neutral" icon={<Icon.Info size={16} />}>
        Channels are set up per agent. WhatsApp numbers are attached to one agent,
        so set them up for every agent that needs them. Email is different: one mailbox for the
        whole workspace, attached to one agent — it only shows on that agent.
      </Banner>

      <div className="grid gap-4 sm:grid-cols-3">
        <ChannelCard
          plate="bg-ok-soft text-ok"
          icon={<Icon.Chat size={20} />}
          name="WhatsApp"
          description="Answer messages sent to your WhatsApp Business number."
          status={waLive ? (waTokenMissing ? "Token missing" : myWhatsapp.length === 1 ? "Connected" : `${myWhatsapp.length} numbers`) : "Not set up"}
          tone={waLive ? (waTokenMissing ? "warn" : "ok") : "neutral"}
          action={
            <Button variant={waLive ? "secondary" : "primary"} onClick={() => setPanel("whatsapp")}>
              {waLive ? "Manage" : "Set up"}
            </Button>
          }
          note={otherWhatsapp > 0
            ? `${otherWhatsapp} other number${otherWhatsapp === 1 ? "" : "s"} in this workspace answer${otherWhatsapp === 1 ? "s" : ""} with a different agent.`
            : undefined}
        />
        <ChannelCard
          plate="bg-warn-soft text-warn"
          icon={<Icon.Inbox size={20} />}
          name="Email"
          description="Forward your support@ address here and the agent will reply."
          status={emailGated
            ? "Not available"
            : emailNeedsServer
              ? "Needs server setup"
              : myEmail
                ? myEmail.sending_domain_verified
                  ? "Connected"
                  : "Needs DNS"
                : "Not set up"}
          tone={emailGated || emailNeedsServer
            ? "neutral"
            : myEmail
              ? (myEmail.sending_domain_verified ? "ok" : "warn")
              : "neutral"}
          action={
            <Button
              variant={myEmail ? "secondary" : "primary"}
              disabled={emailGated}
              onClick={() => setPanel("email")}
            >
              {myEmail ? "Manage" : emailNeedsServer ? "What is missing" : "Set up"}
            </Button>
          }
          note={emailGated
            ? "Email is not available here yet, so there is nothing to configure from this page."
            : emailNeedsServer
              ? "The channel is built and running — email just needs a one-time server setup. Open the card to see which settings to fill in."
              : myEmail
                ? `Forward your mail to ${myEmail.address} — add that rule in your mail provider, then press Manage to verify.`
                : otherEmail > 0
                  ? `${otherEmail} other mailbox${otherEmail === 1 ? "" : "es"} in this workspace answer${otherEmail === 1 ? "s" : ""} with a different agent.`
                  : undefined}
        />
        <ChannelCard
          plate="bg-trace-soft text-trace"
          icon={<Icon.Plug size={20} />}
          name="Slack"
          description="Answer in a Slack channel or a DM with the agent."
          status="Not available"
          tone="neutral"
          action={<Button variant="secondary" disabled>Set up</Button>}
          note="No Slack app exists for this workspace yet."
        />
        <ChannelCard
          plate="bg-canvas text-muted"
          icon={<Icon.Key size={20} />}
          name="API"
          description="Call the agent from your own backend and put the answer wherever you like."
          status="Always on"
          tone="ok"
          action={null}
        />
      </div>

      <SlideOver
        open={panel === "whatsapp"}
        onClose={() => setPanel(null)}
        title="WhatsApp"
        description={`Numbers and message templates answered by ${agentName}.`}
        width={720}
      >
        <WhatsAppPanel
          agentId={agentId}
          agentName={agentName}
          integrations={myWhatsapp}
          unattached={otherWhatsapp}
          onChanged={load}
        />
      </SlideOver>

      <SlideOver
        open={panel === "email"}
        onClose={() => setPanel(null)}
        title="Email"
        description={`Connect the mailbox ${agentName} answers from, and choose how it replies.`}
        width={720}
      >
        <EmailPanel
          agentId={agentId}
          agentName={agentName}
          state={email}
          channel={myEmail}
          escalation={agent?.config?.escalation}
          onSaveReply={saveReplyMode}
          onChanged={load}
          onClose={() => setPanel(null)}
        />
      </SlideOver>
    </div>
  );
}
