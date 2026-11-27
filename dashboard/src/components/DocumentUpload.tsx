// Drag-drop with per-file progress and inline rejection reasons. A file the gate
// refuses says why (too large, wrong type, quota) instead of failing silently —
// the reason comes straight from the ApiError code the upload endpoint returns.
import { useRef, useState } from "react";
import { api, ApiError, bustGetCache } from "../lib/api";
import { tenantHeaders } from "../lib/auth";
import type { DocumentOut, UploadResponse } from "../lib/types";
import { Icon } from "./ui/icons";
import { cx, Spinner } from "./ui";

interface Props { onUploaded: (doc: DocumentOut, duplicate: boolean) => void; }

interface Pending { name: string; state: "uploading" | "done" | "error"; message?: string; }

export function DocumentUpload({ onUploaded }: Props) {
  const [drag, setDrag] = useState(false);
  const [pending, setPending] = useState<Pending[]>([]);
  const input = useRef<HTMLInputElement>(null);

  async function send(files: FileList) {
    for (const file of Array.from(files)) {
      setPending((p) => [...p, { name: file.name, state: "uploading" }]);
      try {
        // FormData path in api.upload; X-Tenant carried via the header helper is
        // not needed here because upload() uses the default token source
        const res = await uploadWithTenant(file);
        onUploaded(res.document, res.duplicate);
        setPending((p) => p.map((x) => x.name === file.name ? { ...x, state: "done" } : x));
      } catch (e) {
        const message = e instanceof ApiError ? e.message : "upload failed";
        setPending((p) => p.map((x) => x.name === file.name ? { ...x, state: "error", message } : x));
      }
    }
  }

  return (
    <div>
      <div
        role="button"
        tabIndex={0}
        aria-label="Upload documents"
        className={cx(
          "group flex flex-col items-center justify-center gap-3 text-center rounded-2xl border-2 border-dashed px-6 py-12 cursor-pointer transition-all outline-none",
          "focus-visible:ring-4 focus-visible:ring-brand-soft focus-visible:border-brand",
          drag
            ? "bg-brand-soft border-brand text-brand scale-[1.005]"
            : "border-line-strong bg-canvas text-muted hover:border-brand hover:bg-brand-soft",
        )}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => { e.preventDefault(); setDrag(false); send(e.dataTransfer.files); }}
        onClick={() => input.current?.click()}
        onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.current?.click(); } }}
      >
        <span className={cx(
          "grid place-items-center w-14 h-14 rounded-2xl transition-colors",
          drag ? "bg-brand text-brand-fg" : "bg-surface border border-line text-faint group-hover:text-brand group-hover:border-brand/40",
        )}>
          <Icon.Upload size={24} />
        </span>
        <div>
          <div className="text-sm font-semibold text-ink">
            Drop files here, or <span className="text-brand">click to browse</span>
          </div>
          <div className="text-xs text-faint mt-1">PDF, DOCX, XLSX, PPTX, HTML, and plain text.</div>
        </div>
        <input ref={input} type="file" multiple hidden onChange={(e) => e.target.files && send(e.target.files)} />
      </div>
      {pending.length > 0 && (
        <div className="mt-3 flex flex-col gap-1.5">
          {pending.map((p, i) => (
            <div key={i} className={cx(
              "flex items-center justify-between gap-3 rounded-md border px-3 py-2 text-sm",
              p.state === "error" ? "border-danger/30 bg-danger-soft" : "border-line bg-surface",
            )}>
              <span className="flex items-center gap-2 min-w-0">
                {p.state === "uploading" && <Spinner className="text-brand shrink-0" />}
                {p.state === "done" && <Icon.Check size={16} className="text-ok shrink-0" />}
                {p.state === "error" && <Icon.Alert size={16} className="text-danger shrink-0" />}
                <span className="truncate text-ink">{p.name}</span>
              </span>
              <span className={cx("shrink-0 text-xs font-medium", p.state === "error" ? "text-danger" : "text-muted")}>
                {p.state === "error" ? p.message : p.state === "uploading" ? "Uploading…" : "Added"}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// upload() in api.ts does not thread the tenant header, so re-issue with it here
async function uploadWithTenant(file: File): Promise<UploadResponse> {
  const form = new FormData();
  form.append("file", file);
  const { tenantSession } = await import("../lib/auth");
  const token = tenantSession()?.token;
  const res = await fetch("/v1/documents", {
    method: "POST",
    headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...tenantHeaders() },
    body: form,
  });
  const payload = await res.json();
  if (!res.ok) throw new ApiError(payload?.error?.code ?? "unknown", payload?.error?.message ?? "upload failed", res.status);
  // raw fetch above bypasses the client's mutation hooks — bust manually so a
  // revisited list screen does not serve the pre-upload snapshot
  bustGetCache();
  return payload as UploadResponse;
}

// re-export so screens can use the shared client for non-upload calls
export { api };
