// Connections: one card per provider the platform knows about — every one of
// them connectable.
//
// The list comes from the server, so the screen cannot drift from what the
// backend will actually accept; anything the platform has not built yet is
// simply absent rather than shown as a dead card.
import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, growthApi } from "../../../lib/api";
import { tenantHeaders } from "../../../lib/auth";
import type {
  EmailChannelState,
  GrowthFormSetup,
  GrowthIntegration,
  GrowthTelegramSetup,
  IntegrationStatus,
} from "../../../lib/types";
import {
  Alert,
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CopyField,
  Field,
  Input,
  Modal,
  PageHeader,
  Select,
  Skeleton,
  Spinner,
} from "../../../components/ui";
import { Icon } from "../../../components/ui/icons";
import { useToast } from "../../../components/ui/Toast";

const GROUP_LABELS: Record<string, string> = {
  owner: "Where we reach you",
  channel: "How we reach customers",
  email: "Email sending",
  leads: "Lead data",
  inbound: "Inbound leads",
  store: "Your store",
  crm: "CRM",
};

// The order sections are read in: the owner's own alerts first, because nothing
// else is usable until the owner can be reached.
const GROUP_ORDER = ["owner", "channel", "email", "inbound", "leads", "store", "crm"];

const STATUS_TONE: Record<IntegrationStatus, "ok" | "warn" | "danger" | "neutral"> = {
  active: "ok",
  verifying: "warn",
  pending: "warn",
  error: "danger",
  not_connected: "neutral",
  planned: "neutral",
};

const STATUS_LABEL: Record<IntegrationStatus, string> = {
  active: "Connected",
  verifying: "Verifying",
  pending: "Pending",
  error: "Needs attention",
  not_connected: "Not connected",
  planned: "Planned",
};

/** What each connectable provider needs from the owner, in their words. */
const FORM_COPY: Record<string, { ref: string; refHint: string; secret: string; secretHint: string }> = {
  telegram: {
    ref: "Chat ID",
    refHint: "Message @userinfobot on Telegram to get yours. A group starts with a minus.",
    secret: "Bot token",
    secretHint: "From @BotFather, looks like 123456789:AA… Stored encrypted; never shown again.",
  },
  slack: {
    ref: "Channel name",
    refHint: "For your own reference only.",
    secret: "Incoming webhook URL",
    secretHint: "From Slack → Apps → Incoming Webhooks. Must be a hooks.slack.com URL.",
  },
  whatsapp: {
    ref: "Your own WhatsApp number",
    refHint:
      "With country code. Approvals and alerts go here, not to customers — customer WhatsApp connects on an agent's Channels screen.",
    secret: "",
    secretHint: "",
  },
  resend_domain: {
    ref: "Sending subdomain",
    refHint:
      "e.g. mail.yourdomain.com. We register it and show you the DNS records to add at your host — outbound mail starts once they resolve and you press Verify.",
    secret: "",
    secretHint: "",
  },
  apollo: {
    ref: "Label",
    refHint: "For your own reference only.",
    secret: "Apollo API key",
    secretHint: "From Apollo → Settings → Integrations → API. Credits are billed by Apollo to you.",
  },
  hunter: {
    ref: "Label",
    refHint: "For your own reference only.",
    secret: "Hunter API key",
    secretHint: "From Hunter → API. Used to find the email pattern behind a company domain.",
  },
  email_verifier: {
    ref: "Label",
    refHint: "For your own reference only.",
    secret: "Verifier API key",
    secretHint: "Checks an address will accept mail before we send. Only hard-bounces are dropped.",
  },
  website_form: {
    ref: "Label",
    refHint: "For your own reference. Your script tag and webhook URL appear below once connected.",
    secret: "",
    secretHint: "",
  },
  // JustDial has nothing to type in: connecting only records that the owner
  // intends to forward enquiry mail, and the setup — pointing JustDial at the
  // inbound address — happens in their seller panel. The modal says so, and
  // renders the address itself when the email channel has one provisioned.
  justdial: {
    ref: "",
    refHint: "",
    secret: "",
    secretHint: "",
  },
  indiamart: {
    ref: "Label",
    refHint: "For your own reference only.",
    secret: "IndiaMART API key",
    secretHint:
      "From your IndiaMART seller panel's API settings — the key named glusr-crt-key. Stored encrypted; never shown again.",
  },
};

/**
 * The providers that connect by OAuth instead of by typing a key in.
 *
 * They are deliberately absent from FORM_COPY above: there is no field for an
 * owner to fill in, and opening the credential modal for them would ask for a
 * secret that does not exist. Their card runs the authorize redirect instead —
 * the same shape as Shopify on the Customers screen.
 */
const OAUTH_CRM = new Set(["zoho", "hubspot"]);

/**
 * Zoho's data centres.
 *
 * Asked for before the redirect rather than discovered afterwards: the login,
 * token and API hosts all differ per DC, so an account in the EU sent at the US
 * host fails at the token exchange with an error nobody can act on. There is no
 * way to detect it from this side, so the only honest thing is to ask.
 */
const ZOHO_REGIONS: { value: string; label: string }[] = [
  { value: "us", label: "United States (zoho.com)" },
  { value: "eu", label: "Europe (zoho.eu)" },
  { value: "in", label: "India (zoho.in)" },
  { value: "au", label: "Australia (zoho.com.au)" },
  { value: "jp", label: "Japan (zoho.jp)" },
  { value: "ca", label: "Canada (zohocloud.ca)" },
  { value: "sa", label: "Saudi Arabia (zoho.sa)" },
  { value: "uk", label: "United Kingdom (zoho.uk)" },
];

export function GrowthIntegrations() {
  const toast = useToast();
  const [rows, setRows] = useState<GrowthIntegration[] | null>(null);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<GrowthIntegration | null>(null);
  const [externalRef, setExternalRef] = useState("");
  const [secret, setSecret] = useState("");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState("");
  // JustDial's setup step is "point JustDial at your inbound address", so the
  // modal wants the address itself. Empty until it is fetched (and stays empty
  // when the email channel has no mailbox yet — the modal then says where to
  // find it instead of showing one).
  const [inboundAddress, setInboundAddress] = useState("");
  // OAuth CRMs. The region is a Zoho-only input and lives here rather than on
  // the card so it survives the list reload a connect triggers.
  const [zohoRegion, setZohoRegion] = useState("us");
  // The Shopify install needs the shop's *.myshopify.com domain before the
  // redirect can be minted. Lives here for the same reason as zohoRegion.
  const [shopDomain, setShopDomain] = useState("");
  const [connecting, setConnecting] = useState("");
  const [verifyingDomain, setVerifyingDomain] = useState(false);
  // Aborts the JustDial address fetch when another modal opens or the page
  // unmounts — the same lifecycle discipline `load` follows, kept for a fetch
  // that lives inside a click handler instead of an effect.
  const inboundAbort = useRef<AbortController | null>(null);
  useEffect(() => () => inboundAbort.current?.abort(), []);

  function load() {
    const controller = new AbortController();
    growthApi
      .integrations(tenantHeaders(), controller.signal)
      .then(setRows)
      .catch((e) => {
        if (controller.signal.aborted) return;
        setError(e instanceof ApiError ? e.message : "could not load your connections");
      });
    return () => controller.abort();
  }

  useEffect(load, []);

  const grouped = useMemo(() => {
    const out = new Map<string, GrowthIntegration[]>();
    for (const row of rows ?? []) {
      const list = out.get(row.group) ?? [];
      list.push(row);
      out.set(row.group, list);
    }
    return out;
  }, [rows]);

  function open(row: GrowthIntegration) {
    setEditing(row);
    setExternalRef(row.external_ref);
    setSecret("");
    setFormError("");
    setInboundAddress("");
    // Fetched when the JustDial modal opens rather than on every card render —
    // no other provider needs it. A failure is not worth an alert: the modal
    // falls back to saying where the address lives.
    if (row.provider === "justdial") {
      inboundAbort.current?.abort();
      const controller = new AbortController();
      inboundAbort.current = controller;
      api
        .get<EmailChannelState>(
          "/v1/integrations/email",
          tenantHeaders(),
          controller.signal,
        )
        .then((state) => {
          if (controller.signal.aborted) return;
          setInboundAddress(state.channel?.address ?? "");
        })
        .catch(() => undefined);
    }
  }

  async function connect() {
    if (!editing) return;
    setSaving(true);
    setFormError("");
    try {
      await growthApi.connect(
        editing.provider,
        {
          external_ref: externalRef,
          // an empty secret means "leave the stored one alone", which is what
          // makes re-saving a chat id possible without re-typing a bot token
          ...(secret ? { secret } : {}),
          status: "active",
        },
        tenantHeaders(),
      );
      setEditing(null);
      toast.success(`${editing.label} connected.`);
      load();
    } catch (e) {
      setFormError(e instanceof ApiError ? e.message : "could not save this connection");
    } finally {
      setSaving(false);
    }
  }

  /**
   * OAuth connect for a CRM. Same shape as `startShopify()` on Customers: ask
   * the server for an authorize URL and open it, because the redirect carries a
   * state parameter the server minted and only it can validate.
   *
   * Opened with `noopener` — the CRM's login page must not get a handle on this
   * window. The list is not reloaded here: nothing is connected until the owner
   * finishes on the provider's side and the callback lands.
   */
  async function startCrm(row: GrowthIntegration) {
    setConnecting(row.provider);
    setError("");
    try {
      const res = await growthApi.crmConnect(
        row.provider,
        row.provider === "zoho" ? zohoRegion : undefined,
        tenantHeaders(),
      );
      // `window.open` runs after an await, so the browser no longer treats it as
      // user-initiated — a popup blocker kills it silently and returns null
      const win = window.open(res.authorize_url, "_blank", "noopener");
      if (!win) {
        setError(`could not open the ${row.label} approval window — allow popups and try again`);
        return;
      }
      toast.show(
        `Approve the app on ${row.label}, then come back — your leads start pushing within the hour.`,
      );
    } catch (e) {
      setError(e instanceof ApiError ? e.message : `could not start ${row.label} connect`);
    } finally {
      setConnecting("");
    }
  }

  /**
   * Start the Shopify public-app install. Same shape as `startCrm`: the
   * authorize URL carries a server-minted state, so it must be asked for
   * rather than built here. The shop domain is typed on the card; the server
   * rejects anything that is not *.myshopify.com with a message the card shows.
   */
  async function startShopify(row: GrowthIntegration) {
    if (!shopDomain.trim()) {
      setError("enter your shop domain first — e.g. your-store.myshopify.com");
      return;
    }
    setConnecting(row.provider);
    setError("");
    try {
      const res = await growthApi.shopifyConnect(shopDomain.trim(), tenantHeaders());
      const win = window.open(res.authorize_url, "_blank", "noopener");
      if (!win) {
        setError("could not open the Shopify approval window — allow popups and try again");
        return;
      }
      toast.show("Approve the app on Shopify, then come back — your orders start syncing.");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "could not start Shopify connect");
    } finally {
      setConnecting("");
    }
  }

  /**
   * Re-check the sending domain's DNS at Resend. The row's `pending` status
   * is the honest state — outbound mail refuses to run until Resend sees the
   * records — so this button, not a "connected" badge, is the finish line.
   */
  async function verifyDomain() {
    setVerifyingDomain(true);
    setError("");
    try {
      const row = await growthApi.verifyDomain(tenantHeaders());
      if (row.connected) {
        toast.success("Sending domain verified — outbound mail can start.");
      } else {
        toast.show("Records not visible yet — DNS can take a few minutes to propagate.");
      }
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "could not check the domain");
    } finally {
      setVerifyingDomain(false);
    }
  }

  async function disconnect(row: GrowthIntegration) {    try {
      await growthApi.connect(
        row.provider,
        {
          status: "not_connected",
          // provider mode's telegram row is keyed by the bound chat id; without
          // it the server would retire a row that does not exist and leave the
          // binding live. Per-tenant mode sends exactly what it always did
          ...(row.provider === "telegram" && row.provider_mode && row.external_ref
            ? { external_ref: row.external_ref }
            : {}),
        },
        tenantHeaders(),
      );
      toast.success(`${row.label} disconnected.`);
      load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "could not disconnect");
    }
  }

  if (error && !rows) {
    return (
      <div className="max-w-4xl">
        <PageHeader title="Connections" />
        <Alert>{error}</Alert>
      </div>
    );
  }

  if (!rows) {
    return (
      <div className="max-w-4xl">
        <PageHeader title="Connections" />
        <div className="grid sm:grid-cols-2 gap-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <Card key={i}>
              <CardBody className="flex flex-col gap-3">
                <Skeleton className="h-4 w-32" />
                <Skeleton className="h-3 w-48" />
                <Skeleton className="h-8 w-24" />
              </CardBody>
            </Card>
          ))}
        </div>
      </div>
    );
  }

  const copy = editing ? FORM_COPY[editing.provider] : undefined;

  return (
    <div className="max-w-4xl">
      <PageHeader
        title="Connections"
        description="What the agents can reach. Everything not built yet is listed too, with when it lands."
      />
      {error && (
        <div className="mb-4">
          <Alert>{error}</Alert>
        </div>
      )}

      {GROUP_ORDER.filter((g) => grouped.has(g)).map((group) => (
        <div key={group} className="mb-8">
          <h2 className="text-[13px] font-semibold uppercase tracking-wide text-faint mb-3">
            {GROUP_LABELS[group] ?? group}
          </h2>
          <div className="grid sm:grid-cols-2 gap-4">
            {(grouped.get(group) ?? []).map((row) => {
              const lastError = String(row.health?.["last_error"] ?? "");
              return (
                <Card key={row.provider}>
                  <CardHeader
                    title={row.label}
                    actions={
                      <Badge tone={STATUS_TONE[row.status]} dot>
                        {STATUS_LABEL[row.status]}
                      </Badge>
                    }
                  />
                  <CardBody className="flex flex-col gap-3">
                    {OAUTH_CRM.has(row.provider) ? (
                      <>
                        {row.external_ref && (
                          <div className="text-[13px] text-muted font-mono truncate">
                            {row.external_ref}
                          </div>
                        )}
                        {/* rendered whenever the provider left one, not only on
                            `error`: a refresh token that expired overnight is
                            exactly why nothing has reached the CRM since, and a
                            card that hides it turns that into a mystery */}
                        {lastError && <Alert>{lastError}</Alert>}
                        {row.provider === "zoho" && (
                          <Field
                            label="Data centre"
                            hint="Where your Zoho account lives. We cannot detect it, and the wrong one fails at sign-in."
                          >
                            <Select
                              value={zohoRegion}
                              onChange={(e) => setZohoRegion(e.target.value)}
                            >
                              {ZOHO_REGIONS.map((region) => (
                                <option key={region.value} value={region.value}>
                                  {region.label}
                                </option>
                              ))}
                            </Select>
                          </Field>
                        )}
                        <div className="flex flex-wrap gap-2">
                          <Button
                            size="sm"
                            variant="primary"
                            onClick={() => void startCrm(row)}
                            disabled={connecting === row.provider}
                          >
                            {connecting === row.provider ? (
                              <Spinner />
                            ) : (
                              <Icon.External size={15} />
                            )}
                            {row.connected ? "Reconnect" : "Connect"}
                          </Button>
                          {row.connected && (
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => void disconnect(row)}
                            >
                              Disconnect
                            </Button>
                          )}
                        </div>
                      </>
                    ) : (
                      <>
                        {row.external_ref && (
                          <div className="text-[13px] text-muted font-mono truncate">
                            {row.external_ref}
                          </div>
                        )}
                        {/* inbound sources report errors while still connected —
                            one failed JustDial poll or IndiaMART pull does not
                            disconnect the feed, and hiding the reason would
                            make the silence look like "no enquiries". But the
                            server carries health across a disconnect, so a
                            card that is no longer connected must not show the
                            dead feed's last complaint next to a "not
                            connected" badge */}
                        {(row.status === "error" ||
                          (row.group === "inbound" && row.connected)) &&
                          lastError && <Alert>{lastError}</Alert>}
                        {/* provider mode's telegram card has nothing to paste:
                            connecting is one message to the platform bot, and
                            the binding block below carries the instructions.
                            Shopify is OAuth like the CRMs — but with the shop
                            domain typed here, because the authorize URL is
                            per-shop and the credential modal's client id /
                            secret fields would be refused by the server. */}
                        {row.provider === "shopify" ? (
                          <>
                            <Field
                              label="Shop domain"
                              hint="your-store.myshopify.com — from Shopify admin → Settings → Domains."
                            >
                              <Input
                                value={shopDomain}
                                onChange={(e) => setShopDomain(e.target.value)}
                                placeholder="your-store.myshopify.com"
                              />
                            </Field>
                            <div className="flex gap-2">
                              <Button
                                size="sm"
                                variant="primary"
                                onClick={() => void startShopify(row)}
                                disabled={connecting === row.provider}
                              >
                                {connecting === row.provider ? (
                                  <Spinner />
                                ) : (
                                  <Icon.External size={15} />
                                )}
                                {row.connected ? "Reconnect" : "Connect"}
                              </Button>
                              {row.connected && (
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  onClick={() => void disconnect(row)}
                                >
                                  Disconnect
                                </Button>
                              )}
                            </div>
                          </>
                        ) : (
                          <div className="flex gap-2">
                            {!(row.provider === "telegram" && row.provider_mode) && (
                              <Button size="sm" onClick={() => open(row)}>
                                {row.connected ? "Edit" : "Connect"}
                              </Button>
                            )}
                            {row.connected && (
                              <Button
                                size="sm"
                                variant="ghost"
                                onClick={() => void disconnect(row)}
                              >
                                Disconnect
                              </Button>
                            )}
                          </div>
                        )}
                        {row.provider === "telegram" && row.provider_mode && (
                          <TelegramProviderCard />
                        )}
                        {row.provider === "resend_domain" &&
                          (row.status === "pending" || row.status === "verifying" || row.connected) && (
                            <div className="flex flex-col gap-2.5 rounded-lg border border-line bg-canvas p-3">
                              <p className="m-0 text-[13px] text-muted">
                                Add these records at your DNS host, then press Verify.
                                {row.health?.["spf"] ? " SPF ✓." : ""}
                                {row.health?.["dkim"] ? " DKIM ✓." : ""}
                              </p>
                              <div className="overflow-x-auto">
                                <table className="w-full text-[12px] font-mono">
                                  <tbody>
                                    {(Array.isArray(row.health?.["dns_records"])
                                      ? (row.health["dns_records"] as Record<string, string>[])
                                      : []
                                    ).map((rec, i) => (
                                      <tr key={i} className="border-t border-line">
                                        <td className="py-1 pr-3 text-faint">{rec.type ?? ""}</td>
                                        <td className="py-1 pr-3 truncate max-w-[12rem]">{rec.name ?? ""}</td>
                                        <td className="py-1 truncate max-w-[10rem]">{rec.record ?? ""}</td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>
                              <Button
                                size="sm"
                                variant="secondary"
                                onClick={() => void verifyDomain()}
                                disabled={verifyingDomain}
                              >
                                {verifyingDomain ? <Spinner /> : <Icon.Check size={15} />}
                                Verify
                              </Button>
                            </div>
                          )}
                        {row.provider === "indiamart" && row.connected && (
                          <IndiaMartBackfill />
                        )}
                      </>
                    )}
                  </CardBody>
                </Card>
              );
            })}
          </div>
        </div>
      ))}

      <TelegramSetup
        connected={rows.some((r) => r.provider === "telegram" && r.connected)}
        providerMode={rows.some((r) => r.provider === "telegram" && r.provider_mode)}
      />

      <WebsiteFormSetup connected={rows.some((r) => r.provider === "website_form" && r.connected)} />

      <Modal
        open={editing !== null}
        onClose={() => {
          if (!saving) setEditing(null);
        }}
        title={editing ? `Connect ${editing.label}` : ""}
        description={
          editing?.provider === "telegram" || editing?.provider === "slack"
            ? "Approvals and alerts are delivered here. Nothing customer-facing goes to this channel."
            : editing?.provider === "justdial"
              ? "In your JustDial seller panel, set the enquiry notification email to your inbound address — the same address your email channel already reads. Enquiries then land on the board as leads."
              : undefined
        }
        footer={
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setEditing(null)} disabled={saving}>
              Cancel
            </Button>
            <Button variant="primary" onClick={connect} disabled={saving}>
              {saving ? (
                <>
                  <Spinner /> Connecting…
                </>
              ) : (
                "Connect"
              )}
            </Button>
          </div>
        }
      >
        <div className="flex flex-col gap-4">
          {formError && <Alert>{formError}</Alert>}
          {copy && (
            <>
              {copy.ref && (
                <Field label={copy.ref} hint={copy.refHint}>
                  <Input
                    value={externalRef}
                    onChange={(e) => setExternalRef(e.target.value)}
                    autoFocus
                  />
                </Field>
              )}
              {copy.secret && (
                <Field
                  label={copy.secret}
                  hint={
                    editing?.connected
                      ? `${copy.secretHint} Leave blank to keep the stored one.`
                      : copy.secretHint
                  }
                >
                  <Input
                    type="password"
                    value={secret}
                    placeholder={editing?.connected ? "•••••••• (unchanged)" : ""}
                    onChange={(e) => setSecret(e.target.value)}
                  />
                </Field>
              )}
            </>
          )}
          {editing?.provider === "justdial" &&
            (inboundAddress ? (
              <Field
                label="Your inbound address"
                hint="Set this as the enquiry notification email in the JustDial seller panel. It is the address your email channel already reads."
              >
                <CopyField value={inboundAddress} />
              </Field>
            ) : (
              <div className="flex items-start gap-2 text-[13px] text-muted">
                <Icon.Info size={15} />
                <span>
                  Your inbound address is on the Email card of your agent&rsquo;s
                  Channels screen — the same address your email channel already
                  reads. Set that as the enquiry notification email in the JustDial
                  seller panel.
                </span>
              </div>
            ))}
          {/* nothing is stored for the no-field providers, so the encryption
              note would be a promise about credentials this form never takes */}
          {(copy?.ref || copy?.secret) && (
            <div className="flex items-start gap-2 text-[13px] text-muted">
              <Icon.Lock size={15} />
              <span>Credentials are encrypted before they are stored and are never shown again.</span>
            </div>
          )}
        </div>
      </Modal>
    </div>
  );
}

/**
 * The provider-mode binding block, on the Telegram card.
 *
 * Nothing to paste: the platform's own bot is the bot, and connecting is one
 * message. The command is offered ready-made for copy, and a fresh code is one
 * press — the old code dies the moment the new one is issued, which is why the
 * toast says so rather than implying both are live.
 */
function TelegramProviderCard() {
  const toast = useToast();
  const [setup, setSetup] = useState<GrowthTelegramSetup | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    growthApi
      .telegramSetup(tenantHeaders(), controller.signal)
      .then(setSetup)
      // as with the panels: a failure is not worth an alert on a screen whose
      // main job is the cards — the block simply does not render
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  async function refresh() {
    setRefreshing(true);
    try {
      setSetup(await growthApi.telegramSetup(tenantHeaders(), undefined, true));
      toast.success("New code issued — the previous one no longer works.");
    } catch {
      toast.show("could not issue a new code — try again");
    } finally {
      setRefreshing(false);
    }
  }

  if (!setup?.provider_mode) return null;

  return (
    <div className="flex flex-col gap-3">
      <div className="text-[13px] text-muted">
        Message{" "}
        {setup.bot_username ? (
          <a
            className="underline"
            href={`https://t.me/${setup.bot_username}`}
            target="_blank"
            rel="noopener noreferrer"
          >
            @{setup.bot_username}
          </a>
        ) : (
          "the platform bot"
        )}{" "}
        on Telegram and send:
      </div>
      <CopyField value={`/start ${setup.binding_code ?? ""}`} />
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="ghost" onClick={() => void refresh()} disabled={refreshing}>
          {refreshing ? <Spinner /> : "New code"}
        </Button>
        <span className="text-[12px] text-faint">
          Codes last a day and work once. A new one retires the old.
        </span>
      </div>
    </div>
  );
}

/**
 * The setWebhook URL, shown once the bot token is stored.
 *
 * Without this panel the bot is connected on our side and deaf on Telegram's:
 * a bot receives nothing until somebody calls setWebhook with this URL, and
 * nowhere else in the product says what that URL is.
 *
 * Telegram accepts one webhook URL per bot and no custom headers, so the key
 * has to travel in the query string — the same fallback the website form calls
 * a last resort, and here the only form there is. It is labelled as such.
 *
 * Provider mode swaps whose step this is: the platform owner's, once per
 * deployment, and it renders whether or not this tenant has bound a chat —
 * the tenant's own step (the /start message) is on the card above.
 */
function TelegramSetup({ connected, providerMode }: { connected: boolean; providerMode: boolean }) {
  const [setup, setSetup] = useState<GrowthTelegramSetup | null>(null);

  useEffect(() => {
    if (!connected && !providerMode) {
      setSetup(null);
      return undefined;
    }
    const controller = new AbortController();
    growthApi
      .telegramSetup(tenantHeaders(), controller.signal)
      .then(setSetup)
      // as with the form panel: a failure here is not worth an alert on a screen
      // whose main job is the cards above — the panel simply does not render
      .catch(() => undefined);
    return () => controller.abort();
  }, [connected, providerMode]);

  if (setup?.provider_mode) {
    return (
      <Card className="mb-8">
        <CardHeader
          title="The platform Telegram bot"
          description="One bot for every workspace on this deployment. This step is the platform owner's, done once — tenants never paste a webhook URL."
        />
        <CardBody className="flex flex-col gap-5">
          <Field
            label="setWebhook URL (platform owner, once)"
            hint="Point the platform bot at this URL via BotFather. Telegram allows one webhook per bot, so this replaces anything else registered on the same bot."
          >
            <CopyField value={setup.webhook_url_with_key} masked />
          </Field>
          <div className="flex items-start gap-2 text-[13px] text-muted">
            <Icon.Info size={15} />
            <span>
              The key is in the address, because Telegram sends no custom headers — so
              it will appear in server logs and browser history on its path. It names
              the platform bot, not your workspace.
            </span>
          </div>
        </CardBody>
      </Card>
    );
  }

  if (!connected || !setup?.connected) return null;

  return (
    <Card className="mb-8">
      <CardHeader
        title="Your Telegram bot"
        description="One last step on Telegram's side: point the bot at this URL and approvals arrive as messages you can answer."
      />
      <CardBody className="flex flex-col gap-5">
        <Field
          label="setWebhook URL"
          hint="Open this once in a browser, or send it to BotFather's setWebhook. Telegram allows one webhook per bot, so this replaces any other."
        >
          <CopyField value={setup.webhook_url_with_key} masked />
        </Field>
        <div className="flex items-start gap-2 text-[13px] text-muted">
          <Icon.Info size={15} />
          <span>
            The key is in the address, because Telegram sends no custom headers — so
            it will appear in server logs and browser history on its path. Disconnect
            and reconnect to rotate it.
          </span>
        </div>
        <details>
          <summary className="cursor-pointer text-[13px] text-muted">
            The address without the key
          </summary>
          <div className="mt-3 flex flex-col gap-5">
            <Field
              label="Endpoint"
              hint="For your own records. Telegram cannot use this on its own — it has no way to send the key."
            >
              <CopyField value={setup.webhook_url} />
            </Field>
            <Field label="Ingress key" hint="The same key the rest of your inbound traffic carries.">
              <CopyField value={setup.key} masked />
            </Field>
          </div>
        </details>
      </CardBody>
    </Card>
  );
}

/**
 * The two ways to wire a website form up, shown once it is connected.
 *
 * Both, not one: which affordance a tenant can actually use depends on software
 * we know nothing about. A script tag works on a page they control; a webhook URL
 * is the only option for a form builder that will post to a URL and nothing else.
 *
 * The key is derived from the tenant rather than stored, so there is nothing to
 * go stale — and disconnecting the integration is what revokes it, whatever
 * pages are still carrying the snippet.
 */
function WebsiteFormSetup({ connected }: { connected: boolean }) {
  const [setup, setSetup] = useState<GrowthFormSetup | null>(null);

  useEffect(() => {
    if (!connected) {
      setSetup(null);
      return undefined;
    }
    const controller = new AbortController();
    growthApi
      .formSetup(tenantHeaders(), controller.signal)
      .then(setSetup)
      // a failure here is not worth an alert on a screen whose main job is the
      // cards above: the panel simply does not render
      .catch(() => undefined);
    return () => controller.abort();
  }, [connected]);

  if (!connected || !setup?.connected) return null;

  return (
    <Card className="mb-8">
      <CardHeader
        title="Your website form"
        description="Either one works. Enquiries land as leads, get an acknowledgement within minutes, and ping you straight away."
      />
      <CardBody className="flex flex-col gap-5">
        <Field
          label="Script tag"
          hint="Paste into your page's HTML, then add data-growth-form to the form element. Your existing form keeps working exactly as it does now."
        >
          <CopyField value={setup.snippet} />
        </Field>
        <Field
          label="Webhook URL"
          hint="For a form builder that posts to a URL. Accepts JSON or a normal form post."
        >
          <CopyField value={setup.webhook_url} />
        </Field>
        <Field
          label={`Send your key in the "${setup.webhook_header}" header`}
          hint="Or as a field called key in the body. Either keeps the key out of server logs and browser history."
        >
          <CopyField value={setup.key} masked />
        </Field>
        {/* offered last and named a fallback on purpose: a key in a URL is
            written into every access log, proxy log and browser history on its
            path. Some builders accept nothing else, so it stays available */}
        <details>
          <summary className="cursor-pointer text-[13px] text-muted">
            My form builder can&rsquo;t send headers or add fields
          </summary>
          <div className="mt-3">
            <Field
              label="Fallback URL, key included"
              hint="Use this only if there is no other option — the key is in the address, so it will appear in server logs. Disconnect and reconnect to rotate it."
            >
              <CopyField value={setup.webhook_url_with_key} masked />
            </Field>
          </div>
        </details>
        <div className="flex items-start gap-2 text-[13px] text-muted">
          <Icon.Info size={15} />
          <span>
            Add a checkbox named <code className="font-mono">whatsapp_optin</code> if you
            want to acknowledge on WhatsApp — without a ticked opt-in we always
            acknowledge by email, because a form submission is not WhatsApp consent.
          </span>
        </div>
      </CardBody>
    </Card>
  );
}

/**
 * IndiaMART history pull, offered on the card once the feed is connected.
 *
 * Mail-forwarding sources have nothing to pull — JustDial gets no such control —
 * and the feed itself only reaches back a year, so 365 is the largest choice.
 * The request returns as soon as the job is queued, which is why the toast says
 * where the enquiries will appear rather than claiming they are already there.
 */
function IndiaMartBackfill() {
  const toast = useToast();
  const [days, setDays] = useState(30);
  const [pulling, setPulling] = useState(false);
  const [error, setError] = useState("");

  async function backfill() {
    setPulling(true);
    setError("");
    try {
      await growthApi.backfillInbound({ source: "indiamart", days }, tenantHeaders());
      toast.success("Backfill queued — new enquiries appear on the board as leads.");
    } catch (e) {
      // the server's message is the honest answer (e.g. why this source cannot
      // be pulled), so it is shown as-is rather than paraphrased
      setError(e instanceof ApiError ? e.message : "could not queue the backfill");
    } finally {
      setPulling(false);
    }
  }

  return (
    <div className="flex flex-col gap-2">
      {error && <Alert>{error}</Alert>}
      <div className="flex flex-wrap items-center gap-2">
        <Select
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          aria-label="How far back to pull enquiries"
        >
          {[7, 30, 90, 365].map((value) => (
            <option key={value} value={value}>
              Last {value} days
            </option>
          ))}
        </Select>
        <Button size="sm" onClick={() => void backfill()} disabled={pulling}>
          {pulling ? (
            <>
              <Spinner /> Backfilling…
            </>
          ) : (
            "Backfill"
          )}
        </Button>
      </div>
      <div className="text-[12px] text-faint">
        Pulls enquiries from the last {days} days of the feed onto the board as leads.
      </div>
    </div>
  );
}
