// The one place an agent config edit is turned into a PATCH body (§4.8).
//
// PATCH /v1/agents/{id} does `merged = {**existing.config, **payload.config}` and
// then validates the result. That merge is SHALLOW and only at the top level, so
// a sub-block you send REPLACES the stored one: sending
// `{retrieval: {top_k: 30}}` resets `min_confidence`, `rerank_enabled` and every
// other retrieval field to its Pydantic default. Sending a partial block is
// therefore data loss, not a partial update.
//
// The rule this module enforces: start from the last-read config, apply the edit
// on top, and send back the COMPLETE sub-blocks that changed — and only those.
// Blocks the screen never touched are absent from the payload, which is safe,
// because absent is the one case the server's shallow merge handles correctly.
import type { AgentConfig, AgentDetail } from "./types";

/** Spec name for the config blob (§4.8). Same shape as `AgentConfig`. */
export type AgentConfigShape = AgentConfig;

export type DeepPartial<T> = T extends (infer U)[]
  ? U[]
  : T extends object
    ? { [K in keyof T]?: DeepPartial<T[K]> }
    : T;

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// Arrays and scalars replace wholesale; only plain objects merge key by key.
// Merging arrays element-wise would make "remove the second refusal phrase"
// impossible to express, which is the common edit on every list field.
function mergeDeep(base: unknown, edit: unknown): unknown {
  if (!isPlainObject(edit)) return edit;
  const out: Record<string, unknown> = isPlainObject(base) ? { ...base } : {};
  for (const [key, value] of Object.entries(edit)) {
    // `undefined` is "I am not editing this", never "delete this". A form that
    // spreads its state can hand us undefined for every field it does not own.
    if (value === undefined) continue;
    out[key] = mergeDeep(out[key], value);
  }
  return out;
}

/**
 * Build a PATCH `config` body from the last-read config plus an edit.
 *
 * - Only top-level keys named in `edits` appear in the result, so untouched
 *   blocks are left to the server's own merge instead of being re-sent.
 * - Every key that does appear carries the complete block: stored values first,
 *   the edit applied over them.
 * - `version` is stripped. It is a row column, not config, and echoing the
 *   client's copy back into the blob is what made the two drift apart.
 * - `opts.replace` names blocks that should be sent verbatim instead of merged.
 *   A deep merge cannot express "this key is gone", which is fine for the fixed
 *   blocks (every field is always present in the form) and wrong for the
 *   free-form maps inside them — `retrieval.filters` is a user-authored
 *   dictionary, and removing a filter has to actually remove it. A caller that
 *   holds the complete block passes its name here and owns it end to end.
 */
export function patchConfig(
  base: AgentConfigShape | null | undefined,
  edits: DeepPartial<AgentConfigShape>,
  opts?: { replace?: readonly string[] },
): Partial<AgentConfigShape> {
  const stored = (base ?? {}) as unknown as Record<string, unknown>;
  const out: Record<string, unknown> = {};
  for (const [key, edit] of Object.entries(edits as Record<string, unknown>)) {
    if (key === "version") continue;
    if (edit === undefined) continue;
    out[key] = opts?.replace?.includes(key) ? edit : mergeDeep(stored[key], edit);
  }
  return out as Partial<AgentConfigShape>;
}

/**
 * Thrown by `AgentLayout.patch()` when the agent's `version` moved between the
 * read the editor started from and the write it is trying to make — i.e. someone
 * else saved while this form was open. The caller owns the recovery UI (§4.8:
 * discard mine and reload / see what changed / save mine anyway), which is why
 * the remote row rides along: the banner needs it to diff without a second GET.
 */
export class AgentVersionConflict extends Error {
  constructor(
    readonly localVersion: number,
    readonly remote: AgentDetail,
  ) {
    super("This agent changed while you were editing it.");
    this.name = "AgentVersionConflict";
  }
}
