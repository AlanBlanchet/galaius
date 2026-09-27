// The webview↔host wire types come from shared.ts — ONE declaration, so a change to a cell kind
// can't compile on one side and silently mismatch on the other. `import type` is erased at build
// time, so this pulls no extension-host runtime code (esbuild bundles the webview separately).
import type {
  Action,
  RangeId,
  CellContent,
  CellUpdate,
  AgentLane,
  AgentGroupBy,
  AgentStatus,
} from "../src/shared";
import { PROMPT_ACTIONS } from "../src/promptActions";
import { amongYours, shortCompetence } from "../src/competence";

declare function acquireVsCodeApi(): { postMessage(msg: unknown): void };

const vscode = acquireVsCodeApi();

const TABS = [
  { id: "dashboard", label: "Dashboard", cells: ["status", "agents", "comparison", "models", "consumption"] },
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
      "cfg-benchmarks",
      "cfg-display",
    ],
  },
  { id: "prompts", label: "Prompts", cells: ["prompt-workspace"] },
] as const;

let activeTab: string = "dashboard";
const cellCache = new Map<string, CellUpdate>();
let promptInputGeneration = 0;
let pendingPromptAction: { kind: "reload" | "save"; generation: number } | null = null;

function post(type: string, data?: Record<string, unknown>): void {
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

function SettingEditor({ item }: { item: CellContent & { kind: "setting" } }): Node {
  const save = (event: Event): void => {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    const control = form.elements.namedItem("value") as HTMLInputElement | HTMLSelectElement;
    post("saveSetting", { setting: item.key, value: control.value });
  };
  const input = item.input === "bool" || item.input === "enum"
    ? <select name="value" aria-label={item.label}>
        {(item.input === "bool" ? [{ label: "On", value: "true" }, { label: "Off", value: "false" }]
          : item.options ?? []).map((option) =>
          <option value={option.value} selected={option.value === item.value || undefined}>{option.label}</option>) }
      </select>
    : <input name="value" type={item.input === "int" ? "number" : "text"}
        value={item.value} placeholder={item.defaultValue} aria-label={item.label}
        min={item.minimum} step={item.input === "int" ? 1 : undefined} pattern={item.pattern}
        onInvalid={(event: Event) => (event.currentTarget as HTMLInputElement).setAttribute("aria-invalid", "true")}
        onInput={(event: Event) => {
          const control = event.currentTarget as HTMLInputElement;
          if (control.validity.valid) control.removeAttribute("aria-invalid");
        }} />;
  return <form className="setting-editor" title={item.description} onSubmit={save}>
    <label>{item.label}{input}</label>
    <span className="row-actions">
      <button type="submit">Save</button>
      <button type="reset" className="secondary">Cancel</button>
    </span>
  </form>;
}

function PromptWorkspace({ item }: { item: CellContent & { kind: "prompt-workspace" } }): Node {
  const send = (type: string, data: Record<string, unknown>): void => post(type, data);
  return <section className="prompt-workspace" data-selected={item.selected ?? ""} data-digest={item.digest ?? ""}
    data-revision={item.revision ?? 0}>
    <p>Editing the canonical personal Git source. Installed projections, product defaults, and runtime system prompts are read-only and are not listed here.</p>
    <label>Prompt file<select aria-label="Prompt file" onChange={(event: Event) =>
      send("promptSelect", { path: (event.currentTarget as HTMLSelectElement).value })}>
      {item.files.map((file) => <option value={file} selected={file === item.selected || undefined}>{file}</option>)}
    </select></label>
    <textarea id="prompt-buffer" aria-label="Prompt content" onInput={() => { promptInputGeneration += 1; }}>
      {item.content ?? ""}
    </textarea>
    <input id="prompt-commit-message" aria-label="Prompt commit message" placeholder="Commit message" />
    <label>Publication service endpoint<input id="prompt-publish-endpoint" type="url"
      placeholder="https://prompts.example" aria-label="Publication service endpoint" /></label>
    <label>Private token file<input id="prompt-publish-token-file" type="text"
      placeholder="/path/to/private-token" aria-label="Private token file read by Interact" /></label>
    {item.status && <p role="status">{item.status}</p>}
    <div className="row-actions">
      <button onClick={(event: Event) => {
        pendingPromptAction = { kind: "save", generation: promptInputGeneration };
        send("promptSave", { path: item.selected,
        digest: (event.currentTarget as HTMLButtonElement).closest<HTMLElement>(".prompt-workspace")?.dataset.digest,
        content: (document.getElementById("prompt-buffer") as HTMLTextAreaElement).value });
      }}>Save</button>
      <button className="secondary" onClick={() => {
        pendingPromptAction = { kind: "reload", generation: promptInputGeneration };
        send("promptSelect", { path: item.selected });
      }}>
        Reload from disk
      </button>
      {PROMPT_ACTIONS.map((action) => <button className="secondary" onClick={() =>
        send("promptAction", { action: action.id, explicit: action.remote,
          message: (document.getElementById("prompt-commit-message") as HTMLInputElement).value,
          endpoint: action.id === "publish"
            ? (document.getElementById("prompt-publish-endpoint") as HTMLInputElement).value : undefined,
          tokenFile: action.id === "publish"
            ? (document.getElementById("prompt-publish-token-file") as HTMLInputElement).value : undefined })}>{action.id}</button>)}
    </div>
  </section>;
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

/**
 * Horizontal bars in plain DOM, NOT a scaled SVG.
 *
 * The old version drew a 320-unit viewBox at `width="100%"`: stretched into an ~800px column that
 * is a 2.5x scale on everything inside it, which is how a 10px label became a 28px word sitting
 * inside a chunky slab — and it printed no number at all. Here the label is a real 12px label in
 * its own column, the bar is a percentage-width div, and the VALUE is printed at the end where it
 * can be read.
 */
function BarH({ item }: { item: CellContent & { kind: "bar-h" } }): Node {
  const maxV = Math.max(...item.bars.map((b) => b.value), 0.0001);
  const totalV = item.bars.reduce((s, b) => s + b.value, 0);
  const prefix = item.valuePrefix ?? "";
  const fmt = (v: number) =>
    `${prefix}${v >= 100 ? v.toFixed(0) : v >= 1 ? v.toFixed(2) : v.toFixed(3)}`;
  return (
    <div className="barlist" role="img" aria-label={item.ariaSummary} title={item.ariaSummary}>
      {item.bars.map((b) => (
        <div className="barlist-row">
          <span className="barlist-label" title={b.label}>
            {b.label}
          </span>
          <span className="barlist-track">
            <span
              className="barlist-fill"
              style={{
                width: `${Math.max(1.5, (b.value / maxV) * 100)}%`,
                background: b.color ?? "var(--vscode-charts-blue)",
              }}
            />
          </span>
          <span className="barlist-value">
            {fmt(b.value)}
            <span className="barlist-pct">
              {totalV > 0 ? `${((b.value / totalV) * 100).toFixed(0)}%` : "—"}
            </span>
          </span>
        </div>
      ))}
    </div>
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

/** Ring in a FIXED-size SVG (so nothing inside it is scaled up) with the legend as real HTML
 *  text — same fix as BarH, applied to the other chart that carried scaled labels. */
function Donut({ item }: { item: CellContent & { kind: "donut" } }): Node {
  const total = item.segments.reduce((s, x) => s + x.value, 0);
  const size = 132;
  const cx = size / 2,
    cy = size / 2;
  const rOuter = 62,
    rInner = 40;
  let acc = -Math.PI / 2;
  return (
    <div className="donut-wrap" role="img" aria-label={item.ariaSummary} title={item.ariaSummary}>
      <div className="donut-ring">
        <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="donut">
          {total === 0 ? (
            <circle cx={cx} cy={cy} r={rOuter - 1} className="donut-empty" />
          ) : (
            item.segments.map((seg) => {
              const a0 = acc;
              const a1 = acc + (seg.value / total) * Math.PI * 2;
              acc = a1;
              return (
                <path
                  d={donutPath(cx, cy, rOuter - 1, rInner, a0, a1)}
                  style={{ fill: seg.color }}
                  className="donut-segment"
                />
              );
            })
          )}
        </svg>
        {item.centerLabel && <span className="donut-center">{item.centerLabel}</span>}
      </div>
      <div className="donut-legend">
        {item.segments.map((seg) => (
          <div className="legend-row">
            <span className="legend-swatch" style={{ background: seg.color }} />
            <span className="legend-label" title={seg.label}>
              {seg.label}
            </span>
            <span className="legend-value">
              {String(seg.value)}
              <span className="legend-pct">
                {total > 0 ? `${((seg.value / total) * 100).toFixed(0)}%` : "—"}
              </span>
            </span>
          </div>
        ))}
      </div>
    </div>
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

/* ------------------------------------------------------------------ agent board
 *
 * "Flight deck": an agent is not a table row, it is an INTERVAL on a clock every other agent
 * shares. So the lane row IS the timeline row — identity on the left, the run's bar on the shared
 * axis on the right — and overlap, duration and gaps are readable without a second chart. The
 * concurrency ribbon above the lanes is the same axis integrated: how many were working at once.
 *
 * Everything positional is percentage-of-window on plain DOM elements, never an SVG scaled by
 * `width="100%"` — that is exactly what turned the old bars into slabs with 28px labels (a 320-unit
 * viewBox stretched to ~800px scales its 10px text to 25px). The two SVGs here (the ribbon and the
 * status glyphs) hold NO text: the ribbon stretches with `preserveAspectRatio="none"` and keeps its
 * stroke via `vector-effect`, and the glyphs are fixed-size.
 */

const MINUTE = 60_000;
const HOUR = 3_600_000;

/** Distinct SHAPE per status, not merely a distinct colour — "running vs done vs crashed must not
 *  look alike" has to survive a colour-blind reader and a grayscale screenshot. */
/** Status glyphs, as FACTORIES — never shared nodes.
 *
 *  This JSX runtime builds real DOM nodes, and a DOM node can only exist at one place in the
 *  tree: appending the same node to a second row MOVES it out of the first. Held as module-level
 *  constants, one row kept its glyph and every other row of the same status rendered an empty
 *  <svg> — so "running" and "done" were indistinguishable in grayscale, which is precisely the
 *  claim this shape-coding makes. Each call returns fresh nodes.
 */
const STATUS_GLYPH: Record<AgentStatus, () => (Node | null)[]> = {
  starting: () => [
    <circle cx="7" cy="7" r="5.4" className="g-stroke" fill="none" stroke-dasharray="1.5 1.5" />,
    <circle cx="7" cy="7" r="1.5" className="g-fill" />,
  ],
  running: () => [
    <circle cx="7" cy="7" r="5.4" className="g-stroke" fill="none" />,
    <circle cx="7" cy="7" r="2.4" className="g-fill" />,
  ],
  done: () => [
    <circle cx="7" cy="7" r="5.4" className="g-stroke" fill="none" />,
    <path d="M4.2 7.1 L6.2 9.1 L9.9 5" className="g-stroke" fill="none" />,
  ],
  waiting: () => [
    <circle cx="7" cy="7" r="5.4" className="g-stroke" fill="none" />,
    <path d="M5.3 4.4 V9.6 M8.7 4.4 V9.6" className="g-stroke" fill="none" />,
  ],
  failed: () => [
    <path d="M7 1.3 L13 11.9 L1 11.9 Z" className="g-stroke" fill="none" />,
    <path d="M7 5.2 V8" className="g-stroke" fill="none" />,
    <circle cx="7" cy="10" r="0.85" className="g-fill" />,
  ],
  crashed: () => [
    <circle cx="7" cy="7" r="5.4" className="g-stroke" fill="none" />,
    <path d="M4.6 4.6 L9.4 9.4 M9.4 4.6 L4.6 9.4" className="g-stroke" fill="none" />,
  ],
  stopped: () => [<rect x="2.6" y="2.6" width="8.8" height="8.8" rx="1.6" className="g-fill" />],
  cancelled: () => [
    <path d="M3 3 L11 11 M11 3 L3 11" className="g-stroke" fill="none" />,
    <circle cx="7" cy="7" r="5.4" className="g-stroke" fill="none" />,
  ],
  foreign: () => [
    <circle
      cx="7"
      cy="7"
      r="5.4"
      className="g-stroke"
      fill="none"
      stroke-dasharray="2.2 2.2"
    />,
  ],
  // Declared in the company, never asked: an empty seat — the outline of a circle with a dot,
  // present but quiet. The board never receives one today; the Record is total on purpose.
  declared: () => [
    <circle cx="7" cy="7" r="5.4" className="g-stroke" fill="none" stroke-dasharray="1 2.4" />,
    <circle cx="7" cy="7" r="1" className="g-fill" />,
  ],
};

const STATUS_LABEL: Record<AgentStatus, string> = {
  starting: "starting",
  running: "running",
  waiting: "waiting",
  done: "done",
  failed: "failed",
  crashed: "crashed",
  stopped: "stopped",
  cancelled: "cancelled",
  foreign: "foreign",
  declared: "ready",
};

/** Provider → one of VS Code's own chart hues, so a cross-provider team is legible in both themes
 *  without a single hard-coded hex. Unknown providers hash into the same palette. */
const CHART_HUES = [
  "var(--vscode-charts-blue)",
  "var(--vscode-charts-purple)",
  "var(--vscode-charts-green)",
  "var(--vscode-charts-orange)",
  "var(--vscode-charts-yellow)",
  "var(--vscode-charts-red)",
];
const PINNED_HUE: Record<string, string> = {
  claude: "var(--vscode-charts-orange)",
  anthropic: "var(--vscode-charts-orange)",
  codex: "var(--vscode-charts-green)",
  openai: "var(--vscode-charts-green)",
  gemini: "var(--vscode-charts-blue)",
  google: "var(--vscode-charts-blue)",
  zai: "var(--vscode-charts-purple)",
  glm: "var(--vscode-charts-purple)",
};

function providerHue(name: string): string {
  const key = name.toLowerCase();
  if (PINNED_HUE[key]) return PINNED_HUE[key];
  let h = 0;
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) >>> 0;
  return CHART_HUES[h % CHART_HUES.length];
}

function fmtDuration(ms: number): string {
  if (!isFinite(ms) || ms < 0) return "—";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${String(m % 60).padStart(2, "0")}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

/** Reported or estimated USD value. `null` is UNKNOWN and renders an em dash: "$0.00" would claim
 *  the run was free, which is a different fact. The charge path beside it owns its meaning.
 *
 *  One fixed precision down the whole column, so tabular figures line up on the decimal instead of
 *  switching shape between "~$1.94" and "~$0.6102" two rows apart. */
function fmtCost(usd: number | null | undefined, certainty: AgentLane["costCertainty"]): string {
  if (usd == null) return "—";
  return `${certainty === "known" ? "" : "~"}$${usd >= 100 ? usd.toFixed(2) : usd.toFixed(4)}`;
}

/** When a lane's interval ends, or `null` when nothing on disk says. A crash the registry never
 *  got to stamp has no end — the board draws that ignorance instead of inventing a duration. */
function laneEnd(lane: AgentLane, now: number): number | null {
  if (lane.status === "running") return now;
  return lane.endedAt ?? null;
}

function groupKeyOf(lane: AgentLane, by: AgentGroupBy): string {
  if (by === "project") return lane.project || "(no project)";
  if (by === "provider") return lane.provider || "(unknown)";
  if (by === "model") return lane.resolvedModel || lane.model || "(model not recorded)";
  return "all";
}

interface LaneGroup {
  key: string;
  lanes: AgentLane[];
  running: number;
  span: number;
}

function buildGroups(lanes: AgentLane[], by: AgentGroupBy, now: number): LaneGroup[] {
  const map = new Map<string, AgentLane[]>();
  for (const lane of lanes) {
    const key = groupKeyOf(lane, by);
    (map.get(key) ?? map.set(key, []).get(key)!).push(lane);
  }
  const groups = [...map.entries()].map(([key, group]) => {
    const ends = group.map((l) => laneEnd(l, now)).filter((e): e is number => e != null);
    const starts = group.map((l) => l.startedAt);
    return {
      key,
      lanes: group,
      running: group.filter((l) => l.status === "running").length,
      span: ends.length ? Math.max(...ends) - Math.min(...starts) : 0,
    };
  });
  // Live work first — a supervisor looks for what is moving, not for the alphabet.
  return groups.sort(
    (a, b) => b.running - a.running || b.lanes.length - a.lanes.length || a.key.localeCompare(b.key),
  );
}

/** Tick step chosen so the axis carries ~4-6 labels whatever the window is (seconds to days). */
const TICK_STEPS = [
  5_000, 15_000, 30_000, MINUTE, 2 * MINUTE, 5 * MINUTE, 10 * MINUTE, 15 * MINUTE, 30 * MINUTE,
  HOUR, 2 * HOUR, 6 * HOUR, 12 * HOUR, 24 * HOUR, 7 * 24 * HOUR,
];

function tickStep(span: number): number {
  return TICK_STEPS.find((s) => span / s <= 6) ?? TICK_STEPS[TICK_STEPS.length - 1];
}

/** Time is shown RELATIVE to now ("-5m"), because supervision asks "how long ago", never "at what
 *  wall-clock instant".
 *
 *  The sub-unit is kept whenever it is non-zero: rounding to whole minutes on a three-minute window
 *  printed "-2m, -2m, -3m, -3m" across four distinct ticks, which reads as a broken axis. */
function relLabel(msAgo: number): string {
  if (msAgo < 1000) return "now";
  const s = Math.round(msAgo / 1000);
  if (s < 60) return `-${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return s % 60 ? `-${m}m${s % 60}s` : `-${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `-${h}h${m % 60}m` : `-${h}h`;
  const d = Math.floor(h / 24);
  return h % 24 ? `-${d}d${h % 24}h` : `-${d}d`;
}

/** Which groups the reader folded away. Local to the webview: collapsing is a viewing gesture, not
 *  a fact the host owns, so it costs no round-trip. */
const collapsed = new Set<string>();

interface Axis {
  start: number;
  end: number;
  now: number;
  pct: (t: number) => number;
  ticks: { pct: number; label: string }[];
}

/** The verticals every lane, the ribbon and the axis share — drawn from the SAME tick percentages
 *  as the labels, so alignment is exact by construction rather than by gradient arithmetic. This
 *  common grid is what makes a stack of bars read as ONE clock instead of N unrelated rows. */
function gridLines(axis: Axis): Node[] {
  return axis.ticks.map((t) => <span className="grid-line" style={{ left: `${t.pct}%` }} />);
}

function buildAxis(windowStart: number, now: number): Axis {
  // 6% of headroom past `now` keeps the live edge, its glow and the now-line inside the box.
  const rawSpan = Math.max(1000, now - windowStart);
  const start = windowStart;
  const end = now + rawSpan * 0.06;
  const span = end - start;
  const pct = (t: number) => ((t - start) / span) * 100;

  // Ticks walk backwards from `now`, so "how long ago" reads off round numbers.
  const step = tickStep(rawSpan);
  const ticks: { pct: number; label: string }[] = [];
  for (let t = now; t >= start - 1 && ticks.length < 9; t -= step) {
    ticks.push({ pct: pct(t), label: relLabel(now - t) });
  }
  return { start, end, now, pct, ticks };
}

/** The step function "how many agents were running at time t" — the read that turns a list into a
 *  TEAM. Lanes whose end was never recorded are excluded and counted separately rather than
 *  guessed, so the peak is a floor, never an invention. */
function ConcurrencyRibbon({ lanes, axis }: { lanes: AgentLane[]; axis: Axis }): Node {
  const events: { t: number; d: number }[] = [];
  let unknown = 0;
  for (const lane of lanes) {
    if (lane.status === "running") {
      // Still going: it opens and never closes inside the window, so the curve stays elevated all
      // the way to `now`. Closing it at `now` would draw every live agent finishing this instant.
      events.push({ t: lane.startedAt, d: 1 });
      continue;
    }
    if (lane.endedAt == null) {
      unknown++;
      continue;
    }
    events.push({ t: lane.startedAt, d: 1 }, { t: lane.endedAt, d: -1 });
  }
  // Ends before starts at the same instant: a hand-off must not read as an overlap.
  events.sort((a, b) => a.t - b.t || a.d - b.d);

  const steps: { t: number; n: number }[] = [];
  let n = 0;
  for (const e of events) {
    n += e.d;
    steps.push({ t: e.t, n });
  }
  const peak = steps.reduce((m, s) => Math.max(m, s.n), 0);
  const H = 100;
  const top = 6;
  // Scale against at least four lanes: normalising to a peak of 1 made a single agent fill the
  // whole band, which reads as a crisis. Past four it normalises as usual, and the printed peak
  // carries the exact number either way.
  const denom = Math.max(peak, 4);
  const y = (v: number) => H - (v / denom) * (H - top);

  // The curve stops at `now` — the headroom to its right is the future, and drawing into it would
  // assert concurrency nobody has observed yet.
  const xNow = axis.pct(axis.now) * 10;
  const cmds: string[] = [];
  let prevY = y(0);
  cmds.push(`M 0 ${prevY}`);
  for (const s of steps) {
    const x = Math.max(0, Math.min(xNow, axis.pct(s.t) * 10));
    cmds.push(`L ${x} ${prevY}`, `L ${x} ${y(s.n)}`);
    prevY = y(s.n);
  }
  cmds.push(`L ${xNow} ${prevY}`);
  const line = cmds.join(" ");
  const area = `${line} L ${xNow} ${H} L 0 ${H} Z`;

  return (
    <div className="board-row board-ribbon-row">
      <div className="board-aside">
        <div className="aside-kicker">Concurrency</div>
        <div className="aside-peak">
          <strong>{String(peak)}</strong> peak
          {unknown > 0 && <span className="aside-note"> · {String(unknown)} unknown end</span>}
        </div>
      </div>
      {/* The column says WHAT it is. "Spell out their intelligence score" was the ask, and the
          column carried bare numbers with the metric's name nowhere on the surface — only inside
          a hover title, which at rest is nowhere at all. Split so BOTH halves fit the column: one
          line needed 197px in a 118px cell, and the user's own noun leads the second. */}
      <div className="board-score-col score-head"
           title="Artificial Analysis intelligence index — one measure, never a verdict">
        <span className="score-head-name">Intelligence score</span>
        <span className="score-head-hedge">Artificial Analysis · not a verdict</span>
      </div>
      <div className="lane-track ribbon-track">
        {gridLines(axis)}
        <svg
          className="ribbon"
          viewBox={`0 0 1000 ${H}`}
          preserveAspectRatio="none"
          role="img"
          aria-label={`Peak ${peak} agents running at once`}
        >
          <defs>
            <linearGradient id="ribbonGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="var(--vscode-charts-blue)" stop-opacity="0.42" />
              <stop offset="100%" stop-color="var(--vscode-charts-blue)" stop-opacity="0.03" />
            </linearGradient>
          </defs>
          <path d={area} className="ribbon-area" />
          <path d={line} className="ribbon-line" vector-effect="non-scaling-stroke" />
        </svg>
        <div className="now-line" style={{ left: `${axis.pct(axis.now)}%` }} />
      </div>
      <div className="board-meta-col" />
    </div>
  );
}

/** One agent: identity, its interval on the shared clock, its numbers. */
function Lane({
  lane,
  axis,
  threads,
  localDepth,
  externalParent,
  fleet,
}: {
  lane: AgentLane;
  axis: Axis;
  threads: { x: number; kind: "start" | "through" | "end"; hue: string }[];
  localDepth: number;
  externalParent?: string;
  /** Every lane on the board, not just this group's — the rank a row shows is against the models
   *  the COMPANY runs, and splitting by model would otherwise give each group a field of one. */
  fleet: AgentLane[];
}): Node {
  const end = laneEnd(lane, axis.now);
  const unknownEnd = end == null;
  const left = axis.pct(lane.startedAt);
  // An unknown end still has a KNOWN start, and a 3px stub hides it. So the bar keeps a legible
  // stretch and then dissolves (see .bar-unknown-end): "began here, ended at some point after —
  // nothing recorded when". The fade is the claim, the length is not.
  const width = unknownEnd
    ? Math.max(7, (100 - left) * 0.35)
    : Math.max(0.4, axis.pct(end) - left);
  const hue = providerHue(lane.provider);
  const elapsed = unknownEnd ? "—" : fmtDuration(end - lane.startedAt);

  const tooltip = [
    lane.name,
    lane.cwd,
    unknownEnd && lane.status !== "running" ? "end time was never recorded" : "",
    lane.task,
    `billing: ${lane.chargePath} · ${lane.costCertainty}`,
  ]
    .filter(Boolean)
    .join("\n");

  return (
    <div className={`board-row lane lane-${lane.status}`} title={tooltip}>
      <div className="board-aside lane-id">
        {/* Indent per ancestry level, with ONE connector hooking into the parent above. Drawing a
            full multi-level rail needs per-ancestor "has a later sibling" state, and a rail that
            guesses wrong renders as disconnected fragments — the lineage is carried properly by
            the threads on the track, so this stays an indent plus one honest hook. */}
        {localDepth > 0 && (
          <span className="rails" style={{ width: `${localDepth * 11}px` }}>
            <span className="rail-hook" />
          </span>
        )}
        <svg className="glyph" width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
          {STATUS_GLYPH[lane.status]()}
        </svg>
        <span className="lane-id-text">
          <span className="lane-name-line">
            <span className="lane-name">{lane.name}</span>
            {externalParent && (
              <span className="ext-parent" title={`Spawned by ${externalParent}`}>
                ⤴ {externalParent}
              </span>
            )}
            {/* The hue identifies the provider on the BORDER and dot, never on the text. VS
                Code's chart colours are tuned as chart fills: used as 10px text they measured
                2.77:1 on the light theme's white, under the 4.5:1 floor. The label inherits the
                theme foreground, so it is legible in both themes while the colour still reads. */}
            <span className="chip chip-provider" style={{ borderColor: hue }}>
              <span className="chip-dot" style={{ background: hue }} />
              {lane.provider}
            </span>
            {/* The RESOLVED spelling: one model under two provider prefixes read as two
                colleagues, inside a group whose own header had already resolved them to one. */}
            {(lane.resolvedModel || lane.model) && (
              <span className="chip chip-model"
                    title={lane.resolvedModel && lane.resolvedModel !== lane.model
                      ? `${lane.resolvedModel} — served as ${lane.model}`
                      : lane.model}>
                {lane.resolvedModel || lane.model}
              </span>
            )}
          </span>
          <span className="lane-doing">
            {lane.status === "running" && <span className="doing-pip" />}
            <span className="doing-text">
              {lane.last || (lane.status === "running" ? "working…" : STATUS_LABEL[lane.status])}
            </span>
          </span>
        </span>
      </div>

      {/* The ask's second half — "trust one agent more than another" — read DOWN the board. Its
          own grid column, never squeezed into a neighbour's row: on the name line it starved to
          "44.4 · …" on every lane, and as its own line it made the lane bleed. */}
      <div
        className={`board-score-col${lane.competence ? "" : " score-none"}`}
        title={lane.competence
          ? `${lane.model ?? "this model"} — ${lane.competence}`
            + (amongYours(lane.score, fleet) ? ` — ${amongYours(lane.score, fleet)}` : "")
            + " — one measure, never a verdict"
          : lane.model
            ? `${lane.model} — no ranking carries this model`
            : "this run did not record which model it used"}
      >
        {/* The ROW carries the rank among the models YOU run; the board-wide place stays in the
            title. "24th of 450" was identical on every lane — a denominator none of them
            competes against. An EMPTY cell read as a failed fetch, so a dash says the ranking
            does not place this model, which is ordinary. */}
        {lane.competence
          ? (amongYours(lane.score, fleet)
              ? `${(lane.score ?? 0).toFixed(1)} · ${amongYours(lane.score, fleet)}`
              : shortCompetence(lane.competence))
          : "—"}
      </div>

      <div className="lane-track">
        {gridLines(axis)}
        {threads.map((t) => [
          <div className={`thread thread-${t.kind}`} style={{ left: `${t.x}%`, background: t.hue }} />,
          t.kind === "end" && (
            <div className="thread-elbow" style={{ left: `${t.x}%`, borderColor: t.hue }} />
          ),
          t.kind === "start" && (
            <div className="thread-node" style={{ left: `${t.x}%`, background: t.hue }} />
          ),
        ])}
        {/* Status owns the bar's fill and cap, because "running vs done vs crashed must not look
            alike" is the load-bearing read. Provider gets the launch edge only — enough to see a
            mixed-vendor team on the clock without contesting the status channel. */}
        <div
          className={`bar bar-${lane.status} ${unknownEnd ? "bar-unknown-end" : ""}`}
          style={{ left: `${left}%`, width: `${width}%`, borderLeftColor: hue }}
        >
          {lane.status === "running" && (
            <span className="bar-sweep-clip">
              <span className="bar-sweep" />
            </span>
          )}
          <span className="bar-cap" />
        </div>
        <div className="now-line" style={{ left: `${axis.pct(axis.now)}%` }} />
      </div>

      <div className="board-meta-col lane-meta">
        <span className="meta-elapsed">{elapsed}</span>
        <span
          className={`meta-cost ${lane.costUsd == null ? "meta-unknown" : ""}`}
          aria-label={`${lane.chargePath}, ${lane.costCertainty}, ${fmtCost(lane.costUsd, lane.costCertainty)}`}
        >
          {fmtCost(lane.costUsd, lane.costCertainty)}
        </span>
      </div>
    </div>
  );
}

/** Per-status share of a group, as one thin bar — a group's health without reading any row. */
function GroupPulse({ lanes }: { lanes: AgentLane[] }): Node {
  const order: AgentStatus[] = [
    "starting", "running", "waiting", "done", "stopped", "cancelled", "failed", "crashed",
    "foreign", "declared",
  ];
  const counts = order
    .map((s) => ({ s, n: lanes.filter((l) => l.status === s).length }))
    .filter((c) => c.n > 0);
  return (
    <span className="group-pulse" aria-hidden="true">
      {counts.map((c) => (
        <span
          className={`pulse-seg pulse-${c.s}`}
          style={{ flexGrow: String(c.n) }}
          title={`${c.n} ${c.s}`}
        />
      ))}
    </span>
  );
}

const GROUP_OPTIONS: { id: AgentGroupBy; label: string }[] = [
  { id: "project", label: "Project" },
  { id: "provider", label: "Provider" },
  { id: "model", label: "Model" },
  { id: "none", label: "Flat" },
];

function AgentBoard({ item }: { item: CellContent & { kind: "agent-board" } }): Node {
  const { lanes, now } = item;
  const axis = buildAxis(item.windowStart, now);
  const groups = buildGroups(lanes, item.groupBy, now);
  const running = lanes.filter((l) => l.status === "running").length;
  const byId = new Map(lanes.map((l) => [l.id, l]));

  return (
    <div className="board" role="region" aria-label={item.ariaSummary}>
      <div className="board-head">
        <div className="hero">
          <span className={`hero-num ${running ? "hero-live" : ""}`}>
            {running > 0 && <span className="hero-pulse" />}
            {String(running)}
          </span>
          <span className="hero-side">
            <span className="hero-label">running</span>
            <span className="hero-sub">
              {lanes.length} agent{lanes.length === 1 ? "" : "s"} · window{" "}
              {fmtDuration(now - item.windowStart)}
            </span>
          </span>
        </div>
        <div className="hero hero-cost">
          <span className="hero-side">
            <span className="hero-label">{item.billing.heading}</span>
            {item.billing.lines.map((line) => (
              <span className="hero-sub" data-charge-path={line.chargePath}>{line.text}</span>
            ))}
          </span>
        </div>
        <div className="group-control" role="radiogroup" aria-label="Group agents by">
          <span className="group-control-label">Split by</span>
          {GROUP_OPTIONS.map((opt) => (
            <button
              className={`seg ${opt.id === item.groupBy ? "seg-active" : ""}`}
              aria-pressed={opt.id === item.groupBy ? "true" : "false"}
              onClick={() => setAgentGroup(opt.id)}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      <ConcurrencyRibbon lanes={lanes} axis={axis} />

      <div className="board-row board-axis-row">
        <div className="board-aside" />
        <div className="board-score-col" />
        <div className="lane-track axis-track">
          {gridLines(axis)}
          {axis.ticks.map((t) => (
            <span className="axis-tick" style={{ left: `${t.pct}%` }}>
              {t.label}
            </span>
          ))}
        </div>
        <div className="board-meta-col" />
      </div>

      {groups.map((group) => {
        const gid = `${item.groupBy}:${group.key}`;
        const isCollapsed = collapsed.has(gid);
        const present = new Set(group.lanes.map((l) => l.id));

        // Ancestry drawn as a "thread" running down the track from the spawning agent's row to the
        // spawned one's bar start — lineage read IN TIME rather than as indentation. Only within a
        // group: splitting by provider legitimately separates a parent from its child, and that
        // gets an explicit "spawned elsewhere" mark instead of a line to nowhere.
        const threadsFor = group.lanes.map(
          () => [] as { x: number; kind: "start" | "through" | "end"; hue: string }[],
        );
        const externalParent: (string | undefined)[] = group.lanes.map(() => undefined);
        const localDepth = group.lanes.map(() => 0);

        group.lanes.forEach((lane, ci) => {
          if (!lane.parentId) return;
          const parent = byId.get(lane.parentId);
          if (!parent) return;
          if (!present.has(parent.id)) {
            externalParent[ci] = parent.name;
            return;
          }
          const pi = group.lanes.findIndex((l) => l.id === parent.id);
          if (pi < 0 || pi >= ci) return;
          localDepth[ci] = localDepth[pi] + 1;
          const x = axis.pct(lane.startedAt);
          const hue = providerHue(parent.provider);
          threadsFor[pi].push({ x, kind: "start", hue });
          for (let k = pi + 1; k < ci; k++) threadsFor[k].push({ x, kind: "through", hue });
          threadsFor[ci].push({ x, kind: "end", hue });
        });

        return (
          <div className={`group ${isCollapsed ? "group-collapsed" : ""}`}>
            {item.groupBy !== "none" && (
              <div
                className="group-head"
                role="button"
                tabIndex={0}
                onClick={() => toggleGroup(gid)}
              >
                <span className="group-caret">{isCollapsed ? "▸" : "▾"}</span>
                <span className="group-name" title={group.key}>
                  {group.key}
                </span>
                <GroupPulse lanes={group.lanes} />
                <span className="group-totals">
                  <span className={group.running ? "tot-live" : ""}>
                    {group.running} running
                  </span>
                  <span className="tot-sep">·</span>
                  <span>
                    {group.lanes.length} agent{group.lanes.length === 1 ? "" : "s"}
                  </span>
                  <span className="tot-sep">·</span>
                  <span>{fmtDuration(group.span)}</span>
                </span>
              </div>
            )}
            {!isCollapsed &&
              group.lanes.map((lane, i) => (
                <Lane
                  lane={lane}
                  axis={axis}
                  threads={threadsFor[i]}
                  localDepth={localDepth[i]}
                  externalParent={externalParent[i]}
                  fleet={lanes}
                />
              ))}
          </div>
        );
      })}
    </div>
  );
}

/** Grouping is applied locally on click AND reported to the host, so the board answers instantly
 *  and the host still owns the persisted preference. */
function setAgentGroup(group: AgentGroupBy): void {
  const cell = cellCache.get("agents");
  const board = cell?.content.find((c) => c.kind === "agent-board");
  if (board && board.kind === "agent-board") board.groupBy = group;
  post("setAgentGroup", { group });
  renderCell("agents");
}

function toggleGroup(gid: string): void {
  if (collapsed.has(gid)) collapsed.delete(gid);
  else collapsed.add(gid);
  renderCell("agents");
}

function renderContent(item: CellContent): Node | null {
  switch (item.kind) {
    case "row":
      return <Row item={item} />;
    case "setting":
      return <SettingEditor item={item} />;
    case "prompt-workspace":
      return <PromptWorkspace item={item} />;
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
    case "agent-board":
      return <AgentBoard item={item} />;
    case "heading":
      return <div className="group-heading">{item.text}</div>;
    case "empty":
      return (
        <div className="empty">
          <svg className="empty-art" width="88" height="40" viewBox="0 0 88 40" aria-hidden="true">
            <line x1="0" y1="12" x2="88" y2="12" className="empty-rule" />
            <line x1="0" y1="26" x2="88" y2="26" className="empty-rule" />
            <rect x="4" y="7" width="0" height="10" rx="2" className="empty-slot" />
            <rect x="4" y="21" width="0" height="10" rx="2" className="empty-slot" />
            <line x1="80" y1="2" x2="80" y2="36" className="empty-now" />
          </svg>
          <div className="empty-msg">{item.message}</div>
          {item.hint && <code className="empty-hint">{item.hint}</code>}
        </div>
      );
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
  const prompt = cellId === "prompt-workspace"
    ? el.querySelector<HTMLTextAreaElement>("#prompt-buffer") : null;
  const workspace = prompt?.closest<HTMLElement>(".prompt-workspace");
  const draft = prompt?.value;
  const selected = workspace?.dataset.selected;
  const revision = workspace?.dataset.revision;
  const pending = pendingPromptAction;
  const active = document.activeElement;
  const edited = active && ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName)
    ? active as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement : null;
  const preserveControl = edited && el.contains(edited) ? {
    label: edited.getAttribute("aria-label"),
    value: edited.value,
    start: edited.tagName === "INPUT" || edited.tagName === "TEXTAREA"
      ? (edited as HTMLInputElement | HTMLTextAreaElement).selectionStart : null,
    end: edited.tagName === "INPUT" || edited.tagName === "TEXTAREA"
      ? (edited as HTMLInputElement | HTMLTextAreaElement).selectionEnd : null,
  } : null;
  el.replaceChildren();
  buildCellContent(cellId, el);
  const refreshedWorkspace = el.querySelector<HTMLElement>(".prompt-workspace");
  const refreshedPrompt = el.querySelector<HTMLTextAreaElement>("#prompt-buffer");
  const promptChanged = refreshedWorkspace?.dataset.revision !== revision;
  const typedAfterDispatch = pending != null && promptInputGeneration !== pending.generation;
  const preservePrompt = draft !== undefined && refreshedPrompt && refreshedWorkspace
    && refreshedWorkspace.dataset.selected === selected
    && (!promptChanged || pending == null || typedAfterDispatch);
  if (preservePrompt) {
    refreshedPrompt.value = draft;
    if (workspace?.dataset.digest && !(pending?.kind === "save" && promptChanged)) {
      refreshedWorkspace.dataset.digest = workspace.dataset.digest;
    }
  }
  if (promptChanged && pending) pendingPromptAction = null;
  if (preserveControl?.label) {
    const refreshed = Array.from(el.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(
      "input[aria-label], textarea[aria-label], select[aria-label]",
    )).find((control) => control.getAttribute("aria-label") === preserveControl.label);
    if (refreshed) {
      if (refreshed !== refreshedPrompt || preservePrompt) refreshed.value = preserveControl.value;
      refreshed.focus();
      if ((refreshed.tagName === "INPUT" || refreshed.tagName === "TEXTAREA")
          && preserveControl.start != null && preserveControl.end != null) {
        (refreshed as HTMLInputElement | HTMLTextAreaElement)
          .setSelectionRange(preserveControl.start, preserveControl.end);
      }
    }
  }
}

function renderAll(): void {
  const root = document.getElementById("root")!;
  root.replaceChildren();
  root.appendChild(<TabBar />);

  const tab = TABS.find((t) => t.id === activeTab)!;
  const content = document.createElement("div");
  content.className = "tab-content";

  for (const cellId of tab.cells) {
    // Plain DOM rather than JSX: this is a mutable container handed to a builder that needs an
    // Element, and the JSX factory is typed to return Node (a component may return a fragment).
    const container = document.createElement("div");
    container.id = `cell-${cellId}`;
    container.className = "cell";
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
