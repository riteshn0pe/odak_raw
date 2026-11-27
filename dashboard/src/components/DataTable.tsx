// Sortable, paginated table shared by every list screen. Owns empty and loading
// states so no route reinvents them, and takes a render function per column so a
// cell can be a badge or a link without the table knowing about either.
import { useMemo, useState } from "react";
import { cx, Button, Skeleton, EmptyState } from "./ui";
import { Icon } from "./ui/icons";

export interface Column<T> {
  key: string;
  header: string;
  render: (row: T) => React.ReactNode;
  sortValue?: (row: T) => string | number;
  align?: "left" | "right" | "center";
}

interface Props<T> {
  columns: Column<T>[];
  rows: T[];
  loading?: boolean;
  empty?: string;
  onRowClick?: (row: T) => void;
  pageSize?: number;
  /** Adds the checkbox column. Off by default — with it off the DOM is byte-identical
      to before, which is what keeps the existing call sites untouched. */
  selectable?: boolean;
  /** Stable id per row. Falls back to the row index, which is only right for one
      unsorted page, so pass this whenever `selectable` is on. */
  getRowId?: (row: T, index: number) => string;
  /** The selection lives with the caller: the bulk action bar belongs to the screen,
      not to the table. */
  selected?: Set<string>;
  onSelectedChange?: (next: Set<string>) => void;
}

const ALIGN = { left: "text-left", right: "text-right", center: "text-center" } as const;
const NOTHING_SELECTED = new Set<string>();
const CHECKBOX = "h-4 w-4 cursor-pointer accent-[var(--color-brand)] focus:outline-none focus-visible:ring-4 focus-visible:ring-brand-soft";

export function DataTable<T>({
  columns, rows, loading, empty = "Nothing here yet.", onRowClick, pageSize = 25,
  selectable, getRowId, selected, onSelectedChange,
}: Props<T>) {
  const [sortKey, setSortKey] = useState<string | null>(null);
  const [asc, setAsc] = useState(true);
  const [page, setPage] = useState(0);

  const sorted = useMemo(() => {
    if (!sortKey) return rows;
    const col = columns.find((c) => c.key === sortKey);
    if (!col?.sortValue) return rows;
    const factor = asc ? 1 : -1;
    return [...rows].sort((a, b) => {
      const va = col.sortValue!(a), vb = col.sortValue!(b);
      return va < vb ? -factor : va > vb ? factor : 0;
    });
  }, [rows, sortKey, asc, columns]);

  const pages = Math.ceil(sorted.length / pageSize);
  const safePage = Math.min(page, Math.max(0, pages - 1));
  const shown = sorted.slice(safePage * pageSize, safePage * pageSize + pageSize);

  // Selection. "Select all" means all rows on this page, never the whole filtered
  // set: a header checkbox that silently selects 4,000 unseen rows is how people
  // delete things they meant to keep.
  const rowId = (row: T, index: number) => getRowId?.(row, index) ?? String(index);
  const chosen = selected ?? NOTHING_SELECTED;
  const shownIds = selectable ? shown.map((row, i) => rowId(row, safePage * pageSize + i)) : [];
  const allShown = shownIds.length > 0 && shownIds.every((id) => chosen.has(id));
  const someShown = !allShown && shownIds.some((id) => chosen.has(id));

  const toggleShown = () => {
    const next = new Set(chosen);
    for (const id of shownIds) {
      if (allShown) next.delete(id);
      else next.add(id);
    }
    onSelectedChange?.(next);
  };

  const toggleOne = (id: string) => {
    const next = new Set(chosen);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    onSelectedChange?.(next);
  };

  const head = (
    <thead>
      <tr className="border-b border-line bg-canvas/60">
        {selectable && (
          <th className="w-10 px-4 py-3">
            <input
              type="checkbox"
              aria-label="Select all rows on this page"
              checked={allShown}
              // indeterminate is a property, not an attribute — React cannot set it
              // from JSX, so it is written on the node.
              ref={(el) => { if (el) el.indeterminate = someShown; }}
              onChange={toggleShown}
              className={CHECKBOX}
            />
          </th>
        )}
        {columns.map((col) => {
          const sortable = !!col.sortValue;
          const active = sortKey === col.key;
          return (
            <th
              key={col.key}
              aria-sort={active ? (asc ? "ascending" : "descending") : undefined}
              onClick={() => {
                if (!sortable) return;
                if (active) setAsc(!asc);
                else { setSortKey(col.key); setAsc(true); }
              }}
              className={cx(
                "px-4 py-3 text-[11px] uppercase tracking-wide font-semibold text-faint whitespace-nowrap select-none",
                ALIGN[col.align ?? "left"],
                sortable && "cursor-pointer hover:text-muted transition-colors",
              )}
            >
              <span className={cx("inline-flex items-center gap-1", col.align === "right" && "flex-row-reverse")}>
                {col.header}
                {sortable && (
                  <span className={cx("transition-colors", active ? "text-brand" : "text-line-strong")}>
                    {active
                      ? <Icon.Chevron size={13} className={asc ? "rotate-180" : ""} />
                      : <Icon.Selector size={13} />}
                  </span>
                )}
              </span>
            </th>
          );
        })}
      </tr>
    </thead>
  );

  // Skeleton rows keep column widths stable so the table does not jump on load.
  if (loading) {
    return (
      <div className="overflow-x-auto rounded-xl border border-line bg-surface shadow-card">
        <table className="w-full border-collapse text-sm">
          {head}
          <tbody>
            {Array.from({ length: Math.min(pageSize, 6) }).map((_, r) => (
              <tr key={r} className="border-b border-line last:border-0">
                {selectable && (
                  <td className="w-10 px-4 py-3.5"><Skeleton className="h-4 w-4" /></td>
                )}
                {columns.map((col) => (
                  <td key={col.key} className={cx("px-4 py-3.5", ALIGN[col.align ?? "left"])}>
                    <Skeleton className={cx("h-3.5", col.align === "right" ? "w-12 ml-auto" : "w-24")} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  if (rows.length === 0) {
    return (
      <div className="rounded-xl border border-line bg-surface shadow-card">
        <EmptyState icon={<Icon.Inbox />} title={empty} />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="overflow-x-auto rounded-xl border border-line bg-surface shadow-card">
        <table className="w-full border-collapse text-sm">
          {head}
          <tbody>
            {shown.map((row, i) => {
              const id = selectable ? rowId(row, safePage * pageSize + i) : "";
              const isSelected = selectable && chosen.has(id);
              return (
                <tr
                  key={i}
                  onClick={() => onRowClick?.(row)}
                  className={cx(
                    "border-b border-line last:border-0 transition-colors",
                    isSelected && "bg-brand-soft/60",
                    onRowClick && "cursor-pointer hover:bg-canvas",
                  )}
                >
                  {selectable && (
                    // Stops the row's own click: ticking a box is not opening the row.
                    <td className="w-10 px-4 py-3" onClick={(e) => e.stopPropagation()}>
                      <input
                        type="checkbox"
                        aria-label={`Select row ${safePage * pageSize + i + 1}`}
                        checked={!!isSelected}
                        onChange={() => toggleOne(id)}
                        className={CHECKBOX}
                      />
                    </td>
                  )}
                  {columns.map((col) => (
                    <td key={col.key} className={cx("px-4 py-3 text-ink align-middle", ALIGN[col.align ?? "left"])}>
                      {col.render(row)}
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {pages > 1 && (
        <div className="flex items-center justify-between gap-3">
          <Button size="sm" variant="secondary" disabled={safePage === 0} onClick={() => setPage(safePage - 1)}>
            <Icon.Chevron size={14} className="rotate-90" /> Previous
          </Button>
          <span className="text-sm text-muted">
            Page <span className="font-semibold text-ink">{safePage + 1}</span> of {pages}
          </span>
          <Button size="sm" variant="secondary" disabled={safePage >= pages - 1} onClick={() => setPage(safePage + 1)}>
            Next <Icon.Chevron size={14} className="-rotate-90" />
          </Button>
        </div>
      )}
    </div>
  );
}
