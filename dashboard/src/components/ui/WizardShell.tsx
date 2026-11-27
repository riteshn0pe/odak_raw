// The create flows (§5.2): stepper, body, footer nav, with per-step validation
// gating Continue.
//
// The parent owns `current` because the wizards need to deep-link a step and to
// skip steps based on earlier answers; a self-managing index would put that
// decision inside this component where the callers cannot reach it.
import { useState, type ReactNode } from "react";
import { Button, cx } from "./index";
import { Icon } from "./icons";

export interface WizardStep {
  id: string;
  title: string;
  optional?: boolean;
  render: (ctx: { next: () => void; back: () => void }) => ReactNode;
  /** Error message shown under the footer. Returning null means "may continue". */
  validate?: () => string | null;
}

export function WizardShell({
  steps, current, onStepChange, onFinish, onCancel, finishLabel = "Finish", aside,
}: {
  steps: WizardStep[];
  current: number;
  onStepChange: (i: number) => void;
  onFinish: () => void | Promise<void>;
  onCancel: () => void;
  finishLabel?: string;
  aside?: ReactNode;
}) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const index = Math.min(Math.max(current, 0), Math.max(steps.length - 1, 0));
  const step = steps[index];
  const last = index >= steps.length - 1;

  const back = () => {
    setError(null);
    if (index === 0) onCancel();
    else onStepChange(index - 1);
  };

  const advance = () => {
    // A step's own validate() is the gate for both the footer button and the
    // `next` handed to its body, so a form that submits on Enter cannot bypass it.
    const failure = step?.validate?.() ?? null;
    setError(failure);
    if (failure) return false;
    if (!last) onStepChange(index + 1);
    return true;
  };

  const next = () => { advance(); };

  const finish = async () => {
    if (!advance()) return;
    setBusy(true);
    try {
      await onFinish();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong. Nothing was saved.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-auto w-full max-w-[1100px]">
      <div className="mb-6 flex items-center justify-between gap-4">
        <div>
          <div className="text-[11px] font-semibold uppercase tracking-wide text-faint">
            Step {index + 1} of {steps.length}
          </div>
          <h1 className="m-0 mt-1 text-2xl font-bold tracking-tight">
            {step?.title ?? ""}
            {step?.optional && <span className="ml-2 align-middle text-[13px] font-medium text-faint">Optional</span>}
          </h1>
        </div>
        {/* Decorative: the "Step N of M" line above carries the same information
            for assistive tech, and a list of 6 unlabelled dots does not. */}
        <div className="flex items-center gap-1.5" aria-hidden>
          {steps.map((s, i) => (
            <span
              key={s.id}
              className={cx(
                "h-1.5 w-1.5 rounded-full transition-colors",
                i <= index ? "bg-ink" : "bg-line-strong",
              )}
            />
          ))}
        </div>
      </div>

      <div className={cx("grid gap-6", aside != null && "lg:grid-cols-[minmax(0,1fr)_320px]")}>
        <div className="min-w-0">
          {step?.render({ next, back })}

          <div className="mt-6 flex items-center justify-between gap-3 border-t border-line pt-5">
            <Button variant="ghost" onClick={onCancel} disabled={busy}>Cancel</Button>
            <div className="flex items-center gap-2">
              {index > 0 && (
                <Button variant="secondary" onClick={back} disabled={busy}>
                  <Icon.Chevron size={14} className="rotate-90" /> Back
                </Button>
              )}
              {last ? (
                <Button variant="brand" onClick={finish} loading={busy}>{finishLabel}</Button>
              ) : (
                <Button variant="primary" onClick={next} disabled={busy}>
                  Continue <Icon.Chevron size={14} className="-rotate-90" />
                </Button>
              )}
            </div>
          </div>

          {error && (
            <p role="alert" className="m-0 mt-3 text-right text-[13px] font-medium text-danger">
              {error}
            </p>
          )}
        </div>

        {aside && <div className="min-w-0">{aside}</div>}
      </div>
    </div>
  );
}
