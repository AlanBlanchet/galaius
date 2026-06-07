import * as vscode from "vscode";
import {
  KeyManager,
  ModelsData,
  CellContent,
  CellUpdate,
  Action,
  RangeId,
  cfg,
  SETTINGS,
  Setting,
  metaOf,
  ensureKeys,
} from "./shared";
import {
  usdRateTo,
  formatMoney,
  currencySymbol,
  COMMON_CURRENCIES,
} from "./currency";
import {
  readUsageLog,
  filterByRange,
  aggregateByProvider,
  aggregateStackedByModel,
  aggregateTokensByModel,
  aggregateCallsByModel,
  colorFor,
} from "./usage";

function getNonce(): string {
  const chars =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  let result = "";
  for (let i = 0; i < 32; i++) {
    result += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return result;
}

const VIEW_TYPE = "interact.dashboard";

// Benchmark ids the dashboard surfaces. These are taxonomy ids (matches
// Benchmark.id in benchmarks.json), not domain data.
const BENCHMARK_IDS = ["screenspot_pro", "screenspot"];
const RECOMMENDATION_BENCHMARK = "screenspot_pro";

interface PublishedEntryData {
  model_name: string;
  score: number;
}
interface PublishedTableData {
  source_url: string;
  retrieved: string;
  lib_recommendation: string | null;
  entries: PublishedEntryData[];
}
interface BenchmarkData {
  id: string;
  name: string;
  description: string;
  url: string;
  metric: string;
  published: PublishedTableData | null;
  lib_recommendation_model_id: string | null;
  measured: Record<string, number>;
  recommendations: RecommendationData[];
}
interface RecommendationData {
  model_id: string;
  score: number;
  source: "published" | "measured";
  rank: number;
  cost_per_million: number | null;
  quality_per_dollar: number | null;
}
export interface BenchmarksFile {
  benchmarks: BenchmarkData[];
}

function asBenchmarks(raw: unknown): BenchmarksFile {
  if (
    raw &&
    typeof raw === "object" &&
    Array.isArray((raw as { benchmarks?: unknown }).benchmarks)
  ) {
    return raw as BenchmarksFile;
  }
  return { benchmarks: [] };
}

export class DashboardPanel {
  static instance: DashboardPanel | undefined;
  private readonly panel: vscode.WebviewPanel;
  private disposed = false;
  private range: RangeId = "7d";

  private constructor(
    panel: vscode.WebviewPanel,
    private readonly extensionUri: vscode.Uri,
    private readonly keyManager: KeyManager,
    private readonly modelsData: ModelsData,
    private readonly benchmarksData: BenchmarksFile,
    private readonly emitter: vscode.EventEmitter<void>,
  ) {
    this.panel = panel;
    panel.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(extensionUri, "out")],
    };
    panel.webview.onDidReceiveMessage((msg) => this.handleMessage(msg));
    panel.onDidDispose(() => {
      this.disposed = true;
      DashboardPanel.instance = undefined;
    });
    panel.webview.html = this.getHtml();
    setTimeout(() => this.refresh(), 500);
  }

  static createOrShow(
    extensionUri: vscode.Uri,
    keyManager: KeyManager,
    modelsData: ModelsData,
    benchmarksRaw: unknown,
    emitter: vscode.EventEmitter<void>,
  ): DashboardPanel {
    if (DashboardPanel.instance) {
      DashboardPanel.instance.panel.reveal(vscode.ViewColumn.One);
      return DashboardPanel.instance;
    }
    const panel = vscode.window.createWebviewPanel(
      VIEW_TYPE,
      "Interact",
      vscode.ViewColumn.One,
      {
        enableScripts: true,
        localResourceRoots: [vscode.Uri.joinPath(extensionUri, "out")],
      },
    );
    DashboardPanel.instance = new DashboardPanel(
      panel,
      extensionUri,
      keyManager,
      modelsData,
      asBenchmarks(benchmarksRaw),
      emitter,
    );
    return DashboardPanel.instance;
  }

  static registerSerializer(
    extensionUri: vscode.Uri,
    keyManager: KeyManager,
    modelsData: ModelsData,
    benchmarksRaw: unknown,
    emitter: vscode.EventEmitter<void>,
  ): vscode.Disposable {
    return vscode.window.registerWebviewPanelSerializer(VIEW_TYPE, {
      async deserializeWebviewPanel(panel: vscode.WebviewPanel) {
        DashboardPanel.instance = new DashboardPanel(
          panel,
          extensionUri,
          keyManager,
          modelsData,
          asBenchmarks(benchmarksRaw),
          emitter,
        );
      },
    });
  }

  static refreshIfOpen(): void {
    DashboardPanel.instance?.refresh();
  }

  async refresh(): Promise<void> {
    if (this.disposed) return;
    const cells: CellUpdate[] = [
      this.statusCell(),
      this.apiKeysCell(),
      ...this.settingsCells(),
      this.displayCell(),
      await this.consumptionCell(),
      this.benchmarksCell(),
      this.recommendationsCell(),
    ];
    for (const cell of cells) {
      this.panel.webview.postMessage({ type: "cellUpdate", cell });
    }
  }

  private async handleMessage(msg: {
    type: string;
    setting?: string;
    provider?: string;
  }): Promise<void> {
    switch (msg.type) {
      case "ready":
        this.refresh();
        break;
      case "configureProvider": {
        if (!msg.provider) return;
        await ensureKeys(msg.provider, this.modelsData, this.keyManager, this.emitter);
        this.refresh();
        break;
      }
      case "removeProvider": {
        if (!msg.provider) return;
        const info = this.modelsData.providers[msg.provider];
        if (!info) return;
        for (const k of info.envKeys) {
          await this.keyManager.remove(k);
        }
        this.emitter.fire();
        this.refresh();
        break;
      }
      case "changeModel": {
        await vscode.commands.executeCommand("interact.selectModel");
        this.refresh();
        break;
      }
      case "changeSetting": {
        const setting = SETTINGS.find((s) => s.key === msg.setting);
        if (setting) await this.editSetting(setting);
        break;
      }
      case "changeCurrency": {
        const pick = await vscode.window.showQuickPick(COMMON_CURRENCIES, {
          placeHolder: "Display currency for spend (live ECB conversion)",
        });
        if (pick) {
          await cfg().update("display.currency", pick, vscode.ConfigurationTarget.Global);
          this.refresh();
        }
        break;
      }
      case "refresh":
        this.refresh();
        break;
      case "reloadPanel":
        this.reload();
        break;
      case "setRange": {
        const next = (msg as { range?: string }).range;
        if (next === "24h" || next === "7d" || next === "30d" || next === "all") {
          this.range = next;
          this.refresh();
        }
        break;
      }
    }
  }

  /** Prompt for a setting's new value with the control its kind implies (enum/bool → quick-pick,
   *  numbers/strings/paths → input box), then persist it. Empty clears the override (→ default). */
  private async editSetting(s: Setting): Promise<void> {
    const current = cfg().get<string | number | boolean>(s.key);
    let value: string | number | boolean | undefined;
    if (s.kind === "bool") {
      const pick = await vscode.window.showQuickPick(["on", "off"], {
        placeHolder: `${s.label} — ${s.description}`,
      });
      if (pick === undefined) return;
      value = pick === "on";
    } else if (s.kind === "enum") {
      const pick = await vscode.window.showQuickPick(
        (s.options ?? []).map((o) => ({ label: o.label, value: o.value })),
        { placeHolder: `${s.label} — ${s.description}` },
      );
      if (!pick) return;
      value = pick.value;
    } else {
      const text = await vscode.window.showInputBox({
        prompt: `${s.label} — ${s.description}`,
        value: current === undefined ? "" : String(current),
        placeHolder: s.default ? `default: ${s.default}` : "",
        ignoreFocusOut: true,
      });
      if (text === undefined) return;
      value =
        text === "" ? undefined : s.kind === "int" ? Number(text) : text;
    }
    await cfg().update(s.key, value, vscode.ConfigurationTarget.Global);
    this.refresh();
  }

  private statusCell(): CellUpdate {
    const projectPath = cfg().get<string>("projectPath") || "(auto-detect)";
    return {
      id: "status",
      title: "System Status",
      content: [
        { kind: "row", label: "Extension Active", dot: "ok" },
        { kind: "row", label: "Project:", value: projectPath },
      ],
    };
  }

  private apiKeysCell(): CellUpdate {
    const providers = Object.entries(this.modelsData.providers).filter(
      ([, info]) => info.envKeys.length > 0,
    );
    if (!providers.length) {
      return {
        id: "apiKeys",
        title: "API Keys",
        content: [{ kind: "empty", message: "No model data available" }],
      };
    }
    const content: CellContent[] = providers.map(([name, info]) => {
      const allConfigured = info.envKeys.every((k) => this.keyManager.get(k));
      const actions: Action[] = allConfigured
        ? [{ type: "removeProvider", label: "Remove Keys", data: { provider: name }, style: "secondary" as const }]
        : [{ type: "configureProvider", label: "Configure", data: { provider: name } }];
      return {
        kind: "row" as const,
        label: name,
        dot: allConfigured ? ("ok" as const) : ("missing" as const),
        actions,
      };
    });
    return { id: "apiKeys", title: "API Keys", content };
  }

  /** One row for a setting: current value (or its auto/default) + a Change action; model rows
   *  also show cost and the fallback chain. */
  private settingRows(s: Setting): CellContent[] {
    const recs = this.modelsData.recommendations || {};
    const raw = cfg().get<string | number | boolean>(s.key);
    const isSet = raw !== undefined && raw !== "";
    let value = isSet ? String(raw) : `auto · ${s.default || "default"}`;
    const action = s.kind === "model" ? "changeModel" : "changeSetting";
    if (s.kind === "model" && isSet) {
      const meta = metaOf(String(raw), this.modelsData);
      if (meta?.input_cost_per_million) {
        value += ` — $${meta.input_cost_per_million}/M in, $${meta.output_cost_per_million ?? 0}/M out`;
      }
    }
    const rows: CellContent[] = [
      {
        kind: "row",
        label: s.label,
        value,
        tooltip: s.description,
        actions: [{ type: action, label: "Change", data: { setting: s.key } }],
      },
    ];
    if (s.kind === "model") {
      const chain = (recs[s.role ?? ""] || []).filter((m) => m !== raw).slice(0, 3);
      if (chain.length) {
        rows.push({
          kind: "row",
          label: "  ↳ fallback chain",
          value: chain.join(" → "),
          tooltip: "If the primary model fails, the next is tried automatically (max 3).",
        });
      }
    }
    return rows;
  }

  /** One sub-panel (card) per schema group — Models / Desktop / Browser / Advanced — so the
   *  Configuration tab reads as distinct sections rather than one long list. Cell id is
   *  `cfg-<group>` (the webview lists these); all driven by the shared settings schema. */
  private settingsCells(): CellUpdate[] {
    const byGroup = new Map<string, CellContent[]>();
    for (const s of SETTINGS) {
      const rows = byGroup.get(s.group) ?? [];
      rows.push(...this.settingRows(s));
      byGroup.set(s.group, rows);
    }
    return [...byGroup].map(([group, content]) => ({
      id: `cfg-${group.toLowerCase()}`,
      title: group,
      content,
    }));
  }

  /** Display preferences (UI-only, not server config) — currently the spend display currency. */
  private displayCell(): CellUpdate {
    const cur = cfg().get<string>("display.currency") || "USD";
    return {
      id: "cfg-display",
      title: "Display",
      content: [
        {
          kind: "row",
          label: "Currency",
          value: cur,
          tooltip:
            "Spend is recorded in USD and converted at live ECB rates (frankfurter.app) for display only.",
          actions: [{ type: "changeCurrency", label: "Change" }],
        },
      ],
    };
  }

  private async consumptionCell(): Promise<CellUpdate> {
    const all = await readUsageLog();
    const entries = filterByRange(all, this.range);

    const rangeSelector: CellContent = {
      kind: "range-selector",
      current: this.range,
      options: [
        { id: "24h", label: "Last 24h" },
        { id: "7d", label: "7d" },
        { id: "30d", label: "30d" },
        { id: "all", label: "All" },
      ],
    };

    if (!entries.length) {
      return {
        id: "consumption",
        title: "Consumption",
        content: [
          rangeSelector,
          {
            kind: "empty",
            message:
              "Run a few MCP tool calls to populate consumption charts.",
          },
        ],
      };
    }

    const rangeLabel =
      this.range === "all" ? "all time" : `last ${this.range}`;
    const stackedDays =
      this.range === "24h" ? 1 : this.range === "30d" ? 30 : this.range === "all" ? 30 : 14;

    // Spend is recorded in USD; convert to the user's display currency at a live ECB rate.
    const currency = cfg().get<string>("display.currency") || "USD";
    const rate = await usdRateTo(currency);
    const sym = currencySymbol(currency);
    const money = (usd: number) => formatMoney(usd, currency, rate);

    // a) Spend by provider — horizontal bar
    const provAgg = aggregateByProvider(entries, this.modelsData);
    const provTotal = provAgg.reduce((s, p) => s + p.cost, 0);
    const provBars = provAgg.map((p) => {
      const pct = provTotal > 0 ? (p.cost / provTotal) * 100 : 0;
      return {
        label: `${p.provider} (${money(p.cost)} • ${pct.toFixed(0)}%)`,
        value: p.cost * rate,
        color: colorFor(p.provider),
      };
    });
    const topProv = provAgg[0];
    const topPct =
      provTotal > 0 && topProv ? (topProv.cost / provTotal) * 100 : 0;
    const providerCell: CellContent = {
      kind: "bar-h",
      bars: provBars,
      valuePrefix: sym,
      ariaSummary: topProv
        ? `${topProv.provider} accounts for ${topPct.toFixed(0)}% of spend over ${rangeLabel}, ${money(topProv.cost)} of ${money(provTotal)} total.`
        : `No spend recorded over ${rangeLabel}.`,
    };

    // b) Spend by model over time — stacked bar
    const stacked = aggregateStackedByModel(entries, stackedDays, 5);
    const stackedTotal = stacked.series.reduce(
      (s, ser) => s + ser.values.reduce((a, b) => a + b, 0),
      0,
    );
    const stackedCell: CellContent = {
      kind: "stacked-bar",
      xLabels: stacked.xLabels,
      series: stacked.series.map((ser) => ({
        ...ser,
        values: ser.values.map((v) => v * rate),
      })),
      valuePrefix: sym,
      ariaSummary: `Daily spend across ${stacked.series.length} models over the last ${stackedDays} days, total ${money(stackedTotal)}.`,
    };

    // c) Tokens by model (input vs output)
    const tokAgg = aggregateTokensByModel(entries).slice(0, 6);
    const tokensCell: CellContent = {
      kind: "small-multiples",
      panels: tokAgg.map((t) => ({
        title: t.model,
        bars: [
          {
            label: "in",
            value: t.inputTokens,
            color: "var(--vscode-charts-blue)",
          },
          {
            label: "out",
            value: t.outputTokens,
            color: "var(--vscode-charts-orange)",
          },
        ],
      })),
      ariaSummary: `Input vs output tokens for ${tokAgg.length} models over ${rangeLabel}.`,
    };

    // d) Calls per model — donut
    const callsAgg = aggregateCallsByModel(entries);
    const callsTotal = callsAgg.reduce((s, c) => s + c.calls, 0);
    const donutSegs = callsAgg.slice(0, 6).map((c) => ({
      label: c.model,
      value: c.calls,
      color: colorFor(c.model),
    }));
    if (callsAgg.length > 6) {
      const otherCalls = callsAgg
        .slice(6)
        .reduce((s, c) => s + c.calls, 0);
      donutSegs.push({
        label: "other",
        value: otherCalls,
        color: "var(--vscode-descriptionForeground)",
      });
    }
    const donutCell: CellContent = {
      kind: "donut",
      segments: donutSegs,
      centerLabel: `${callsTotal}`,
      ariaSummary: `${callsTotal} calls across ${callsAgg.length} models over ${rangeLabel}.`,
    };

    return {
      id: "consumption",
      title: "Consumption",
      content: [
        rangeSelector,
        { kind: "row", label: "Spend by provider" },
        providerCell,
        { kind: "row", label: `Spend by model — last ${stackedDays}d` },
        stackedCell,
        { kind: "row", label: "Tokens by model (input vs output)" },
        tokensCell,
        { kind: "row", label: "Calls per model" },
        donutCell,
      ],
    };
  }

  private benchmarksCell(): CellUpdate {
    const content: CellContent[] = [];
    for (const id of BENCHMARK_IDS) {
      const bench = this.benchmarksData.benchmarks.find((b) => b.id === id);
      if (!bench) continue;
      const rows: string[][] = [];
      if (bench.published?.lib_recommendation) {
        rows.push([
          `${bench.published.lib_recommendation} (lib recommendation)`,
          "—",
          "published",
        ]);
      }
      const topPublished = (bench.published?.entries ?? []).slice(0, 5);
      for (const e of topPublished) {
        rows.push([e.model_name, e.score.toFixed(3), "published"]);
      }
      for (const [modelId, score] of Object.entries(bench.measured)) {
        rows.push([modelId, score.toFixed(3), "measured"]);
      }
      if (!rows.length) continue;
      content.push({
        kind: "row",
        label: bench.name,
        value: bench.url,
      });
      content.push({
        kind: "table",
        headers: ["Model", "Score", "Source"],
        rows,
      });
    }
    if (content.length === 0) {
      content.push({ kind: "empty", message: "No benchmark data available" });
    }
    return { id: "benchmarks", title: "Benchmark Reference", content };
  }

  private recommendationsCell(): CellUpdate {
    const bench = this.benchmarksData.benchmarks.find(
      (b) => b.id === RECOMMENDATION_BENCHMARK,
    );
    if (!bench) {
      return {
        id: "recommendations",
        title: "Recommendations",
        content: [{ kind: "empty", message: "No benchmark data" }],
      };
    }
    const knownModels = new Set<string>();
    for (const info of Object.values(this.modelsData.providers)) {
      for (const name of Object.keys(info.models)) knownModels.add(name);
    }
    const top = bench.recommendations
      .filter((r) => knownModels.has(r.model_id))
      .slice(0, 3);
    if (!top.length) {
      return {
        id: "recommendations",
        title: "Recommendations",
        content: [{ kind: "empty", message: "No matching models in registry" }],
      };
    }
    const tableRows = top.map((r) => [
      r.model_id,
      r.score.toFixed(3),
      r.source,
      r.cost_per_million != null ? `$${r.cost_per_million.toFixed(2)}` : "—",
      r.quality_per_dollar != null ? r.quality_per_dollar.toFixed(3) : "—",
    ]);
    return {
      id: "recommendations",
      title: `Recommendations (${bench.name})`,
      content: [
        {
          kind: "table",
          headers: ["Model", "Score", "Source", "$/M", "Quality/$"],
          rows: tableRows,
        },
      ],
    };
  }

  /** Reload just the webview (re-fetches the built JS/CSS) — picks up panel changes WITHOUT a
   *  full window reload. Only the webview is rebuilt this way; extension-host code changes
   *  (dashboard.ts, extension.ts) still need a window reload. */
  reload(): void {
    if (this.disposed) return;
    this.panel.webview.html = this.getHtml(); // re-fetch assets; the webview's "ready" → refresh()
  }

  private getHtml(): string {
    const webview = this.panel.webview;
    const nonce = getNonce();
    // Cache-bust so reload() actually re-fetches a rebuilt bundle instead of a cached copy.
    const v = Date.now();
    const scriptUri = `${webview.asWebviewUri(
      vscode.Uri.joinPath(this.extensionUri, "out", "webview.js"),
    )}?v=${v}`;
    const styleUri = `${webview.asWebviewUri(
      vscode.Uri.joinPath(this.extensionUri, "out", "webview.css"),
    )}?v=${v}`;
    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}' ${webview.cspSource};">
  <link rel="stylesheet" href="${styleUri}">
</head>
<body>
  <div id="root"></div>
  <script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
  }
}
