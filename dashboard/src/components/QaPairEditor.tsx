// The Q&A editor. One row per pair, reorderable, and the same component serves
// both call sites: an unsaved draft inside AddSourceModal (pure controlled
// value/onChange, no network) and the saved review screen, where each row
// PUTs/DELETEs itself. `mode` discriminates, defaulting to the draft.
//
// The important UI claim: a pair holds *every phrasing* of one question
// (qa_pairs.questions — "each becomes its own embedded chunk, all sharing one
// parent_id whose parent_text is the answer"). So adding a phrasing is the
// normal act, not an advanced one: Enter in a question field opens the next.
//
// Reordering is SortableList (Alt+Arrow keyboard path included) and writes
// qa_pairs.ordinal in saved mode.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../lib/api";
import { tenantHeaders } from "../lib/auth";
import { relativeTime } from "../lib/format";
import type { QaPairActivity, QaPairIn, QaPairOut } from "../lib/types";
import { LineChart } from "./Chart";
import { ConfirmDialog } from "./ConfirmDialog";
import { StatusBadge } from "./StatusBadge";
import { Alert, Badge, Button, Field, Input, Spinner, Textarea, cx } from "./ui";
import { Icon } from "./ui/icons";
import { SortableList } from "./ui/SortableList";

/* --------------------------------------------------------------- server caps */

// qa_pairs: questions 1..20, answer 8000, title 200. Enforced here so a save
// never comes back 422 for something the field could have refused.
export const QA_MAX_QUESTIONS = 20;
export const QA_ANSWER_MAX = 8000;
export const QA_TITLE_MAX = 200;

export function qaPairProblem(pair: QaPairIn): string | null {
  const questions = pair.questions.map((q) => q.trim()).filter((q) => q !== "");
  if (questions.length === 0) return "Add at least one question.";
  if (questions.length > QA_MAX_QUESTIONS) return `No more than ${QA_MAX_QUESTIONS} phrasings per pair.`;
  if (pair.answer.trim() === "") return "Add an answer.";
  if (pair.answer.length > QA_ANSWER_MAX) return `The answer is longer than ${QA_ANSWER_MAX} characters.`;
  if ((pair.title ?? "").length > QA_TITLE_MAX) return `The title is longer than ${QA_TITLE_MAX} characters.`;
  return null;
}

/** First problem across a draft set, prefixed with which pair it is in. */
export function qaPairsProblem(pairs: QaPairIn[]): string | null {
  if (pairs.length === 0) return "Add at least one question and answer.";
  for (let i = 0; i < pairs.length; i += 1) {
    const pair = pairs[i];
    if (pair === undefined) continue;
    const problem = qaPairProblem(pair);
    if (problem !== null) return `Pair ${i + 1}: ${problem}`;
  }
  return null;
}

export function emptyQaPair(): QaPairIn {
  return { title: "", questions: [""], answer: "" };
}

/* ----------------------------------------------------------------- row model */

interface QaRow {
  /** Stable across reorders — the saved id, or a generated draft key. */
  key: string;
  savedId: string | null;
  title: string;
  questions: string[];
  answer: string;
  status: string | null;
  error: string | null;
  timesAsked: number;
  lastAsked: string | null;
  dirty: boolean;
}

function rowToPairIn(row: QaRow): QaPairIn {
  return {
    title: row.title.trim() === "" ? null : row.title.trim(),
    questions: row.questions.map((q) => q.trim()).filter((q) => q !== ""),
    answer: row.answer,
  };
}

function rowFromOut(out: QaPairOut): QaRow {
  return {
    key: out.id,
    savedId: out.id,
    title: out.title,
    questions: out.questions.length > 0 ? out.questions : [""],
    answer: out.answer,
    status: out.status,
    error: out.error,
    timesAsked: out.times_asked,
    lastAsked: out.last_asked,
    dirty: false,
  };
}

function rowFromIn(pair: QaPairIn, key: string): QaRow {
  return {
    key,
    savedId: null,
    title: pair.title ?? "",
    questions: pair.questions.length > 0 ? pair.questions : [""],
    answer: pair.answer,
    status: null,
    error: null,
    timesAsked: 0,
    lastAsked: null,
    dirty: false,
  };
}

/* ------------------------------------------------------------------- props */

type Props =
  | {
      /** Draft: nothing is written until the parent submits the whole set. */
      mode?: "draft";
      value: QaPairIn[];
      onChange: (next: QaPairIn[]) => void;
      disabled?: boolean;
      className?: string;
    }
  | {
      /** Saved: every row owns its own PUT / DELETE against a real source. */
      mode: "saved";
      sourceId: string;
      pairs: QaPairOut[];
      /** Called after any successful write so the screen can refetch. */
      onChanged?: () => void;
      disabled?: boolean;
      className?: string;
    };

let draftSeq = 0;
function nextDraftKey(): string {
  draftSeq += 1;
  return `qa-draft-${draftSeq}`;
}

/* --------------------------------------------------------------- component */

export function QaPairEditor(props: Props) {
  const saved = props.mode === "saved";
  const disabled = props.disabled === true;

  // Draft keys live in a ref and travel with the rows through every mutation,
  // so a reorder does not remount a row and steal focus from the drag handle.
  const draftKeys = useRef<string[]>([]);
  const [savedRows, setSavedRows] = useState<QaRow[]>([]);
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [rowError, setRowError] = useState<Record<string, string>>({});
  const [confirming, setConfirming] = useState<QaRow | null>(null);

  const serverPairs = saved ? props.pairs : null;
  useEffect(() => {
    if (serverPairs === null) return;
    // Refresh from the server, but never clobber a row the tenant is editing
    // or a new row they have not saved yet.
    setSavedRows((prev) => {
      const held = new Map(prev.filter((r) => r.dirty && r.savedId !== null).map((r) => [r.savedId as string, r]));
      const unsaved = prev.filter((r) => r.savedId === null);
      return [...serverPairs.map((p) => held.get(p.id) ?? rowFromOut(p)), ...unsaved];
    });
  }, [serverPairs]);

  const draftValue = saved ? null : props.value;
  const rows = useMemo<QaRow[]>(() => {
    if (draftValue === null) return savedRows;
    while (draftKeys.current.length < draftValue.length) draftKeys.current.push(nextDraftKey());
    if (draftKeys.current.length > draftValue.length) draftKeys.current.length = draftValue.length;
    return draftValue.map((pair, i) => rowFromIn(pair, draftKeys.current[i] ?? nextDraftKey()));
  }, [draftValue, savedRows]);

  // One writer for both modes: draft pushes back through onChange, saved keeps
  // the row local and marks it dirty until its Save lands.
  const commit = useCallback(
    (next: QaRow[]) => {
      if (draftValue === null) {
        setSavedRows(next);
        return;
      }
      draftKeys.current = next.map((r) => r.key);
      if (props.mode !== "saved") props.onChange(next.map(rowToPairIn));
    },
    // props is a union; the draft branch is the only one that reads onChange
    [draftValue, props],
  );

  const patch = useCallback(
    (key: string, change: Partial<QaRow>) => {
      commit(rows.map((r) => (r.key === key ? { ...r, ...change, dirty: true } : r)));
    },
    [commit, rows],
  );

  const addPair = useCallback(() => {
    const key = nextDraftKey();
    commit([...rows, rowFromIn(emptyQaPair(), key)]);
  }, [commit, rows]);

  const removeRow = useCallback(
    (row: QaRow) => {
      commit(rows.filter((r) => r.key !== row.key));
      setRowError((prev) => {
        const next = { ...prev };
        delete next[row.key];
        return next;
      });
    },
    [commit, rows],
  );

  const setBusyFor = (key: string, value: boolean) =>
    setBusy((prev) => ({ ...prev, [key]: value }));
  const setErrorFor = (key: string, message: string | null) =>
    setRowError((prev) => {
      const next = { ...prev };
      if (message === null) delete next[key];
      else next[key] = message;
      return next;
    });

  /* ---- saved-mode writes ---- */

  async function savePair(row: QaRow) {
    if (props.mode !== "saved") return;
    const body = rowToPairIn(row);
    const problem = qaPairProblem(body);
    if (problem !== null) {
      setErrorFor(row.key, problem);
      return;
    }
    setBusyFor(row.key, true);
    setErrorFor(row.key, null);
    try {
      const ordinal = rows.findIndex((r) => r.key === row.key);
      const out =
        row.savedId === null
          ? await api.post<QaPairOut>(`/v1/sources/qa/${props.sourceId}/pairs`, { ...body, ordinal }, tenantHeaders())
          : await api.put<QaPairOut>(`/v1/sources/qa/pairs/${row.savedId}`, { ...body, ordinal }, tenantHeaders());
      setSavedRows((prev) => prev.map((r) => (r.key === row.key ? rowFromOut(out) : r)));
      props.onChanged?.();
    } catch (e) {
      setErrorFor(row.key, writeFailure(e));
    } finally {
      setBusyFor(row.key, false);
    }
  }

  async function deletePair(row: QaRow) {
    if (props.mode !== "saved" || row.savedId === null) {
      removeRow(row);
      return;
    }
    setBusyFor(row.key, true);
    try {
      // api.del carries no header argument (api.ts:79); every other delete in
      // the dashboard calls it exactly like this.
      await api.del(`/v1/sources/qa/pairs/${row.savedId}`);
      setSavedRows((prev) => prev.filter((r) => r.key !== row.key));
      setConfirming((c) => (c && c.key === row.key ? null : c));
      props.onChanged?.();
    } catch (e) {
      setErrorFor(row.key, writeFailure(e));
    } finally {
      setBusyFor(row.key, false);
    }
  }

  // Reorder is the ordinal write. Only the rows that actually moved are sent,
  // so dragging one pair to the top does not PUT the whole set.
  async function reorder(next: QaRow[]) {
    const before = rows.map((r) => r.key);
    commit(next);
    if (props.mode !== "saved") return;
    const moved = next.filter((r, i) => r.savedId !== null && before[i] !== r.key);
    for (const row of moved) {
      const ordinal = next.findIndex((r) => r.key === row.key);
      setBusyFor(row.key, true);
      try {
        await api.put<QaPairOut>(
          `/v1/sources/qa/pairs/${row.savedId}`,
          { ...rowToPairIn(row), ordinal },
          tenantHeaders(),
        );
      } catch (e) {
        setErrorFor(row.key, writeFailure(e));
      } finally {
        setBusyFor(row.key, false);
      }
    }
    if (moved.length > 0) props.onChanged?.();
  }

  return (
    <div className={cx("flex flex-col gap-3", props.className)}>
      {rows.length === 0 ? (
        <div className="rounded-xl border border-dashed border-line-strong bg-canvas px-4 py-8 text-center">
          <p className="text-sm font-medium text-ink">No pairs yet</p>
          <p className="mt-1 text-[13px] text-muted">
            A pair is one answer plus every way your customers ask for it.
          </p>
          <Button variant="secondary" size="sm" className="mt-3" disabled={disabled} onClick={addPair}>
            <Icon.Plus size={14} /> Add the first pair
          </Button>
        </div>
      ) : (
        <SortableList
          items={rows}
          getId={(row) => row.key}
          onReorder={(next) => void reorder(next)}
          disabled={disabled || rows.length < 2}
          renderItem={(row, index) => (
            <PairRow
              row={row}
              index={index}
              total={rows.length}
              saved={saved}
              disabled={disabled}
              busy={busy[row.key] === true}
              failure={rowError[row.key] ?? null}
              onPatch={patch}
              onSave={() => void savePair(row)}
              onRemove={() => {
                if (saved && row.savedId !== null) setConfirming(row);
                else removeRow(row);
              }}
            />
          )}
        />
      )}

      {rows.length > 0 && (
        <div>
          <Button variant="ghost" size="sm" disabled={disabled} onClick={addPair}>
            <Icon.Plus size={14} /> Add another pair
          </Button>
        </div>
      )}

      {confirming !== null && (
        <ConfirmDialog
          title="Delete this pair?"
          message={`"${confirming.title || confirming.questions[0] || "Untitled pair"}" stops being answerable as soon as this is deleted.`}
          danger
          error={rowError[confirming.key] ?? undefined}
          busy={busy[confirming.key] === true}
          onCancel={() => { if (busy[confirming.key] !== true) setConfirming(null); }}
          onConfirm={() => {
            void deletePair(confirming);
          }}
        />
      )}
    </div>
  );
}

function writeFailure(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 403) return "Q&A pairs are switched off for this workspace.";
    if (e.status === 429) return "Too many changes at once. Try again in a moment.";
    return e.message;
  }
  return "That did not save. Check your connection and try again.";
}

/* ------------------------------------------------------------------ one pair */

function PairRow({
  row,
  index,
  total,
  saved,
  disabled,
  busy,
  failure,
  onPatch,
  onSave,
  onRemove,
}: {
  row: QaRow;
  index: number;
  total: number;
  saved: boolean;
  disabled: boolean;
  busy: boolean;
  failure: string | null;
  onPatch: (key: string, change: Partial<QaRow>) => void;
  onSave: () => void;
  onRemove: () => void;
}) {
  const [activityOpen, setActivityOpen] = useState(false);
  const [activity, setActivity] = useState<QaPairActivity | null>(null);
  const [activityBusy, setActivityBusy] = useState(false);
  const [activityError, setActivityError] = useState<string | null>(null);

  const atQuestionCap = row.questions.length >= QA_MAX_QUESTIONS;

  const setQuestion = (i: number, value: string) =>
    onPatch(row.key, { questions: row.questions.map((q, qi) => (qi === i ? value : q)) });

  const addQuestion = () => {
    if (atQuestionCap) return;
    onPatch(row.key, { questions: [...row.questions, ""] });
  };

  const removeQuestion = (i: number) => {
    if (row.questions.length <= 1) {
      onPatch(row.key, { questions: [""] });
      return;
    }
    onPatch(row.key, { questions: row.questions.filter((_, qi) => qi !== i) });
  };

  async function toggleActivity() {
    if (activityOpen) {
      setActivityOpen(false);
      return;
    }
    setActivityOpen(true);
    if (activity !== null || row.savedId === null) return;
    setActivityBusy(true);
    setActivityError(null);
    try {
      setActivity(
        await api.get<QaPairActivity>(`/v1/sources/qa/pairs/${row.savedId}/activity?days=30`, tenantHeaders()),
      );
    } catch (e) {
      // Degrades to the counters already on the row rather than blanking it.
      setActivityError(e instanceof ApiError ? e.message : "Activity is not available yet.");
    } finally {
      setActivityBusy(false);
    }
  }

  const answerLong = row.answer.length > QA_ANSWER_MAX * 0.9;

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-line bg-surface p-4">
      <div className="flex items-start gap-2">
        <span className="mt-2 shrink-0 text-[11px] font-semibold tabular-nums text-faint">
          {index + 1}/{total}
        </span>
        <div className="min-w-0 flex-1">
          <Input
            value={row.title}
            maxLength={QA_TITLE_MAX}
            disabled={disabled}
            placeholder="What is this pair about? (optional)"
            aria-label={`Pair ${index + 1} title`}
            onChange={(e) => onPatch(row.key, { title: e.target.value })}
          />
        </div>
        <div className="flex shrink-0 items-center gap-1.5 pt-1">
          {busy && <Spinner className="text-brand" />}
          {saved && row.status !== null && <StatusBadge status={row.status} reason={row.error} />}
          {saved && row.savedId === null && <Badge tone="warn">unsaved</Badge>}
          {!saved && row.dirty && <span className="text-[11px] text-faint">edited</span>}
          <button
            type="button"
            disabled={disabled || busy}
            onClick={onRemove}
            aria-label={`Remove pair ${index + 1}`}
            className="rounded-md p-1.5 text-faint transition-colors hover:bg-danger-soft hover:text-danger disabled:pointer-events-none disabled:opacity-50"
          >
            <Icon.Trash size={16} />
          </button>
        </div>
      </div>

      <Field
        label="Questions"
        hint="Every phrasing your customers use. Each one is matched on its own, and they all answer with the text below — so more phrasings means more of them get answered."
      >
        <div className="flex flex-col gap-1.5">
          {row.questions.map((question, i) => (
            <div key={i} className="flex items-center gap-1.5">
              <Input
                value={question}
                disabled={disabled}
                placeholder={i === 0 ? "How do I get a refund?" : "Another way to ask the same thing"}
                aria-label={`Pair ${index + 1}, phrasing ${i + 1}`}
                onChange={(e) => setQuestion(i, e.target.value)}
                onKeyDown={(e) => {
                  // Enter opens the next phrasing — the whole point of the row.
                  // Alt is deliberately untouched: Alt+Arrow belongs to
                  // SortableList's keyboard reorder path.
                  if (e.key === "Enter" && !e.altKey && !e.shiftKey) {
                    e.preventDefault();
                    addQuestion();
                  }
                }}
              />
              <button
                type="button"
                disabled={disabled || (row.questions.length === 1 && question === "")}
                onClick={() => removeQuestion(i)}
                aria-label={`Remove phrasing ${i + 1}`}
                className="shrink-0 rounded-md p-1.5 text-faint transition-colors hover:bg-canvas hover:text-ink disabled:pointer-events-none disabled:opacity-40"
              >
                <Icon.X size={14} />
              </button>
            </div>
          ))}
          <div className="flex items-center justify-between gap-2">
            <Button variant="ghost" size="sm" disabled={disabled || atQuestionCap} onClick={addQuestion}>
              <Icon.Plus size={14} /> Add a phrasing
            </Button>
            <span className={cx("text-[11px] tabular-nums", atQuestionCap ? "text-warn" : "text-faint")}>
              {row.questions.length} of {QA_MAX_QUESTIONS}
            </span>
          </div>
        </div>
      </Field>

      <Field label="Answer" hint="Answered word for word, so write it the way you would say it.">
        <Textarea
          mono
          resizable
          value={row.answer}
          rows={4}
          maxLength={QA_ANSWER_MAX}
          disabled={disabled}
          placeholder="Refunds are available within 30 days of purchase…"
          aria-label={`Pair ${index + 1} answer`}
          onChange={(e) => onPatch(row.key, { answer: e.target.value })}
        />
        <div className="mt-1 flex justify-end">
          <span className={cx("text-[11px] tabular-nums", answerLong ? "text-warn" : "text-faint")}>
            {row.answer.length.toLocaleString()} / {QA_ANSWER_MAX.toLocaleString()}
          </span>
        </div>
      </Field>

      {row.error !== null && row.error !== "" && <Alert tone="warn">{row.error}</Alert>}
      {failure !== null && <Alert tone="danger">{failure}</Alert>}

      {saved && (
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line pt-3">
          <div className="flex items-center gap-3 text-[12px] text-muted">
            <span className="tabular-nums">
              Asked {row.timesAsked.toLocaleString()} {row.timesAsked === 1 ? "time" : "times"}
            </span>
            {row.lastAsked !== null && <span>Last {relativeTime(row.lastAsked)}</span>}
            {row.savedId !== null && (
              <Button variant="ghost" size="sm" onClick={() => void toggleActivity()}>
                <Icon.Chart size={14} /> {activityOpen ? "Hide activity" : "Activity"}
              </Button>
            )}
          </div>
          <Button
            variant="primary"
            size="sm"
            loading={busy}
            disabled={disabled || busy || !row.dirty}
            onClick={onSave}
          >
            {row.savedId === null ? "Save pair" : "Save changes"}
          </Button>
        </div>
      )}

      {saved && activityOpen && (
        <div className="rounded-lg border border-line bg-canvas p-3">
          {activityBusy && (
            <span className="flex items-center gap-2 text-[12px] text-muted">
              <Spinner className="text-brand" /> Loading the last 30 days…
            </span>
          )}
          {activityError !== null && <p className="text-[12px] text-muted">{activityError}</p>}
          {activity !== null && activity.points.length === 0 && (
            <p className="text-[12px] text-muted">Nobody has asked this in the last 30 days.</p>
          )}
          {activity !== null && activity.points.length > 0 && (
            <LineChart
              height={84}
              series={{
                label: "Times asked",
                points: activity.points.map((p) => ({ x: p.day, y: p.asked })),
              }}
            />
          )}
        </div>
      )}
    </div>
  );
}

