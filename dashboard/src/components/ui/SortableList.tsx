// Drag-to-reorder (§5.2): procedure steps, suggested questions, procedure branch
// children.
//
// Native HTML5 drag plus a dragover index, and a mandatory keyboard path: a
// focused handle moves its item with Alt+ArrowUp / Alt+ArrowDown and the result
// is announced. That path is the reason this is hand-rolled instead of pulled
// from a drag library — we own the accessibility rather than inheriting it.
import { useId, useState, type ReactNode } from "react";
import { cx } from "./index";
import { Icon } from "./icons";

export function SortableList<T>({ items, getId, onReorder, renderItem, handle = true, disabled }: {
  items: T[];
  getId: (item: T) => string;
  onReorder: (next: T[]) => void;
  renderItem: (item: T, index: number, dragging: boolean) => ReactNode;
  handle?: boolean;
  disabled?: boolean;
}) {
  const [dragId, setDragId] = useState<string | null>(null);
  const [overIndex, setOverIndex] = useState<number | null>(null);
  // Which row's handle is currently held down. Without this, `draggable` on the
  // row would hijack text selection inside the row's own inputs.
  const [armed, setArmed] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const hintId = useId();

  const move = (from: number, to: number) => {
    const target = Math.min(Math.max(to, 0), items.length - 1);
    if (from === target || from < 0) return;
    const next = items.slice();
    const [moved] = next.splice(from, 1);
    if (moved === undefined) return;
    next.splice(target, 0, moved);
    onReorder(next);
    setAnnouncement(`Moved to position ${target + 1} of ${items.length}.`);
  };

  const indexOf = (id: string | null) => (id === null ? -1 : items.findIndex((it) => getId(it) === id));

  const reset = () => { setDragId(null); setOverIndex(null); setArmed(null); };

  return (
    <div>
      <p id={hintId} className="sr-only">
        Press Alt with the up or down arrow keys to move this item.
      </p>

      <ul className="m-0 flex list-none flex-col gap-2 p-0">
        {items.map((item, i) => {
          const id = getId(item);
          const dragging = dragId === id;
          const isOver = overIndex === i && dragId !== null && !dragging;
          return (
            <li
              key={id}
              draggable={!disabled && (!handle || armed === id)}
              onDragStart={(e) => {
                setDragId(id);
                e.dataTransfer.effectAllowed = "move";
                // Firefox refuses to start a drag without payload.
                e.dataTransfer.setData("text/plain", id);
              }}
              onDragOver={(e) => {
                if (dragId === null) return;
                e.preventDefault();
                e.dataTransfer.dropEffect = "move";
                setOverIndex(i);
              }}
              onDrop={(e) => {
                e.preventDefault();
                move(indexOf(dragId), i);
                reset();
              }}
              onDragEnd={reset}
              className={cx(
                "flex items-start gap-2 rounded-lg border bg-surface transition-colors",
                dragging ? "border-brand opacity-50" : "border-line",
                isOver && "border-brand ring-4 ring-brand-soft",
              )}
            >
              {handle && (
                <button
                  type="button"
                  disabled={disabled}
                  aria-label={`Reorder item ${i + 1} of ${items.length}`}
                  aria-describedby={hintId}
                  onPointerDown={() => setArmed(id)}
                  onPointerUp={() => setArmed(null)}
                  onBlur={() => setArmed(null)}
                  onKeyDown={(e) => {
                    if (disabled || !e.altKey) return;
                    if (e.key === "ArrowUp") { e.preventDefault(); move(i, i - 1); }
                    else if (e.key === "ArrowDown") { e.preventDefault(); move(i, i + 1); }
                  }}
                  className={cx(
                    "mt-1 ml-1 shrink-0 rounded-md p-1.5 text-faint transition-colors",
                    "hover:bg-canvas hover:text-muted focus:outline-none focus:ring-4 focus:ring-brand-soft",
                    disabled ? "cursor-not-allowed opacity-45" : "cursor-grab active:cursor-grabbing",
                  )}
                >
                  <Icon.Selector size={15} />
                </button>
              )}
              <div className="min-w-0 flex-1 p-2">{renderItem(item, i, dragging)}</div>
            </li>
          );
        })}
      </ul>

      {/* Live region outside the list: moving a row re-parents its DOM node, and a
          message announced from inside the moved subtree is unreliable. */}
      <p aria-live="polite" role="status" className="sr-only">{announcement}</p>
    </div>
  );
}
