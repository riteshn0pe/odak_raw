// Inline SVG icons (no dependency). Stroke-based, 1.75 width, currentColor.
import type { ReactNode, SVGProps } from "react";

type P = SVGProps<SVGSVGElement> & { size?: number };
function Svg({ size = 18, children, ...rest }: P & { children: ReactNode }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth={1.75} strokeLinecap="round" strokeLinejoin="round"
      aria-hidden {...rest}>{children}</svg>
  );
}

export const Icon = {
  Doc: (p: P) => <Svg {...p}><path d="M14 3v4a1 1 0 0 0 1 1h4" /><path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2Z" /></Svg>,
  Upload: (p: P) => <Svg {...p}><path d="M12 16V4m0 0 4 4m-4-4L8 8" /><path d="M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" /></Svg>,
  Trash: (p: P) => <Svg {...p}><path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2m-8 0 1 13a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1l1-13" /></Svg>,
  Plus: (p: P) => <Svg {...p}><path d="M12 5v14M5 12h14" /></Svg>,
  Search: (p: P) => <Svg {...p}><circle cx="11" cy="11" r="7" /><path d="m21 21-4.3-4.3" /></Svg>,
  Check: (p: P) => <Svg {...p}><path d="m20 6-11 11-5-5" /></Svg>,
  X: (p: P) => <Svg {...p}><path d="M18 6 6 18M6 6l12 12" /></Svg>,
  Chevron: (p: P) => <Svg {...p}><path d="m6 9 6 6 6-6" /></Svg>,
  Link: (p: P) => <Svg {...p}><path d="M9 15 15 9" /><path d="M11 6l1-1a4 4 0 0 1 6 6l-1 1" /><path d="M13 18l-1 1a4 4 0 0 1-6-6l1-1" /></Svg>,
  Chat: (p: P) => <Svg {...p}><path d="M21 15a2 2 0 0 1-2 2H8l-4 4V5a2 2 0 0 1 2-2h13a2 2 0 0 1 2 2Z" /></Svg>,
  Settings: (p: P) => <Svg {...p}><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2V21a2 2 0 1 1-4 0v-.1A1.7 1.7 0 0 0 7 19.4l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0-1.2-2.9H3a2 2 0 1 1 0-4h.1A1.7 1.7 0 0 0 4.6 7l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 10 4.6V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0 1.2 2.9H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z" /></Svg>,
  Key: (p: P) => <Svg {...p}><circle cx="7.5" cy="15.5" r="4.5" /><path d="m10.5 12.5 8-8m-3 3 2 2m-4 0 2 2" /></Svg>,
  Chart: (p: P) => <Svg {...p}><path d="M3 3v18h18" /><path d="M7 14v3M12 9v8M17 5v12" /></Svg>,
  Users: (p: P) => <Svg {...p}><circle cx="9" cy="8" r="3.5" /><path d="M2.5 20a6.5 6.5 0 0 1 13 0" /><path d="M16 5.5a3.5 3.5 0 0 1 0 6.9M17 20a6.5 6.5 0 0 0-3-5.5" /></Svg>,
  Shield: (p: P) => <Svg {...p}><path d="M12 3 5 6v5c0 4.5 3 8 7 10 4-2 7-5.5 7-10V6l-7-3Z" /></Svg>,
  Book: (p: P) => <Svg {...p}><path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2V5Z" /><path d="M19 3v16" /></Svg>,
  Plug: (p: P) => <Svg {...p}><path d="M9 3v6M15 3v6M6 9h12v3a6 6 0 0 1-12 0V9ZM12 18v3" /></Svg>,
  Copy: (p: P) => <Svg {...p}><rect x="9" y="9" width="12" height="12" rx="2" /><path d="M5 15V5a2 2 0 0 1 2-2h10" /></Svg>,
  External: (p: P) => <Svg {...p}><path d="M14 4h6v6M20 4l-9 9M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4" /></Svg>,
  Spark: (p: P) => <Svg {...p}><path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M18 6l-2.5 2.5M8.5 15.5 6 18" /></Svg>,
  Alert: (p: P) => <Svg {...p}><path d="M12 8v5M12 16h.01" /><circle cx="12" cy="12" r="9" /></Svg>,
  Logout: (p: P) => <Svg {...p}><path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 17l5-5-5-5M15 12H3" /></Svg>,
  Home: (p: P) => <Svg {...p}><path d="M4 11 12 4l8 7M6 10v9a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1v-9" /></Svg>,
  Inbox: (p: P) => <Svg {...p}><path d="M4 13h4l1.5 3h5L16 13h4M4 13 6 5h12l2 8v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1Z" /></Svg>,
  Sun: (p: P) => <Svg {...p}><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></Svg>,
  Moon: (p: P) => <Svg {...p}><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z" /></Svg>,
  Info: (p: P) => <Svg {...p}><circle cx="12" cy="12" r="9" /><path d="M12 16v-4M12 8h.01" /></Svg>,
  Bolt: (p: P) => <Svg {...p}><path d="M13 2 4 14h6l-1 8 9-12h-6l1-8Z" /></Svg>,
  ChevronDown: (p: P) => <Svg {...p}><path d="m6 9 6 6 6-6" /></Svg>,
  Selector: (p: P) => <Svg {...p}><path d="m8 9 4-4 4 4M8 15l4 4 4-4" /></Svg>,
  // Added for the agent-identity glyph set (§6.1): that list names Globe, and a
  // missing key there is a type error, not a missing picture.
  Globe: (p: P) => <Svg {...p}><circle cx="12" cy="12" r="9" /><path d="M3 12h18" /><path d="M12 3a13 13 0 0 1 0 18 13 13 0 0 1 0-18Z" /></Svg>,
  // Critical-settings console: the step-up gate and secret reveal rely on
  // these — the eye is the only affordance that ever shows a key in clear.
  Lock: (p: P) => <Svg {...p}><rect x="5" y="11" width="14" height="9" rx="2" /><path d="M8 11V7a4 4 0 0 1 8 0v4" /></Svg>,
  Eye: (p: P) => <Svg {...p}><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z" /><circle cx="12" cy="12" r="2.8" /></Svg>,
  EyeOff: (p: P) => <Svg {...p}><path d="M4 4l16 16" /><path d="M9.9 5.6A9.4 9.4 0 0 1 12 5.5c6 0 9.5 6.5 9.5 6.5a17.3 17.3 0 0 1-3.2 3.9M6.1 6.4C3.4 8.3 2.5 12 2.5 12s3.5 6.5 9.5 6.5a9.6 9.6 0 0 0 5-1.4" /><path d="M9.9 9.9a2.8 2.8 0 0 0 4 4" /></Svg>,
  // Report PDFs: the arrow-into-tray reads as "save this file" where External
  // (arrow leaving a box) reads as "go somewhere else".
  Download: (p: P) => <Svg {...p}><path d="M12 3v11M8 10l4 4 4-4" /><path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" /></Svg>,
};

