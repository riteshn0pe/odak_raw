// Minimal SVG line and bar charts. Hand-rolled because a charting library would
// be the largest dependency in the build for a handful of series. Not a general
// tool — just enough for the usage and quality panels.
interface Series { label: string; points: { x: string; y: number }[]; }

function NoData() {
  return (
    <div className="rounded-xl border border-dashed border-line bg-canvas px-5 py-12 text-center text-sm text-faint">
      No data yet.
    </div>
  );
}

export function LineChart({ series, height = 160 }: { series: Series; height?: number }) {
  const pts = series.points;
  if (pts.length === 0) return <NoData />;
  const max = Math.max(...pts.map((p) => p.y), 1);
  const w = 100, h = 100;
  const gid = `line-fill-${series.label.replace(/[^a-z0-9]/gi, "")}`;
  const coords = pts.map((p, i) => `${(i / Math.max(pts.length - 1, 1)) * w},${h - (p.y / max) * h}`);
  const area = `0,${h} ${coords.join(" ")} ${w},${h}`;
  return (
    <div className="relative">
      {/* horizontal gridlines for a sense of scale */}
      <div aria-hidden className="absolute inset-0 flex flex-col justify-between">
        {[0, 1, 2, 3].map((i) => <span key={i} className="h-px w-full bg-line/60" />)}
      </div>
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" style={{ width: "100%", height }} className="relative text-brand overflow-visible">
        <defs>
          <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="currentColor" stopOpacity={0.18} />
            <stop offset="100%" stopColor="currentColor" stopOpacity={0.01} />
          </linearGradient>
        </defs>
        <polygon points={area} fill={`url(#${gid})`} />
        <polyline points={coords.join(" ")} fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
      </svg>
    </div>
  );
}

export function BarChart({ data, height = 160 }: { data: { label: string; value: number }[]; height?: number }) {
  if (data.length === 0) return <NoData />;
  const max = Math.max(...data.map((d) => d.value), 1);
  return (
    <div className="flex items-end gap-2 text-brand" style={{ height }}>
      {data.map((d) => (
        <div key={d.label} className="group flex flex-col items-center gap-1.5 flex-1 min-w-0" title={`${d.label}: ${d.value.toLocaleString()}`}>
          <div
            className="w-full rounded-t-md bg-current opacity-85 transition-[height,opacity] group-hover:opacity-100"
            style={{ height: `${(d.value / max) * (height - 22)}px` }}
          />
          <span className="text-[10px] text-faint truncate max-w-full">{d.label}</span>
        </div>
      ))}
    </div>
  );
}
