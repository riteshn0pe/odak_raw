// Theme: light by default (production look), dark opt-in via `html.dark`.
// Persists choice; applied before React renders so there is no flash.
const KEY = "ca.theme";
export type Theme = "light" | "dark";

export function getTheme(): Theme {
  return (localStorage.getItem(KEY) as Theme) || "light";
}

export function applyTheme(t: Theme = getTheme()): void {
  document.documentElement.classList.toggle("dark", t === "dark");
}

export function setTheme(t: Theme): void {
  localStorage.setItem(KEY, t);
  applyTheme(t);
}

export function toggleTheme(): Theme {
  const next: Theme = getTheme() === "dark" ? "light" : "dark";
  setTheme(next);
  return next;
}

// apply immediately on import
applyTheme();
