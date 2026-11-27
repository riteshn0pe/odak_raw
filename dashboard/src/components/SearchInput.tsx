// The search-box pattern that used to be hand-rolled in ~8 screens: a relative
// wrapper, an absolutely-positioned Icon.Search, and pl-9 padding on the input.
// One component so every filter bar looks and feels identical.
import type { InputHTMLAttributes } from "react";
import { cx, Input } from "./ui";
import { Icon } from "./ui/icons";

export function SearchInput(props: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <div className="relative">
      <span className="absolute left-3 top-1/2 -translate-y-1/2 text-faint pointer-events-none">
        <Icon.Search size={16} />
      </span>
      <Input {...props} className={cx("pl-9", props.className)} />
    </div>
  );
}
