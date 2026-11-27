// Client-side CSV export. The audit log (§7.5) needs "give me this table as a
// file" and has no server endpoint for it. The rows are already in memory,
// so serialising them here costs one function and no dependency, which is also
// what keeps FRONTEND_SPEC.md's zero-dependency claim true.
//
// Escaping rule, and it matters more than it looks: a quote inside a field is
// doubled, and a field is quoted whenever it contains a quote, a comma, a
// newline, or leading/trailing whitespace Excel would otherwise eat. A leading
// `=`, `+`, `-` or `@` is prefixed with a tab, because Excel and Sheets both
// treat such a cell as a formula — an audit entry whose detail begins with `=`
// must not become executable content in a compliance reviewer's spreadsheet.

export type CsvCell = string | number | boolean | null | undefined;

const NEEDS_QUOTES = /[",\r\n]|^\s|\s$/;
const FORMULA_LEAD = /^[=+\-@\t\r]/;

function cell(value: CsvCell): string {
  if (value === null || value === undefined) return "";
  let text = typeof value === "string" ? value : String(value);
  // Defuse spreadsheet formula injection before quoting, not after: the tab has
  // to be inside the quoted field for the escape to survive a round trip.
  if (FORMULA_LEAD.test(text)) text = `\t${text}`;
  if (NEEDS_QUOTES.test(text)) return `"${text.replace(/"/g, '""')}"`;
  return text;
}

/** Serialise a header row plus body rows to CSV text with CRLF line endings. */
export function toCsv(header: readonly string[], rows: readonly CsvCell[][]): string {
  const lines = [header.map(cell).join(",")];
  for (const row of rows) lines.push(row.map(cell).join(","));
  // CRLF because that is what every spreadsheet on Windows expects, and the
  // ones that do not care accept it silently.
  return lines.join("\r\n");
}

/**
 * Build the CSV and hand it to the browser as a download.
 *
 * The BOM is not decoration: without it Excel decodes a UTF-8 file as the local
 * ANSI codepage, so an agent name with an accent or a rupee sign arrives
 * mangled. The object URL is revoked on the next frame — revoking it
 * synchronously cancels the download in Safari.
 */
export function downloadCsv(
  filename: string,
  header: readonly string[],
  rows: readonly CsvCell[][],
): void {
  const blob = new Blob(["﻿", toCsv(header, rows)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename.endsWith(".csv") ? filename : `${filename}.csv`;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  requestAnimationFrame(() => URL.revokeObjectURL(url));
}

/** `audit-log-2026-08-16.csv` — a stable, sortable filename stem. */
export function datedFilename(stem: string, at: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${stem}-${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}.csv`;
}
