// Textarea + live parse + line-accurate error + format button (§5.2).
//
// Replaces the raw Textarea and try/catch that reported a bare "Invalid JSON"
// with no location: on a 40-line metadata blob that message tells you a character
// is wrong somewhere, which is the same as telling you nothing.
import { useEffect, useMemo, useRef, useState } from "react";
import { Button, Textarea, cx } from "./index";
import { Icon } from "./icons";

/**
 * Line and column for a JSON.parse failure.
 *
 * Engines disagree on the message: V8 emits both "at position 42" and, since
 * v20, "(line 3 column 5)"; Safari and Firefox word it differently again. So the
 * explicit line/column is used when present and the byte offset is converted
 * otherwise, and an unrecognised message degrades to "no location" rather than
 * to a wrong one.
 */
function locate(text: string, message: string): { line: number; column: number } | null {
  const explicit = /line (\d+) column (\d+)/i.exec(message);
  const el = explicit?.[1];
  const ec = explicit?.[2];
  if (el && ec) return { line: Number(el), column: Number(ec) };

  const offset = /position (\d+)/i.exec(message)?.[1];
  if (!offset) return null;
  const at = Math.min(Number(offset), text.length);
  const before = text.slice(0, at);
  const lastBreak = before.lastIndexOf("\n");
  return { line: before.split("\n").length, column: at - lastBreak };
}

export function JsonEditor({
  value, onChange, onValidChange, placeholder, minHeight = 180, readOnly,
}: {
  value: string;
  onChange: (text: string) => void;
  /** Receives the parsed value, or undefined while the text is invalid or empty. */
  onValidChange?: (parsed: unknown | undefined) => void;
  placeholder?: string;
  minHeight?: number;
  readOnly?: boolean;
}) {
  const [formatted, setFormatted] = useState(false);

  const parsed = useMemo(() => {
    if (value.trim() === "") return { ok: true as const, data: undefined as unknown };
    try {
      return { ok: true as const, data: JSON.parse(value) as unknown };
    } catch (e) {
      return { ok: false as const, message: e instanceof Error ? e.message : "Invalid JSON" };
    }
  }, [value]);

  // The callback is held in a ref so an inline arrow at the call site cannot turn
  // "tell me when the parse changed" into "tell me on every render", which is a
  // render loop the moment the consumer sets state from it.
  const notify = useRef(onValidChange);
  useEffect(() => { notify.current = onValidChange; }, [onValidChange]);
  useEffect(() => {
    notify.current?.(parsed.ok ? parsed.data : undefined);
  }, [parsed]);

  useEffect(() => {
    if (!formatted) return;
    const t = window.setTimeout(() => setFormatted(false), 1400);
    return () => window.clearTimeout(t);
  }, [formatted]);

  const where = parsed.ok ? null : locate(value, parsed.message);
  // Pre-rendered rather than computed in the handler: the union narrows here,
  // and a `canFormat` boolean would not carry that narrowing into the JSX.
  const formattable =
    !readOnly && parsed.ok && parsed.data !== undefined ? JSON.stringify(parsed.data, null, 2) : null;

  return (
    <div className="flex flex-col gap-2">
      <Textarea
        value={value}
        readOnly={readOnly}
        spellCheck={false}
        placeholder={placeholder}
        aria-invalid={parsed.ok ? undefined : true}
        mono
        resizable
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          // Tab inserts two spaces instead of leaving the field: this is a code
          // surface, and Escape then Tab still gets you out.
          if (e.key !== "Tab" || e.shiftKey || readOnly) return;
          e.preventDefault();
          const el = e.currentTarget;
          const { selectionStart: s, selectionEnd: end } = el;
          onChange(value.slice(0, s) + "  " + value.slice(end));
          window.requestAnimationFrame(() => { el.selectionStart = el.selectionEnd = s + 2; });
        }}
        style={{ minHeight }}
        className={cx(
          "resize-y leading-relaxed",
          parsed.ok ? "" : "border-danger focus:border-danger focus:ring-danger-soft",
        )}
      />

      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 text-xs" role="status" aria-live="polite">
          {parsed.ok ? (
            parsed.data === undefined ? (
              <span className="text-faint">Empty — nothing will be sent.</span>
            ) : (
              <span className="inline-flex items-center gap-1.5 font-medium text-ok">
                <Icon.Check size={13} /> Valid JSON
              </span>
            )
          ) : (
            <span className="font-medium text-danger">
              {where ? `Line ${where.line}, column ${where.column} — ` : ""}
              {parsed.message.replace(/^JSON\.parse: /, "").replace(/ in JSON at position \d+.*$/, "")}
            </span>
          )}
        </div>

        {formattable !== null && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => { onChange(formattable); setFormatted(true); }}
          >
            {formatted ? <Icon.Check size={14} /> : <Icon.Spark size={14} />}
            {formatted ? "Formatted" : "Format"}
          </Button>
        )}
      </div>
    </div>
  );
}
