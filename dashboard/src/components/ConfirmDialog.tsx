// Confirmation for destructive actions. The irreversible ones require typing the
// resource name, so a suspend or a revoke cannot happen on a stray click.
import { useState } from "react";
import { Alert, Button, Input, Field } from "./ui";
import { Icon } from "./ui/icons";

interface Props {
  title: string;
  message: string;
  confirmWord?: string; // require typing this to enable the button
  danger?: boolean;
  /** Server refusal from a failed attempt, shown in place rather than as a toast:
      the dialog stays open, so the reason belongs where the button is. */
  error?: React.ReactNode;
  /** In-flight on the network: disables Cancel and shows a spinner on Confirm. */
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({ title, message, confirmWord, danger, error, busy, onConfirm, onCancel }: Props) {
  const [typed, setTyped] = useState("");
  const armed = !confirmWord || typed === confirmWord;
  return (
    <div className="fixed inset-0 z-50 bg-black/40 grid place-items-center p-4" onClick={busy ? undefined : onCancel}>
      <div
        className="w-full max-w-md bg-surface border border-line rounded-xl shadow-pop p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start gap-3">
          {danger && (
            <span className="grid place-items-center w-9 h-9 rounded-lg bg-danger-soft text-danger shrink-0">
              <Icon.Alert size={18} />
            </span>
          )}
          <div className="min-w-0">
            <h3 className="text-[15px] font-semibold tracking-tight m-0">{title}</h3>
            <p className="text-sm text-muted mt-1 m-0">{message}</p>
          </div>
        </div>
        {confirmWord && (
          <div className="mt-4">
            <Field label={<>Type <b className="text-ink">{confirmWord}</b> to confirm</>}>
              <Input value={typed} onChange={(e) => setTyped(e.target.value)} autoFocus disabled={busy} />
            </Field>
          </div>
        )}
        {error && <div className="mt-4"><Alert>{error}</Alert></div>}
        <div className="flex items-center justify-end gap-2 mt-6">
          <Button variant="secondary" disabled={busy} onClick={onCancel}>Cancel</Button>
          <Button variant={danger ? "danger" : "primary"} loading={busy} disabled={!armed} onClick={onConfirm}>
            Confirm
          </Button>
        </div>
      </div>
    </div>
  );
}
