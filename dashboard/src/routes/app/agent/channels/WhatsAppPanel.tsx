// WhatsApp channel form — the WABA / phone / verify flow from
// `Integrations.tsx:688-829` rehoused into the Channels SlideOver (§4.7).
//
// §8.3 R5 names retyping this logic as the single highest-risk act in the plan,
// so it is MOVED, not rewritten: same endpoints, same request shapes, same state
// machine, same copy. Two things change, both forced by the new mount point:
//   * `agent_id` is this agent, so the "Answer with agent" Select is gone — the
//     panel is already agent-scoped and a picker here could only be wrong.
//   * the tenant-wide GET /v1/integrations/whatsapp stays in Channels.tsx (B17);
//     this panel takes the filtered slice and asks the parent to re-read.
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../../../lib/api";
import { tenantHeaders } from "../../../../lib/auth";
import type { WhatsAppEmbeddedSignupState, WhatsAppIntegration, WhatsAppVerifyResult } from "../../../../lib/types";
import { StatusBadge } from "../../../../components/StatusBadge";
import { ConfirmDialog } from "../../../../components/ConfirmDialog";
import {
  Alert, Badge, Button, EmptyState, Field, Input, Skeleton, Spinner, cx,
} from "../../../../components/ui";
import { Icon } from "../../../../components/ui/icons";
import { useToast } from "../../../../components/ui/Toast";

// WhatsApp message templates, as returned by
// /v1/integrations/whatsapp/{id}/templates.
interface WhatsAppTemplate {
  name: string;
  language: string;
  purpose?: string | null;
}

// --- Meta Embedded Signup v4 (WhatsApp Cloud API) ---------------------------
//
// One click, no tokens pasted. The flow:
//   1. load Meta's JS SDK, FB.init with the platform app id
//   2. FB.login(callback, { config_id, response_type: "code",
//      override_default_response_type: true, extras: { setup: {} } })
//   3. the exchangeable code arrives in the login callback (30s TTL)
//   4. the WABA + phone ids arrive in a WA_EMBEDDED_SIGNUP postMessage from Meta
//   5. both go to POST /v1/integrations/whatsapp/embedded-signup, which swaps the
//      code for a customer-scoped token and stores it — the manual path reused.
// Whether the flow can run at all is gated on GET /whatsapp/embedded-signup/state
// (`configured`): when the deployment has no real config_id, the panel shows the
// manual path instead of a flow that would fail at Meta.
const META_SDK_SRC = "https://connect.facebook.net/en_US/sdk.js";
const META_SIGNUP_ORIGINS = ["https://www.facebook.com", "https://facebook.com"];

interface MetaLoginOptions {
  config_id: string;
  response_type: string;
  override_default_response_type: boolean;
  extras: { setup: Record<string, unknown> };
}
interface MetaLoginResponse {
  status?: string;
  authResponse?: { code?: string };
}
interface MetaFB {
  init(opts: { appId: string; version: string; xfbml: boolean }): void;
  login(cb: (response: MetaLoginResponse) => void, opts: MetaLoginOptions): void;
}

interface EmbeddedSignupMessage {
  type?: string;
  event?: string;
  error_message?: string;
  error_code?: string;
  data?: {
    data?: {
      waba_id?: string;
      phone_number_id?: string;
      business_id?: string;
      display_number?: string;
    };
    current_step?: string;
  };
}

function isMetaSignupOrigin(origin: string): boolean {
  try {
    return META_SIGNUP_ORIGINS.includes(new URL(origin).origin);
  } catch {
    return false;
  }
}

function loadMetaSdk(appId: string, graphVersion: string): Promise<MetaFB> {
  return new Promise((resolve, reject) => {
    const w = window as unknown as { FB?: MetaFB; fbAsyncInit?: () => void };
    if (w.FB) {
      // already loaded (possibly by another panel): still init with OUR app id,
      // because a prior init may have used a different one
      w.FB.init({ appId, version: graphVersion || "v23.0", xfbml: true });
      resolve(w.FB);
      return;
    }
    let settled = false;
    const finish = (err?: unknown) => {
      if (settled) return;
      settled = true;
      if (err) reject(err);
      else if (w.FB) resolve(w.FB);
      else reject(new Error("meta_sdk_unavailable"));
    };
    // set before the script tag so the SDK calls it once it has loaded
    w.fbAsyncInit = () => {
      if (!w.FB) return finish(new Error("meta_sdk_unavailable"));
      w.FB.init({ appId, version: graphVersion || "v23.0", xfbml: true });
      finish();
    };
    const script = document.createElement("script");
    script.src = META_SDK_SRC;
    script.async = true;
    script.onerror = () => finish(new Error("meta_sdk_load_failed"));
    document.body.appendChild(script);
  });
}

export function WhatsAppPanel({ agentId, agentName, integrations, unattached, onChanged }: {
  agentId: string;
  agentName: string;
  /** Tenant-wide WhatsApp numbers, already narrowed to this agent by Channels.tsx. */
  integrations: readonly WhatsAppIntegration[];
  /** Numbers in this workspace pointed at some other agent. */
  unattached: number;
  onChanged: () => Promise<void> | void;
}) {
  const toast = useToast();
  const [error, setError] = useState("");

  // --- manual connect ---
  const [form, setForm] = useState({ waba_id: "", phone_number_id: "", display_number: "", access_token: "" });
  const [saving, setSaving] = useState(false);

  // --- embedded signup: one-click Meta flow, code + ids combined before submit ---
  const [esState, setEsState] = useState<WhatsAppEmbeddedSignupState | null>(null);
  const [esStatus, setEsStatus] = useState<"idle" | "launching" | "waiting" | "submitting">("idle");
  const [esInfo, setEsInfo] = useState("");
  // The code (login callback) and the WABA/phone ids (postMessage) arrive in
  // either order, so both are parked in refs and the submit waits for both.
  const embeddedCode = useRef<string>("");
  const embeddedIds = useRef<{ waba_id: string; phone_number_id: string; display_number: string | null } | null>(null);

  const completeEmbedded = useCallback(async () => {
    const code = embeddedCode.current;
    const ids = embeddedIds.current;
    if (!code || !ids) return;
    setEsStatus("submitting"); setEsInfo(""); setError("");
    try {
      await api.post("/v1/integrations/whatsapp/embedded-signup", {
        code,
        waba_id: ids.waba_id,
        phone_number_id: ids.phone_number_id,
        display_number: ids.display_number ?? null,
        agent_id: agentId,
      }, tenantHeaders());
      embeddedCode.current = "";
      embeddedIds.current = null;
      setEsStatus("idle");
      await onChanged();
      toast.success("WhatsApp connected", { description: `Messages are answered by ${agentName}.` });
    } catch (e) {
      setEsStatus("idle");
      setError(e instanceof ApiError ? e.message : "embedded signup failed");
    }
  }, [agentId, agentName, onChanged, toast]);

  const handleSignupMessage = useCallback((msg: EmbeddedSignupMessage) => {
    const ev = msg.event ?? "";
    if (ev === "ERROR") {
      setEsStatus("idle");
      setError(msg.error_message || "Meta could not complete Embedded Signup");
      return;
    }
    if (ev === "CANCEL") {
      setEsStatus("idle");
      setEsInfo("You closed the Meta window before finishing. Nothing was connected — try again whenever you like.");
      return;
    }
    const d = msg.data?.data;
    if (d?.waba_id && d?.phone_number_id) {
      embeddedIds.current = {
        waba_id: d.waba_id,
        phone_number_id: d.phone_number_id,
        display_number: d.display_number ?? null,
      };
      setEsInfo("Meta created your WhatsApp Business account. Connecting it…");
      void completeEmbedded();
    }
  }, [completeEmbedded]);

  useEffect(() => {
    let cancelled = false;
    api.get<WhatsAppEmbeddedSignupState>("/v1/integrations/whatsapp/embedded-signup/state", tenantHeaders())
      .then((s) => { if (!cancelled) setEsState(s); })
      .catch(() => { if (!cancelled) setEsState({ configured: false, app_id: "", config_id: "", graph_version: "" }); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    const handler = (event: MessageEvent) => {
      if (!isMetaSignupOrigin(event.origin)) return;
      const msg = event.data as EmbeddedSignupMessage | undefined;
      if (!msg || msg.type !== "WA_EMBEDDED_SIGNUP") return;
      handleSignupMessage(msg);
    };
    window.addEventListener("message", handler);
    return () => window.removeEventListener("message", handler);
  }, [handleSignupMessage]);

  const launchEmbedded = async () => {
    if (!esState?.configured) return;
    setEsStatus("launching"); setEsInfo(""); setError("");
    embeddedCode.current = "";
    embeddedIds.current = null;
    let FB: MetaFB;
    try {
      FB = await loadMetaSdk(esState.app_id, esState.graph_version);
    } catch {
      setEsStatus("idle");
      setError("Could not load Meta's sign-in script. Check your connection or a script blocker, then try again.");
      return;
    }
    setEsStatus("waiting");
    FB.login((response) => {
      const code = response?.authResponse?.code;
      if (code) {
        embeddedCode.current = code;
        setEsInfo("Connecting…");
        void completeEmbedded();
      } else {
        // popup closed without a code — the CANCEL message usually arrives too
        setEsStatus("idle");
        setEsInfo("The Meta window closed without finishing. If it never opened, allow popups for this site and try again.");
      }
    }, {
      config_id: esState.config_id,
      response_type: "code",
      override_default_response_type: true,
      extras: { setup: {} },
    });
  };

  // per-integration connection handshake
  const [waTesting, setWaTesting] = useState<string | null>(null);
  const [waResults, setWaResults] = useState<Record<string, WhatsAppVerifyResult>>({});

  // --- templates, loaded per integration on expand ---
  const [tplOpen, setTplOpen] = useState<string | null>(null);
  const [templates, setTemplates] = useState<WhatsAppTemplate[]>([]);
  const [tplLoading, setTplLoading] = useState(false);
  const [tplSaving, setTplSaving] = useState(false);
  const [tplSaved, setTplSaved] = useState(false);

  const [disconnect, setDisconnect] = useState<WhatsAppIntegration | null>(null);
  const [disconnecting, setDisconnecting] = useState(false);

  async function connectWhatsApp() {
    if (!form.waba_id || !form.phone_number_id) { setError("WABA ID and phone number ID are required."); return; }
    setSaving(true); setError("");
    try {
      await api.put("/v1/integrations/whatsapp", {
        waba_id: form.waba_id,
        phone_number_id: form.phone_number_id,
        display_number: form.display_number || null,
        access_token: form.access_token || null,
        agent_id: agentId,
      }, tenantHeaders());
      setForm({ waba_id: "", phone_number_id: "", display_number: "", access_token: "" });
      await onChanged();
      toast.success("WhatsApp number connected", { description: `Messages are answered by ${agentName}.` });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "connect failed");
    } finally {
      setSaving(false);
    }
  }

  async function removeWhatsApp(id: string) {
    setDisconnecting(true);
    try {
      await api.del(`/v1/integrations/whatsapp/${id}`);
      setDisconnect(null);
      await onChanged();
      toast.success("WhatsApp disconnected");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "disconnect failed");
    } finally {
      setDisconnecting(false);
    }
  }

  // WhatsApp connection handshake: confirm Meta still accepts the stored token and
  // reflect the number's display name / quality rating back to the tenant
  async function verifyWhatsApp(id: string) {
    setWaTesting(id); setError("");
    try {
      const res = await api.post<WhatsAppVerifyResult>(`/v1/integrations/whatsapp/${id}/verify`, undefined, tenantHeaders());
      setWaResults((r) => ({ ...r, [id]: res }));
    } catch (e) {
      setWaResults((r) => ({
        ...r,
        [id]: {
          ok: false,
          display_number: null,
          verified_name: null,
          quality_rating: null,
          subscribed: false,
          message: e instanceof ApiError ? e.message : "verification failed",
        },
      }));
    } finally {
      setWaTesting(null);
    }
  }

  async function toggleTemplates(id: string) {
    if (tplOpen === id) { setTplOpen(null); return; }
    setTplOpen(id); setTemplates([]); setTplSaved(false); setError("");
    setTplLoading(true);
    try {
      const list = await api.get<WhatsAppTemplate[]>(`/v1/integrations/whatsapp/${id}/templates`, tenantHeaders());
      setTemplates(list.map((t) => ({ name: t.name, language: t.language, purpose: t.purpose ?? "" })));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "could not load templates");
    } finally {
      setTplLoading(false);
    }
  }

  function editTemplate(i: number, patch: Partial<WhatsAppTemplate>) {
    setTemplates((ts) => ts.map((t, idx) => (idx === i ? { ...t, ...patch } : t)));
    setTplSaved(false);
  }

  async function saveTemplates(id: string) {
    setTplSaving(true); setError(""); setTplSaved(false);
    try {
      const clean = templates
        .filter((t) => t.name.trim() && t.language.trim())
        .map((t) => ({ name: t.name.trim(), language: t.language.trim(), purpose: (t.purpose || "").trim() || null }));
      await api.put(`/v1/integrations/whatsapp/${id}/templates`, { templates: clean }, tenantHeaders());
      setTplSaved(true);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "could not save templates");
    } finally {
      setTplSaving(false);
    }
  }

  return (
    <div className="flex flex-col gap-5">
      {error && <Alert tone="danger">{error}</Alert>}

      <p className="m-0 text-[13px] text-muted">
        Connect a WhatsApp Business number and manage its message templates. Messages to a number
        connected here are answered by <span className="text-ink">{agentName}</span>.
      </p>

      {integrations.length === 0 ? (
        <EmptyState
          icon={<Icon.Chat size={22} />}
          title="No WhatsApp number on this agent"
          description="Connect one below, either through Meta's Embedded Signup or by pasting the credentials from Meta Business."
        />
      ) : (
        <div className="flex flex-col gap-3">
          {integrations.map((w) => {
            const res = waResults[w.id];
            const testing = waTesting === w.id;
            return (
              <div key={w.id} className="overflow-hidden rounded-lg border border-line">
                <div className="flex flex-wrap items-center gap-2 border-b border-line bg-canvas px-4 py-3">
                  <span className="text-[13px] font-semibold text-ink">{w.display_number ?? "Number not set"}</span>
                  <StatusBadge status={w.status} />
                  {w.token_configured ? <Badge tone="ok">token set</Badge> : <Badge tone="warn">token missing</Badge>}
                </div>
                <div className="flex flex-col gap-3 p-4">
                  <div className="grid grid-cols-2 gap-3 max-sm:grid-cols-1">
                    <Field label="Phone number ID">
                      <code className="block truncate font-mono text-[13px] text-muted">{w.phone_number_id}</code>
                    </Field>
                    <Field label="WABA ID">
                      <code className="block truncate font-mono text-[13px] text-muted">{w.waba_id}</code>
                    </Field>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Button size="sm" variant="secondary" onClick={() => void verifyWhatsApp(w.id)} disabled={testing}>
                      {testing ? <><Spinner /> Verifying…</> : "Verify connection"}
                    </Button>
                    <Button size="sm" variant="secondary" onClick={() => void toggleTemplates(w.id)}>
                      {tplOpen === w.id ? "Hide templates" : "Templates"}
                    </Button>
                    <Button size="sm" variant="danger" className="ml-auto" onClick={() => setDisconnect(w)}>
                      Disconnect
                    </Button>
                  </div>

                  {res && (
                    <div className={cx(
                      "flex items-start gap-3 rounded-lg border px-3.5 py-2.5",
                      res.ok ? "bg-ok-soft border-ok/30 text-ok" : "bg-danger-soft border-danger/30 text-danger",
                    )}>
                      <span className="mt-0.5 shrink-0">{res.ok ? <Icon.Check size={16} /> : <Icon.Alert size={16} />}</span>
                      <div className="min-w-0 text-[13px]">
                        {res.ok ? (
                          <>
                            <span className="font-semibold">Connection healthy.</span>{" "}
                            {res.display_number && <>Number {res.display_number}. </>}
                            {res.verified_name && <>Verified as “{res.verified_name}”. </>}
                            {res.quality_rating && <>Quality: {res.quality_rating}.</>}
                          </>
                        ) : (
                          <span>{res.message || "Verification failed"}</span>
                        )}
                      </div>
                    </div>
                  )}

                  {tplOpen === w.id && (
                    <div className="flex flex-col gap-3 rounded-lg border border-line bg-canvas p-4">
                      <div className="flex items-center justify-between gap-2">
                        <h4 className="m-0 text-sm font-semibold">Message templates</h4>
                        <div className="flex items-center gap-2">
                          {tplSaved && <Badge tone="ok" dot>Saved</Badge>}
                          <Button
                            size="sm"
                            variant="secondary"
                            onClick={() => setTemplates((ts) => [...ts, { name: "", language: "en", purpose: "" }])}
                          >
                            Add template
                          </Button>
                          <Button size="sm" variant="primary" onClick={() => void saveTemplates(w.id)} disabled={tplSaving}>
                            {tplSaving ? "Saving…" : "Save templates"}
                          </Button>
                        </div>
                      </div>
                      <p className="m-0 text-[13px] text-muted">
                        Templates approved in Meta Business, used to reopen a conversation outside the
                        24-hour window.
                      </p>
                      {tplLoading ? (
                        <Skeleton className="h-24 w-full" />
                      ) : templates.length === 0 ? (
                        <EmptyState title="No templates yet." />
                      ) : templates.map((t, i) => (
                        <div key={i} className="flex gap-2">
                          <Input placeholder="template_name" value={t.name} onChange={(e) => editTemplate(i, { name: e.target.value })} className="flex-[2]" />
                          <Input placeholder="en" value={t.language} onChange={(e) => editTemplate(i, { language: e.target.value })} className="w-24" />
                          <Input placeholder="purpose (optional)" value={t.purpose ?? ""} onChange={(e) => editTemplate(i, { purpose: e.target.value })} className="flex-[2]" />
                          <Button size="sm" variant="danger" onClick={() => setTemplates((ts) => ts.filter((_, idx) => idx !== i))}>Remove</Button>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      <div className="flex flex-col gap-3 rounded-lg border border-line p-4">
        <div>
          <h4 className="m-0 text-sm font-semibold">Connect in one click</h4>
          {esState === null ? (
            <p className="m-0 mt-1 text-[13px] text-muted">Checking whether the one-click flow is available…</p>
          ) : esState.configured ? (
            <p className="m-0 mt-1 text-[13px] text-muted">
              A Meta window opens: log in, pick your business and WhatsApp number, and this agent is
              connected automatically. No tokens to copy.
            </p>
          ) : (
            <p className="m-0 mt-1 text-[13px] text-muted">
              One-click signup isn't configured on this deployment yet. Use{" "}
              <span className="text-ink">Connect manually</span> below — it needs nothing extra.
            </p>
          )}
        </div>
        {esState?.configured && (
          <>
            <div>
              <Button variant="primary" onClick={() => void launchEmbedded()} disabled={esStatus !== "idle"}>
                {esStatus === "launching" || esStatus === "waiting" || esStatus === "submitting"
                  ? <><Spinner /> {esStatus === "submitting" ? "Connecting…" : "Opening Meta…"}</>
                  : "Connect with Meta"}
              </Button>
            </div>
            {esInfo !== "" && <p className="m-0 text-[13px] text-muted">{esInfo}</p>}
          </>
        )}
      </div>

      <div className="flex flex-col gap-3 rounded-lg border border-line p-4">
        <div>
          <h4 className="m-0 text-sm font-semibold">Connect manually</h4>
          <p className="m-0 mt-1 text-[13px] text-muted">
            From Meta Business: the WABA ID, the phone number ID, and a permanent access token. The
            token is encrypted and never shown again.
          </p>
        </div>
        <div className="grid grid-cols-2 gap-3 max-sm:grid-cols-1">
          <Field label="WABA ID"><Input value={form.waba_id} onChange={(e) => setForm({ ...form, waba_id: e.target.value })} /></Field>
          <Field label="Phone number ID"><Input value={form.phone_number_id} onChange={(e) => setForm({ ...form, phone_number_id: e.target.value })} /></Field>
          <Field label="Display number (optional)"><Input value={form.display_number} onChange={(e) => setForm({ ...form, display_number: e.target.value })} /></Field>
        </div>
        <Field label="Access token">
          <Input type="password" value={form.access_token} onChange={(e) => setForm({ ...form, access_token: e.target.value })} />
        </Field>
        <div>
          <Button variant="primary" onClick={() => void connectWhatsApp()} disabled={saving}>
            {saving ? <><Spinner /> Connecting…</> : "Connect"}
          </Button>
        </div>
      </div>

      {unattached > 0 && (
        <p className="m-0 text-[13px] text-muted">
          {unattached === 1
            ? "One other WhatsApp number in this workspace answers with a different agent."
            : `${unattached} other WhatsApp numbers in this workspace answer with a different agent.`}{" "}
          They are managed from those agents.
        </p>
      )}

      {disconnect && (
        <ConfirmDialog
          title="Disconnect WhatsApp"
          message={`Stop answering messages on ${disconnect.display_number ?? disconnect.phone_number_id}.`}
          danger
          busy={disconnecting}
          onConfirm={() => void removeWhatsApp(disconnect.id)}
          onCancel={() => { if (!disconnecting) setDisconnect(null); }}
        />
      )}
    </div>
  );
}
