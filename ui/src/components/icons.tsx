// Stroke icons (24px grid), matching the design's line style.
import type { SVGProps } from "react";

const P: Record<string, string> = {
  logo: "M4 7h2M9 7h11M4 12h2M9 12h7M4 17h2M9 17h11",
  overview: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
  sliders: "M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1M15 4v4M9 10v4M17 16v4",
  table: "M4 5h16v14H4zM4 10h16M4 15h16M10 5v14",
  template: "M8 8h12v12H8zM4 16V4h12",
  layers: "M12 3l9 5-9 5-9-5zM3 13l9 5 9-5",
  clock: "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18zM12 7v5l3 3",
  pipeline: "M4 6h6v4H4zM14 14h6v4h-6zM7 10v4h10",
  server: "M4 4h16v6H4zM4 14h16v6H4zM8 7h.01M8 17h.01",
  plug: "M9 2v6M15 2v6M6 8h12v3a6 6 0 0 1-12 0zM12 17v5",
  users: "M16 20v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 10a4 4 0 1 0 0-8a4 4 0 1 0 0 8M22 20v-2a4 4 0 0 0-3-3.87M16 2.13a4 4 0 0 1 0 7.75",
  shield: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z",
  list: "M9 6h11M9 12h11M9 18h11M4 6h1M4 12h1M4 18h1",
  info: "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18zM12 11v5M12 8h.01",
  alert: "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18zM12 8v5M12 16h.01",
  warn: "M12 3l10 18H2zM12 10v5M12 18h.01",
  check: "M5 12l5 5L20 7",
  x: "M6 6l12 12M18 6L6 18",
  lock: "M6 11V8a6 6 0 0 1 12 0v3M5 11h14v10H5z",
  eye: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12zM12 9a3 3 0 1 0 0 6a3 3 0 1 0 0-6z",
  undo: "M3 12a9 9 0 1 0 3-6.7M3 4v5h5",
  search: "M11 4a7 7 0 1 0 0 14a7 7 0 1 0 0-14zM20 20l-4-4",
  plus: "M12 5v14M5 12h14",
  minus: "M5 12h14",
  download: "M12 4v11M7 10l5 5 5-5M5 20h14",
  copy: "M9 9h11v11H9zM5 15H4V4h11v1",
  trash: "M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3",
  caret: "M7 10l5 5 5-5",
  chevron: "M9 6l6 6-6 6",
  circle: "M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18z",
  refresh: "M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5",
  external: "M14 4h6v6M20 4l-9 9M18 14v6H4V6h6",
  database: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3zM4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  filter: "M4 5h16l-6 8v6l-4-2v-4z",
  columns: "M4 5h16v14H4zM10 5v14M15 5v14",
  chevronLeft: "M15 6l-6 6 6 6",
  first: "M17 6l-6 6 6 6M7 6v12",
  last: "M7 6l6 6-6 6M17 6v12",
  bookmark: "M6 4h12v17l-6-4-6 4z",
  link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
  terminal: "M4 5h16v14H4zM8 10l3 2-3 2M13 15h4",
  expand: "M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5",
  collapse: "M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5",
  activity: "M3 12h4l3-8 4 16 3-8h4",
  key: "M15 7a4 4 0 1 1-3.9 5H8v3H5v-3H3v-3h8.1A4 4 0 0 1 15 7z",
};

export type IconName = keyof typeof P;

export function Icon({ name, size = 18, strokeWidth = 2, ...rest }: { name: IconName; size?: number; strokeWidth?: number } & SVGProps<SVGSVGElement>) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={strokeWidth}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" {...rest}>
      <path d={P[name]} />
    </svg>
  );
}
