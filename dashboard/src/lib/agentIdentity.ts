// Per-agent visual identity (§6.1).
//
// Two agents in the same workspace have to be distinguishable at a glance —
// in a sidebar, a conversation row, a widget preview — without the user reading
// names. So every agent resolves to a colour, a glyph, and a monogram, and the
// resolution is deterministic: same agent, same identity, on every screen and
// every reload. No state, no server round-trip.
//
// Colour is *chosen* if the agent's widget theme sets an accent, otherwise
// *derived* from a hash of the agent id. Deriving from the id and not the name
// matters: renaming an agent must not change its colour, or the visual anchor
// the user built is destroyed by an edit to a text field.
import type { CSSProperties } from "react";
import type { Icon } from "../components/ui/icons";

export interface Identity {
  /** Hex, always. CSS derives tint/hairline/ink from it — see tailwind.css. */
  color: string;
  glyph: keyof typeof Icon;
  monogram: string;
}

// The ten-swatch palette, in spec order. Hand-picked rather than generated:
// every entry clears 3:1 against both canvas colours as a fill, and no two
// neighbours collide for the common forms of colour blindness.
export const IDENTITY_SWATCHES = [
  "#2563eb", // blue
  "#1d4ed8", // deep blue
  "#0f766e", // teal
  "#7c3aed", // violet
  "#059669", // green
  "#b45309", // amber
  "#c2410c", // orange
  "#475569", // slate
  "#0f172a", // graphite
  "#be123c", // rose
] as const;

// Glyphs an agent can be given. Kept to shapes that survive at 16px and read as
// a *kind of assistant* (support, docs, sales) rather than as decoration.
export const IDENTITY_GLYPHS: (keyof typeof Icon)[] = [
  "Chat",
  "Inbox",
  "Spark",
  "Book",
  "Users",
  "Settings",
  "Check",
  "Key",
  "Home",
  "Globe",
  "Shield",
  "Bolt",
];

// Green is the one swatch that fails white text: #059669 on white measures
// 3.77:1, under the 4.5:1 body-text floor. Letter plates — the only place the
// swatch becomes a background for text — therefore use a darkened stand-in
// (#047857, 5.48:1). Swatches, tints, and rails keep the original: they carry no
// text, and shifting them would break the visual match with the widget accent.
const PLATE_OVERRIDES: Record<string, string> = {
  "#059669": "#047857",
};

/** Anything with an id is enough to get an identity; config only refines it. */
export interface AgentLike {
  id: string;
  name?: string | null;
  config?: { widget?: Record<string, unknown> | null } | null;
}

function hash(input: string): number {
  // FNV-1a, 32-bit. Cheap, stable across runtimes, and well spread for the
  // short UUID-ish strings we feed it — a naive charCode sum buckets badly.
  let h = 0x811c9dc5;
  for (let i = 0; i < input.length; i += 1) {
    h ^= input.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

const HEX = /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i;

function widgetTheme(agent: AgentLike): Record<string, unknown> {
  // widget is a loose blob (the server validates it, the client round-trips it),
  // so every hop is checked rather than asserted.
  const widget = agent.config?.widget;
  if (!widget || typeof widget !== "object") return {};
  const theme = (widget as Record<string, unknown>).theme;
  if (!theme || typeof theme !== "object") return {};
  return theme as Record<string, unknown>;
}

/** Initials, up to two, from the agent name. Falls back to the id's first char. */
export function monogramOf(name: string | null | undefined, id: string): string {
  const words = (name ?? "").trim().split(/[\s._-]+/).filter(Boolean);
  const first = words[0];
  if (!first) return (id.slice(0, 1) || "?").toUpperCase();
  if (words.length === 1) return first.slice(0, 2).toUpperCase();
  const last = words[words.length - 1] ?? first;
  return (first.slice(0, 1) + last.slice(0, 1)).toUpperCase();
}

export function identityOf(agent: AgentLike): Identity {
  const theme = widgetTheme(agent);
  const seed = hash(agent.id);

  const accent = theme.accent;
  const color =
    typeof accent === "string" && HEX.test(accent.trim())
      ? accent.trim().toLowerCase()
      : IDENTITY_SWATCHES[seed % IDENTITY_SWATCHES.length] ?? IDENTITY_SWATCHES[0];

  const chosenGlyph = theme.glyph;
  const glyph: keyof typeof Icon =
    typeof chosenGlyph === "string" && (IDENTITY_GLYPHS as string[]).includes(chosenGlyph)
      ? (chosenGlyph as keyof typeof Icon)
      // Shifted seed so an agent's glyph is not locked to its colour: two agents
      // that share a swatch should still differ in shape.
      : IDENTITY_GLYPHS[(seed >>> 8) % IDENTITY_GLYPHS.length] ?? "Chat";

  return { color, glyph, monogram: monogramOf(agent.name, agent.id) };
}

/**
 * Inline style for the element that owns the identity. Pair with the
 * `data-agent-identity` attribute — the CSS in tailwind.css derives
 * --agent-tint / --agent-hairline / --agent-ink from this one variable, so the
 * derivation follows the theme without a re-render.
 */
export function identityStyle(id: Identity): CSSProperties {
  return { "--agent-color": id.color } as CSSProperties;
}

/** Background for a plate that carries text (monogram, letter avatar). */
export function identityPlate(id: Identity): string {
  return PLATE_OVERRIDES[id.color] ?? id.color;
}

function channel(hex: string, at: number): number {
  const v = parseInt(hex.slice(at, at + 2), 16) / 255;
  return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
}

/** WCAG 2.1 relative luminance, 0 (black) to 1 (white). */
export function luminance(hex: string): number {
  const h = hex.trim().replace("#", "");
  // #abc expands to #aabbcc by doubling each digit.
  const full = h.length === 3 ? h.replace(/./g, (c) => c + c) : h;
  if (full.length !== 6) return 0.5; // unparseable: assume mid, change nothing
  return (
    0.2126 * channel(full, 0) + 0.7152 * channel(full, 2) + 0.0722 * channel(full, 4)
  );
}

/**
 * Ink colour for text tinted by an agent's accent.
 *
 * A user-chosen accent is not required to be legible — someone will pick
 * #fde047, and on white that is invisible. Rather than reject the colour (which
 * loses their choice) or ignore legibility (which loses the text), the extremes
 * are pulled back toward a safe anchor and everything in the usable middle is
 * left exactly as chosen.
 */
export function identityInk(hex: string, dark = false): string {
  const l = luminance(hex);
  if (!dark && l > 0.62) {
    // Too pale for white backgrounds — mix toward the near-black primary.
    return `color-mix(in srgb, ${hex} 55%, var(--color-primary))`;
  }
  if (dark && l < 0.06) {
    // Too dark to see on #14171d — lift it toward white.
    return `color-mix(in srgb, ${hex} 55%, white)`;
  }
  return hex;
}
