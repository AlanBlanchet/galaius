// Minimal type re-declarations (matches shared.ts, avoids importing from extension host)
interface Action {
  type: string;
  label: string;
  data?: Record<string, string>;
  style?: "primary" | "secondary";
}

type RangeId = "24h" | "7d" | "30d" | "all";

type CellContent =
  | {
      kind: "row";
      label: string;
      value?: string;
      dot?: "ok" | "missing";
      actions?: Action[];
      tooltip?: string;
    }
  | { kind: "table"; headers: string[]; rows: string[][] }
  | { kind: "chart"; points: { x: string; y: number }[]; yPrefix?: string }
  | {
      kind: "bar-h";
      bars: { label: string; value: number; color?: string }[];
      valuePrefix?: string;
      ariaSummary: string;
    }
  | {
      kind: "stacked-bar";
      xLabels: string[];
      series: { name: string; color: string; values: number[] }[];
      valuePrefix?: string;
      ariaSummary: string;
    }
  | {
      kind: "small-multiples";
      panels: {
        title: string;
        bars: { label: string; value: number; color: string }[];
      }[];
      valuePrefix?: string;
      ariaSummary: string;
    }
  | {
      kind: "donut";
      segments: { label: string; value: number; color: string }[];
      centerLabel?: string;
      ariaSummary: string;
    }
  | {
      kind: "range-selector";
      current: RangeId;
      options: { id: RangeId; label: string }[];
    }
  | { kind: "heading"; text: string }
  | { kind: "empty"; message: string };

interface CellUpdate {
  id: string;
  title: string;
  content: CellContent[];
}

declare function acquireVsCodeApi(): { postMessage(msg: unknown): void };

const vscode = acquireVsCodeApi();

const TABS = [
  { id: "dashboard", label: "Dashboard", cells: ["status", "consumption"] },
  {
    id: "benchmarks",
    label: "Benchmarks",
    cells: ["benchmarks", "recommendations"],
  },
  {
    id: "config",
    label: "Configuration",
    cells: [
      "apiKeys",
      "cfg-models",
      "cfg-desktop",
      "cfg-browser",
      "cfg-advanced",
      "cfg-display",
    ],
  },
] as const;

let activeTab: string = "dashboard";
const cellCache = new Map<string, CellUpdate>();

function post(type: string, data?: Record<string, string>): void {
  vscode.postMessage({ type, ...data });
}

function switchTab(tabId: string): void {
  activeTab = tabId;
  renderAll();
}

function TabBar(): Node {
  return (
    <div className="tab-bar">
      {TABS.map((tab) => (
        <button
          className={`tab ${tab.id === activeTab ? "tab-active" : ""}`}
          onClick={() => switchTab(tab.id)}
        >
          {tab.label}
        </button>
      ))}
      <button
        className="tab-reload"
        title="Reload panel (picks up rebuilt UI without reloading the window)"
        onClick={() => post("reloadPanel")}
      >
        ↻
      </button>
    </div>
  );
}

function ActionButton({ action }: { action: Action }): Node {
  return (
    <button
      className={action.style === "secondary" ? "secondary" : ""}
      onClick={() => post(action.type, action.data)}
    >
      {action.label}
    </button>
  );
}

function Row({ item }: { item: CellContent & { kind: "row" } }): Node {
  return (
    <div className="row" title={item.tooltip}>
      <span className="row-label">
        {item.dot && <span className={`dot dot-${item.dot}`} />}
        {item.label}
        {item.value && [" ", <span className="model-value">{item.value}</span>]}
      </span>
      {item.actions && (
        <span className="row-actions">
          {item.actions.map((a) => (
            <ActionButton action={a} />
          ))}
        </span>
      )}
    </div>
  );
}

function Table({ item }: { item: CellContent & { kind: "table" } }): Node {
  return (
    <table>
      <tr>
        {item.headers.map((h) => (
          <th>{h}</th>
        ))}
      </tr>
      {item.rows.map((row) => (
        <tr>
          {row.map((c) => (
            <td>{c}</td>
          ))}
        </tr>
      ))}
    </table>
  );
}

function Chart({ item }: { item: CellContent & { kind: "chart" } }): Node {
  const w = 300,
    h = 70,
    pad = 24;
  const maxY = Math.max(...item.points.map((p) => p.y), 0.001);
  const n = item.points.length;
  const xStep = n > 1 ? (w - pad * 2) / (n - 1) : 0;
  const prefix = item.yPrefix ?? "";

  const pts = item.points.map((p, i) => ({
    x: pad + i * xStep,
    y: h - pad - (p.y / maxY) * (h - pad * 2),
  }));

  const fillPoints = pts.length
    ? [{ x: pad, y: h - pad }, ...pts, { x: pad + (n - 1) * xStep, y: h - pad }]
    : [];

  return (
    <svg
      width="100%"
      viewBox={`0 0 ${w} ${h}`}
      preserveAspectRatio="xMidYMid meet"
      className="chart"
    >
      <defs>
        <linearGradient id="chartGrad" x1="0" y1="0" x2="0" y2="1">
          <stop
            offset="0%"
            stop-color="var(--vscode-charts-green, #4ec9b0)"
            stop-opacity="0.3"
          />
          <stop
            offset="100%"
            stop-color="var(--vscode-charts-green, #4ec9b0)"
            stop-opacity="0.02"
          />
        </linearGradient>
      </defs>
      {fillPoints.length > 0 && (
        <polygon
          className="chart-fill"
          points={fillPoints.map((p) => `${p.x},${p.y}`).join(" ")}
        />
      )}
      <line x1={pad} y1={h - pad} x2={w - pad} y2={h - pad} className="axis" />
      <text
        x={pad - 4}
        y={h - pad + 3}
        text-anchor="end"
        className="chart-label"
      >
        {prefix}0
      </text>
      <text x={pad - 4} y={pad + 3} text-anchor="end" className="chart-label">
        {prefix}
        {maxY.toFixed(2)}
      </text>
      {pts.length > 0 && (
        <polyline
          className="line"
          points={pts.map((p) => `${p.x},${p.y}`).join(" ")}
        />
      )}
      {pts.map((p) => (
        <circle className="point" cx={p.x} cy={p.y} r={2} />
      ))}
      {n > 0 && (
        <text x={pad} y={h - 6} text-anchor="start" className="chart-label">
          {item.points[0].x}
        </text>
      )}
      {n > 1 && (
        <text
          x={pad + (n - 1) * xStep}
          y={h - 6}
          text-anchor="end"
          className="chart-label"
        >
          {item.points[n - 1].x}
        </text>
      )}
    </svg>
  );
}

function BarH({ item }: { item: CellContent & { kind: "bar-h" } }): Node {
  const w = 320,
    rowH = 18,
    gap = 4,
    pad = 4;
  const n = item.bars.length;
  const h = Math.max(rowH, n * (rowH + gap));
  const maxV = Math.max(...item.bars.map((b) => b.value), 0.0001);
  const labelW = 0; // labels rendered inside bar for compactness
  const prefix = item.valuePrefix ?? "";
  return (
    <svg
      role="img"
      aria-label={item.ariaSummary}
      width="100%"
      viewBox={`0 0 ${w} ${h}`}
      className="bar-h"
    >
      <title>{item.ariaSummary}</title>
      {item.bars.map((b, i) => {
        const y = i * (rowH + gap);
        const bw = ((w - pad * 2 - labelW) * b.value) / maxV;
        return (
          <g>
            <rect
              x={pad + labelW}
              y={y}
              width={Math.max(1, bw)}
              height={rowH - 2}
              rx="2"
              className="bar-h-fill"
              style={{ fill: b.color ?? "var(--vscode-charts-blue)" }}
            />
            <text x={pad + labelW + 6} y={y + rowH - 7} className="bar-h-label">
              {b.label}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function StackedBar({
  item,
}: {
  item: CellContent & { kind: "stacked-bar" };
}): Node {
  const w = 360,
    h = 140,
    padL = 32,
    padR = 8,
    padT = 8,
    padB = 22;
  const innerW = w - padL - padR;
  const innerH = h - padT - padB;
  const n = item.xLabels.length;
  const barW = n > 0 ? (innerW / n) * 0.7 : 0;
  const xStep = n > 0 ? innerW / n : 0;
  const totals = item.xLabels.map((_, i) =>
    item.series.reduce((s, ser) => s + (ser.values[i] ?? 0), 0),
  );
  const maxY = Math.max(...totals, 0.0001);
  const prefix = item.valuePrefix ?? "";
  const tickCount = 4;

  return (
    <svg
      role="img"
      aria-label={item.ariaSummary}
      width="100%"
      viewBox={`0 0 ${w} ${h}`}
      className="stacked-bar"
    >
      <title>{item.ariaSummary}</title>
      {Array.from({ length: tickCount + 1 }).map((_, i) => {
        const v = (maxY * i) / tickCount;
        const y = padT + innerH - (innerH * i) / tickCount;
        return (
          <g>
            <line
              x1={padL}
              x2={padL + innerW}
              y1={y}
              y2={y}
              className="axis-grid"
            />
            <text
              x={padL - 4}
              y={y + 3}
              text-anchor="end"
              className="chart-label"
            >
              {prefix}
              {v.toFixed(2)}
            </text>
          </g>
        );
      })}
      {item.xLabels.map((lbl, i) => {
        const x = padL + i * xStep + (xStep - barW) / 2;
        let cumY = padT + innerH;
        return (
          <g>
            {item.series.map((ser) => {
              const v = ser.values[i] ?? 0;
              const segH = (v / maxY) * innerH;
              cumY -= segH;
              return (
                <rect
                  x={x}
                  y={cumY}
                  width={barW}
                  height={Math.max(0, segH)}
                  style={{ fill: ser.color }}
                />
              );
            })}
            {(i === 0 ||
              i === n - 1 ||
              i % Math.max(1, Math.floor(n / 6)) === 0) && (
              <text
                x={x + barW / 2}
                y={padT + innerH + 12}
                text-anchor="middle"
                className="chart-label"
              >
                {lbl}
              </text>
            )}
          </g>
        );
      })}
      {/* Legend */}
      {item.series.map((ser, i) => (
        <g>
          <rect
            x={padL + i * 70}
            y={0}
            width={8}
            height={8}
            style={{ fill: ser.color }}
          />
          <text x={padL + i * 70 + 12} y={7} className="chart-label">
            {ser.name.length > 10 ? ser.name.slice(0, 10) + "…" : ser.name}
          </text>
        </g>
      ))}
    </svg>
  );
}

function SmallMultiples({
  item,
}: {
  item: CellContent & { kind: "small-multiples" };
}): Node {
  return (
    <div
      className="small-multiples"
      role="img"
      aria-label={item.ariaSummary}
      title={item.ariaSummary}
    >
      {item.panels.map((p) => {
        const maxV = Math.max(...p.bars.map((b) => b.value), 0.0001);
        const w = 100,
          h = 60,
          padT = 14,
          padB = 12;
        const innerH = h - padT - padB;
        const barW = 30;
        const gap = 8;
        const startX =
          (w - (p.bars.length * barW + (p.bars.length - 1) * gap)) / 2;
        return (
          <div className="panel">
            <div className="panel-title" title={p.title}>
              {p.title}
            </div>
            <svg width="100%" viewBox={`0 0 ${w} ${h}`} className="panel-chart">
              {p.bars.map((b, i) => {
                const barH = (b.value / maxV) * innerH;
                const x = startX + i * (barW + gap);
                const y = padT + (innerH - barH);
                return (
                  <g>
                    <rect
                      x={x}
                      y={y}
                      width={barW}
                      height={Math.max(1, barH)}
                      rx="1"
                      style={{ fill: b.color }}
                    />
                    <text
                      x={x + barW / 2}
                      y={y - 2}
                      text-anchor="middle"
                      className="chart-label"
                    >
                      {b.value > 1000
                        ? `${(b.value / 1000).toFixed(1)}k`
                        : b.value}
                    </text>
                    <text
                      x={x + barW / 2}
                      y={h - 2}
                      text-anchor="middle"
                      className="chart-label"
                    >
                      {b.label}
                    </text>
                  </g>
                );
              })}
            </svg>
          </div>
        );
      })}
    </div>
  );
}

function donutPath(
  cx: number,
  cy: number,
  rOuter: number,
  rInner: number,
  a0: number,
  a1: number,
): string {
  const large = a1 - a0 > Math.PI ? 1 : 0;
  const x0 = cx + rOuter * Math.cos(a0);
  const y0 = cy + rOuter * Math.sin(a0);
  const x1 = cx + rOuter * Math.cos(a1);
  const y1 = cy + rOuter * Math.sin(a1);
  const xi1 = cx + rInner * Math.cos(a1);
  const yi1 = cy + rInner * Math.sin(a1);
  const xi0 = cx + rInner * Math.cos(a0);
  const yi0 = cy + rInner * Math.sin(a0);
  return [
    `M ${x0} ${y0}`,
    `A ${rOuter} ${rOuter} 0 ${large} 1 ${x1} ${y1}`,
    `L ${xi1} ${yi1}`,
    `A ${rInner} ${rInner} 0 ${large} 0 ${xi0} ${yi0}`,
    "Z",
  ].join(" ");
}

function Donut({ item }: { item: CellContent & { kind: "donut" } }): Node {
  const total = item.segments.reduce((s, x) => s + x.value, 0);
  const w = 320,
    h = 160;
  const cx = 80,
    cy = h / 2;
  const rOuter = 60,
    rInner = 36;
  let acc = -Math.PI / 2;
  return (
    <svg
      role="img"
      aria-label={item.ariaSummary}
      width="100%"
      viewBox={`0 0 ${w} ${h}`}
      className="donut"
    >
      <title>{item.ariaSummary}</title>
      {total === 0 ? (
        <circle cx={cx} cy={cy} r={rOuter} className="donut-empty" />
      ) : (
        item.segments.map((seg) => {
          const a0 = acc;
          const a1 = acc + (seg.value / total) * Math.PI * 2;
          acc = a1;
          return (
            <path
              d={donutPath(cx, cy, rOuter, rInner, a0, a1)}
              style={{ fill: seg.color }}
              className="donut-segment"
            />
          );
        })
      )}
      {item.centerLabel && (
        <text x={cx} y={cy + 4} text-anchor="middle" className="donut-center">
          {item.centerLabel}
        </text>
      )}
      {/* Legend */}
      {item.segments.map((seg, i) => {
        const ly = 18 + i * 16;
        const pct = total > 0 ? ((seg.value / total) * 100).toFixed(0) : "0";
        const label =
          seg.label.length > 24 ? seg.label.slice(0, 24) + "…" : seg.label;
        return (
          <g>
            <rect
              x={170}
              y={ly - 8}
              width={10}
              height={10}
              style={{ fill: seg.color }}
            />
            <text x={186} y={ly} className="chart-label">
              {label} — {seg.value} ({pct}%)
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function RangeSelector({
  item,
}: {
  item: CellContent & { kind: "range-selector" };
}): Node {
  return (
    <div className="range-selector" role="radiogroup" aria-label="Time range">
      {item.options.map((opt) => (
        <button
          className={`range-btn ${opt.id === item.current ? "range-active" : ""}`}
          onClick={() => post("setRange", { range: opt.id })}
          aria-pressed={opt.id === item.current ? "true" : "false"}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}

function renderContent(item: CellContent): Node | null {
  switch (item.kind) {
    case "row":
      return <Row item={item} />;
    case "table":
      return <Table item={item} />;
    case "chart":
      return <Chart item={item} />;
    case "bar-h":
      return <BarH item={item} />;
    case "stacked-bar":
      return <StackedBar item={item} />;
    case "small-multiples":
      return <SmallMultiples item={item} />;
    case "donut":
      return <Donut item={item} />;
    case "range-selector":
      return <RangeSelector item={item} />;
    case "heading":
      return <div className="group-heading">{item.text}</div>;
    case "empty":
      return <div className="empty">{item.message}</div>;
  }
}

function buildCellContent(cellId: string, container: Element): void {
  const cell = cellCache.get(cellId);
  if (cell) {
    container.appendChild(<div className="cell-title">{cell.title}</div>);
    for (const item of cell.content) {
      const rendered = renderContent(item);
      if (rendered) container.appendChild(rendered);
    }
  }
}

function renderCell(cellId: string): void {
  const el = document.getElementById(`cell-${cellId}`);
  if (!el) {
    renderAll();
    return;
  }
  el.replaceChildren();
  buildCellContent(cellId, el);
}

function renderAll(): void {
  const root = document.getElementById("root")!;
  root.replaceChildren();
  root.appendChild(<TabBar />);

  const tab = TABS.find((t) => t.id === activeTab)!;
  const content = document.createElement("div");
  content.className = "tab-content";

  for (const cellId of tab.cells) {
    const container = <div id={`cell-${cellId}`} className="cell" />;
    buildCellContent(cellId, container);
    content.appendChild(container);
  }

  root.appendChild(content);
}

function mount(): void {
  renderAll();

  window.addEventListener("message", (e: MessageEvent) => {
    const { type, cell } = e.data;
    if (type !== "cellUpdate") return;
    const update = cell as CellUpdate;
    cellCache.set(update.id, update);
    const tab = TABS.find((t) => t.id === activeTab)!;
    if ((tab.cells as readonly string[]).includes(update.id)) {
      renderCell(update.id);
    }
  });

  let resizeTimer: number | undefined;
  window.addEventListener("resize", () => {
    if (resizeTimer != null) window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => renderAll(), 100);
  });

  setTimeout(() => vscode.postMessage({ type: "ready" }), 100);
}

mount();
