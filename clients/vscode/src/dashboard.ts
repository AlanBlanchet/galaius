import * as vscode from "vscode";
import * as fs from "fs";
import * as path from "path";
import {
  KeyManager,
  ModelsData,
  CellContent,
  CellUpdate,
  Action,
  RangeId,
  AgentLane,
  AgentGroupBy,
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
import { scopeStore } from "./scopeStore";
import { readAgentRuns, summarise, withDepth } from "./agents";
import { describeAge as describeBoardAge, readLeaderboard } from "./leaderboard";
import { describeAge, ageSeconds, isLive, loadCatalog, seeingModels, type Catalog } from "./catalog";
import { agentsDir, usageLogPathFor, INTERACT_CONFIG_PATH } from "./paths";
import { DIM_FOREGROUND } from "./themeTokens";
import { claimColumn, nextColumn, releaseColumn } from "./panelColumn";
import { presentMediaStatus } from "./mediaStatus";
import { conversationExtensionVersion, resolveConversationBackend } from "./conversationBackend";
import { promptRequest, type PromptEditorState } from "./promptClient";
import { PROMPT_ACTIONS, type PromptAction } from "./promptActions";
import { comparisonRows } from "./comparison";
import { readOrg } from "./org";
import { bareModelName, shortCompetence } from "./competence";
import { CompetenceStore } from "./competenceStore";
import { billingPresentation } from "./billingPresentation";
import { refreshToolSettings, saveToolSetting, type ToolSettingsView } from "./toolSettings.ts";
import { serverWorkspaceConfigured } from "./workspaceState.ts";
import {
  readUsageLog,
  filterByRange,
  aggregateByProvider,
  aggregateStackedByModel,
  aggregateTokensByModel,
  aggregateCallsByModel,
  summarizeUsage,
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
const RECOMMENDATION_BENCHMARK = "screenspot_pro";

interface PublishedEntryData {
  model_name: string;
  model_id?: string | null;
  score: number;
  normalized_score?: number | null;
  status?: "eligible" | "unverified" | "missing" | "not_applicable" | "approximate" | "unmapped";
}
interface PublishedTableData {
  source_url: string;
  retrieved: string;
  lib_recommendation: string | null;
  entries: PublishedEntryData[];
  freshness?: "current" | "stale" | "unknown";
}
interface BenchmarkData {
  id: string;
  name: string;
  description: string;
  category: "image" | "gui_grounding" | "video";
  source: string;
  source_auth: string;
  requires_auth?: boolean;
  url: string;
  score_url?: string;
  methodology_url?: string;
  metric: string;
  score_range?: [number, number] | null;
  higher_is_better?: boolean | null;
  refresh_supported?: boolean;
  published: PublishedTableData | null;
  lib_recommendation_model_id: string | null;
  measured: Record<string, number>;
  recommendations: RecommendationData[];
}

// Display order + human labels for the three benchmark categories.
const BENCHMARK_CATEGORIES: { id: BenchmarkData["category"]; label: string }[] = [
  { id: "image", label: "Image understanding" },
  { id: "gui_grounding", label: "GUI grounding (where to click)" },
  { id: "video", label: "Video understanding" },
];
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
  /** How the agent board splits its lanes. MODEL first: "compare the most competents and trust one
   *  agent more than another in some situations". That comparison exists under Split by → Model —
   *  one row per model, strict descending — and it opened behind a control nobody pressed, on a
   *  default view whose rows all printed the same string. */
  private agentGroupBy: AgentGroupBy = "model";
  private measuredStore: CompetenceStore | undefined;
  /** Every number this panel shows, and why it has none when it has none.
   *
   *  Lazy, not a field initializer: a test that exercises one cell by calling the method on a
   *  hand-built object never runs field initializers, so the field was `undefined` and the cell
   *  threw where it used to render. A getter answers for both shapes. */
  private get measured(): CompetenceStore {
    return (this.measuredStore ??= new CompetenceStore());
  }
  private watchers: fs.FSWatcher[] = [];
  private refreshTimer: ReturnType<typeof setTimeout> | undefined;
  private refreshRevision = 0;
  private settingsSnapshot: ToolSettingsView | null = null;
  private promptState: PromptEditorState = { files: [] };

  private constructor(
    panel: vscode.WebviewPanel,
    private readonly extensionUri: vscode.Uri,
    private readonly keyManager: KeyManager,
    private readonly modelsData: ModelsData,
    private readonly benchmarksData: BenchmarksFile,
    private readonly emitter: vscode.EventEmitter<void>,
    private readonly conversationCatalog: () => import("./generated/types").ConversationCatalog | undefined,
  ) {
    this.panel = panel;
    panel.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(extensionUri, "out")],
    };
    panel.webview.onDidReceiveMessage((msg) => this.handleMessage(msg));
    // One column for interact's surfaces: a new one joins the group its siblings already hold
    // rather than opening yet another beside your code.
    claimColumn("dashboard", panel.viewColumn);
    panel.onDidDispose(() => {
      this.disposed = true;
      this.stopWatching();
      releaseColumn("dashboard");
      DashboardPanel.instance = undefined;
    });
    panel.webview.html = this.getHtml();
    setTimeout(() => this.refresh(), 500);
    this.startWatching();
  }

  /** Path of the usage log the running server writes to — under the configured base dir, so a
   *  custom `interact.debug.dir` (== Python's INTERACT_DEBUG_DIR) is honoured, not hardcoded. */
  private usageLogPath(): string {
    return usageLogPathFor(cfg().get<string>("debug.dir") || "");
  }

  /** Live-sync the panel: the MCP server is a SEPARATE process that appends to the usage log and
   *  config.env as you work, so without watching them the panel would freeze at open time. Watch
   *  the usage log's dir and config.env's dir, debounced into one refresh. config.env is located
   *  DIRECTLY (not as the log dir's parent): the two move independently — point `interact.debug.dir`
   *  at a project's out/ and the parent is that project, not `~/.interact`, so walking up would
   *  both miss key/setting edits and watch an unrelated directory. */
  private startWatching(): void {
    const dirs = new Set([
      path.dirname(this.usageLogPath()),
      path.dirname(INTERACT_CONFIG_PATH),
      // Agent runs appear and change here; without this a spawned agent would sit invisible
      // until something else happened to trigger a refresh.
      agentsDir(),
    ]);
    for (const dir of dirs) {
      try {
        fs.mkdirSync(dir, { recursive: true });
        const w = fs.watch(dir, () => this.scheduleRefresh());
        w.on("error", () => {}); // a transient watch error must not crash the panel
        this.watchers.push(w);
      } catch {
        /* unwatchable dir → just no live updates from it */
      }
    }
  }

  private stopWatching(): void {
    if (this.refreshTimer) clearTimeout(this.refreshTimer);
    for (const w of this.watchers) {
      try {
        w.close();
      } catch {
        /* already closed */
      }
    }
    this.watchers = [];
  }

  private scheduleRefresh(): void {
    if (this.refreshTimer) clearTimeout(this.refreshTimer);
    this.refreshTimer = setTimeout(() => this.refresh(), 300); // coalesce bursty appends
  }

  static createOrShow(
    extensionUri: vscode.Uri,
    keyManager: KeyManager,
    modelsData: ModelsData,
    benchmarksRaw: unknown,
    emitter: vscode.EventEmitter<void>,
    conversationCatalog: () => import("./generated/types").ConversationCatalog | undefined,
  ): DashboardPanel {
    if (DashboardPanel.instance) {
      DashboardPanel.instance.panel.reveal(DashboardPanel.instance.panel.viewColumn);
      return DashboardPanel.instance;
    }
    const panel = vscode.window.createWebviewPanel(
      VIEW_TYPE,
      "Interact",
      nextColumn() as vscode.ViewColumn,
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
      conversationCatalog,
    );
    return DashboardPanel.instance;
  }

  static registerSerializer(
    extensionUri: vscode.Uri,
    keyManager: KeyManager,
    modelsData: ModelsData,
    benchmarksRaw: unknown,
    emitter: vscode.EventEmitter<void>,
    conversationCatalog: () => import("./generated/types").ConversationCatalog | undefined = () => undefined,
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
          conversationCatalog,
        );
      },
    });
  }

  static refreshIfOpen(): void {
    DashboardPanel.instance?.refresh();
  }

  async refresh(): Promise<void> {
    if (this.disposed) return;
    const revision = ++this.refreshRevision;
    let settingsSnapshot: ToolSettingsView | null = null;
    try { settingsSnapshot = await refreshToolSettings(); } catch { /* settingsCells shows unavailable, never local portable values */ }
    const promptState = !this.promptState.files.length ? await this.loadPromptCatalog() : undefined;
    const models = await this.modelsCell();
    const consumption = await this.consumptionCell();
    if (this.disposed || revision !== this.refreshRevision) return;
    this.settingsSnapshot = settingsSnapshot;
    if (promptState) this.promptState = promptState;
    const cells: CellUpdate[] = [
      this.statusCell(),
      this.apiKeysCell(),
      ...this.settingsCells(),
      this.benchmarkDataCell(),
      this.displayCell(),
      this.agentsCell(),
      models,
      consumption,
      this.benchmarksCell(),
      this.recommendationsCell(),
      { id: "comparison", title: "Agent and model comparison", content: comparisonRows(
        readOrg(), this.conversationCatalog(), this.benchmarksData.benchmarks, readAgentRuns(),
      ) },
      { id: "prompt-workspace", title: "Prompt workspace", content: [{
        kind: "prompt-workspace", ...this.promptState,
      }] },
    ];
    for (const cell of cells) {
      this.panel.webview.postMessage({ type: "cellUpdate", cell });
    }
  }

  private async handleMessage(msg: {
    type: string;
    setting?: string;
    provider?: string;
    key?: string;
    url?: string;
    benchmarkId?: string;
    sourceRole?: "evaluation" | "score" | "methodology";
    value?: string;
    path?: string;
    content?: string;
    digest?: string;
    action?: PromptAction;
    explicit?: boolean;
    message?: string;
    endpoint?: string;
    tokenFile?: string;
  }): Promise<void> {
    if (msg.type === "promptSelect" || msg.type === "promptSave" || msg.type === "promptAction") {
      this.refreshRevision += 1;
    }
    switch (msg.type) {
      case "ready":
        this.refresh();
        break;
      case "promptSelect":
        if (typeof msg.path === "string") await this.loadPrompt(msg.path);
        this.refresh();
        break;
      case "promptSave":
        if (typeof msg.path === "string" && typeof msg.content === "string" && typeof msg.digest === "string") {
          const result = await this.promptCall(["write", msg.path, msg.digest], msg.content);
          if (result.ok) await this.loadPrompt(msg.path);
          else {
            let conflict = false;
            try { conflict = JSON.parse(result.output).code === "conflict"; } catch { /* bounded generic error */ }
            this.promptState = { ...this.promptState, content: msg.content, digest: msg.digest,
              status: conflict
                ? "Conflict: your draft is preserved. Reload from disk explicitly to review the external change."
                : this.promptMessage(result.output) };
          }
        }
        this.refresh();
        break;
      case "promptAction": {
        const metadata = PROMPT_ACTIONS.find((candidate) => candidate.id === msg.action);
        if (!metadata || metadata.remote !== (msg.explicit === true)) break;
        const args: string[] = [metadata.id];
        if (metadata.id === "commit") {
          if (!msg.message?.trim()) {
            this.promptState = { ...this.promptState, status: "A commit message is required." };
            this.refresh();
            break;
          }
          args.push("--message", msg.message.trim());
        } else if (metadata.id === "publish") {
          let endpoint: URL;
          try { endpoint = new URL(msg.endpoint ?? ""); }
          catch { endpoint = new URL("about:blank"); }
          const localHttp = endpoint.protocol === "http:" && ["127.0.0.1", "localhost", "::1"].includes(endpoint.hostname);
          if ((endpoint.protocol !== "https:" && !localHttp) || !msg.tokenFile || !path.isAbsolute(msg.tokenFile)) {
            this.promptState = { ...this.promptState,
              status: "Publish needs an HTTPS or local HTTP service and an absolute token file path." };
            this.refresh();
            break;
          }
          args.push("--endpoint", endpoint.toString().replace(/\/$/, ""), "--token-file", msg.tokenFile);
        }
        const result = await this.promptCall(args);
        this.promptState = { ...this.promptState, status: this.promptMessage(result.output) };
        this.refresh();
        break;
      }
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
      case "saveSetting": {
        const setting = SETTINGS.find((candidate) => candidate.key === msg.setting);
        if (!setting || setting.kind === "model" || typeof msg.value !== "string") break;
        let value: string | number | boolean | undefined = msg.value || undefined;
        if (setting.kind === "bool") {
          if (msg.value !== "true" && msg.value !== "false") break;
          value = msg.value === "true";
        } else if (setting.kind === "int") {
          if (msg.value) {
            const parsed = Number(msg.value);
            if (!Number.isSafeInteger(parsed) || (setting.minimum != null && parsed < setting.minimum)) break;
            value = parsed;
          }
        } else if (setting.kind === "enum") {
          if (!(setting.options ?? []).some((option) => option.value === msg.value)) break;
        } else if (msg.value && setting.pattern && !new RegExp(setting.pattern).test(msg.value)) {
          break;
        }
        this.refreshRevision += 1;
        try {
          if (!await saveToolSetting(setting.env, value === undefined ? undefined : String(value), this.settingsSnapshot)) {
            await cfg().update(setting.key, value, vscode.ConfigurationTarget.Global);
          }
        } catch (error) {
          void vscode.window.showErrorMessage(error instanceof Error ? error.message : "Settings save failed; draft retained.");
          break;
        }
        this.refresh();
        break;
      }
      case "setBenchmarkKey": {
        if (!msg.key) break;
        const value = await vscode.window.showInputBox({
          prompt: `${msg.key} — key for a benchmark data source (improves model recommendations)`,
          password: true,
          ignoreFocusOut: true,
        });
        if (value) {
          await this.keyManager.set(msg.key, value);
          this.emitter.fire(); // re-inject into the server env
          this.refresh();
        }
        break;
      }
      case "clearBenchmarkKey": {
        if (!msg.key) break;
        await this.keyManager.remove(msg.key);
        this.emitter.fire();
        this.refresh();
        break;
      }
      case "openBenchmarkSource": {
        const { trustedBenchmarkSource } = require("./benchmarkTables");
        const benchmark = this.benchmarksData.benchmarks.find(
          (candidate) => candidate.id === msg.benchmarkId,
        );
        const source = trustedBenchmarkSource(msg.sourceRole === "methodology"
          ? benchmark?.methodology_url ?? ""
          : msg.sourceRole === "score" ? benchmark?.score_url ?? "" : benchmark?.url ?? "");
        if (source) await vscode.env.openExternal(vscode.Uri.parse(source));
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
      case "setAgentGroup": {
        // The webview already re-rendered optimistically; this only makes the choice survive the
        // next refresh, so no round-trip is on the interaction's critical path.
        const next = (msg as { group?: string }).group;
        if (next === "project" || next === "provider" || next === "model" || next === "none") {
          this.agentGroupBy = next;
        }
        break;
      }
    }
  }

  private promptMessage(output: string): string {
    try { return JSON.parse(output).error ?? output.slice(0, 4096); }
    catch { return output.trim().slice(0, 4096); }
  }

  private async promptCall(args: string[], stdin = ""): Promise<{ ok: boolean; output: string }> {
    const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (!workspaceRoot) return { ok: false, output: "Open a workspace before using prompts." };
    const backend = await resolveConversationBackend({
      projectPath: cfg().get<string>("projectPath") || process.env.INTERACT_PROJECT_PATH,
      extensionVersion: conversationExtensionVersion(), workspaceRoot,
    });
    if (!backend.available) return { ok: false, output: backend.reason };
    return promptRequest(backend, args, stdin);
  }

  private async loadPromptCatalog(): Promise<PromptEditorState> {
    const result = await this.promptCall(["catalog"]);
    if (!result.ok) return { files: [], status: this.promptMessage(result.output) };
    try {
      const payload = JSON.parse(result.output) as { files: string[] };
      const state: PromptEditorState = { files: payload.files };
      return payload.files[0] ? await this.readPrompt(payload.files[0], state) : state;
    } catch { return { files: [], status: "The prompt catalog response was invalid." }; }
  }

  private async loadPrompt(path: string): Promise<void> {
    this.promptState = await this.readPrompt(path, this.promptState);
  }

  private async readPrompt(path: string, state: PromptEditorState): Promise<PromptEditorState> {
    const result = await this.promptCall(["read", path]);
    if (!result.ok) return { ...state, status: this.promptMessage(result.output) };
    try {
      const payload = JSON.parse(result.output) as { path: string; content: string; digest: string };
      return { ...state, selected: payload.path, content: payload.content,
        digest: payload.digest, status: undefined, revision: (state.revision ?? 0) + 1 };
    } catch { return { ...state, status: "The prompt source response was invalid." }; }
  }

  private statusCell(): CellUpdate {
    const projectPath = cfg().get<string>("projectPath") || "(auto-detect)";
    const billing = cfg().get<"session_only" | "api_allowed">("media.billing") ?? "session_only";
    const backend = cfg().get<"auto" | "session" | "api">("media.backend") ?? "auto";
    const confirmations = cfg().get<string>("media.noExtraUsageConfirmedFor") ?? "";
    return {
      id: "status",
      title: "System Status",
      content: [
        { kind: "row", label: "Extension Active", dot: "ok" },
        { kind: "row", label: "Project:", value: projectPath },
        {
          kind: "row",
          label: "Media:",
          value: presentMediaStatus(
            billing,
            confirmations.split(",").includes("claude"),
            backend,
            billing === "api_allowed" && backend !== "session",
          ),
        },
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
    const personal = this.settingsSnapshot;
    const raw = personal?.configured && personal.portable_keys.includes(s.env)
      ? personal.values[s.env] : cfg().get<string | number | boolean>(s.key);
    const isSet = raw !== undefined && raw !== "";
    let value = isSet ? String(raw) : `auto · ${s.default || "default"}`;
    const action = "changeModel";
    if (s.kind === "model" && isSet) {
      const meta = metaOf(String(raw), this.modelsData);
      if (meta?.input_cost_per_million) {
        value += ` — $${meta.input_cost_per_million}/M in, $${meta.output_cost_per_million ?? 0}/M out`;
      }
    }
    const rows: CellContent[] = s.kind === "model" ? [{
        kind: "row",
        label: s.label,
        value,
        tooltip: s.description,
        actions: [{ type: action, label: "Change", data: { setting: s.key } }],
      }] : [{
        kind: "setting",
        key: s.key,
        label: s.label,
        description: s.description,
        input: s.kind,
        value: isSet ? String(raw) : s.kind === "int" ? String(s.default) : "",
        defaultValue: s.default,
        minimum: s.minimum ?? undefined,
        pattern: s.pattern ?? undefined,
        options: s.options ?? undefined,
      }];
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
      const personal = this.settingsSnapshot;
      if (serverWorkspaceConfigured() && !personal) {
        if (!rows.length) rows.push({ kind: "empty", message: "Personal settings unavailable. Sign in and refresh; local portable values are not used." });
      } else {
        if (!rows.length && personal?.configured) rows.push({ kind: "row", label: "Personal server settings",
          value: `revision ${personal.revision} · ${personal.stale ? "STALE cache — refresh online before saving" : "current"}` });
        rows.push(...this.settingRows(s));
      }
      byGroup.set(s.group, rows);
    }
    return [...byGroup].map(([group, content]) => ({
      id: `cfg-${group.toLowerCase()}`,
      title: group,
      content,
    }));
  }

  /** Benchmark data sources — where each benchmark's live scores come from and the optional key
   *  that source needs. Lets the user supply keys in-UI (no CLI), grouped per benchmark, with a
   *  nudge: live scores let interact recommend the best current model. */
  private benchmarkDataCell(): CellUpdate {
    const content: CellContent[] = [
      { kind: "row", label: "Benchmark source availability and refresh controls." },
    ];
    for (const cat of BENCHMARK_CATEGORIES) {
      const benches = this.benchmarksData.benchmarks.filter((b) => b.category === cat.id);
      if (!benches.length) continue;
      content.push({ kind: "heading", text: cat.label });
      for (const b of benches) {
        if (!b.refresh_supported) {
          content.push({ kind: "row", label: b.name,
            value: `${b.source} · refresh unavailable · informational source`, dot: "missing",
            tooltip: b.description });
        } else if (b.requires_auth && b.source_auth) {
          const set = !!this.keyManager.get(b.source_auth);
          content.push({
            kind: "row",
            label: b.name,
            value: `${b.source} · ${set ? "key set" : "needs a key"}`,
            dot: set ? "ok" : "missing",
            tooltip: `${b.source_auth} — ${b.description}`,
            actions: set
              ? [
                  { type: "clearBenchmarkKey", label: "Clear", data: { key: b.source_auth }, style: "secondary" },
                ]
              : [{ type: "setBenchmarkKey", label: "Add key", data: { key: b.source_auth } }],
          });
        } else {
          content.push({
            kind: "row",
            label: b.name,
            value: `${b.source} · automatic refresh · no key needed`,
            dot: "ok",
            tooltip: b.description,
          });
        }
      }
    }
    return { id: "cfg-benchmarks", title: "Benchmark data", content };
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

  /** Asked BY NAME, because every run names a model the ranking may not carry verbatim
   *  (sonnet, claude-sonnet-5): only a named ask resolves it, and the board on disk fills
   *  whatever the CLI could not. */
  private ensureCompetence(runs: readonly { model?: string | null }[]): void {
    const models = [...new Set(runs.map((r) => r.model).filter((m): m is string => !!m))];
    if (!models.length) return;
    void this.measured.ensure(models).then((known) => { if (known && !this.disposed) this.refresh(); });
  }

  /** The agent team: what is running, what each is doing, and its reported billing path.
   *
   *  A run with no reported cost shows "—", never "$0.00", because unknown is not free. The
   *  shared billing presenter keeps API charges, subscription quota, credits and local compute
   *  separate rather than turning one API-equivalent total into a claim about the user's bill.
   *
   *  Runs interact did NOT spawn (the user's own editor windows) are marked, so the panel is an
   *  honest view of the machine rather than only of our own children.
   */
  private agentsCell(): CellUpdate {
    // Under the workspace scope, like every other surface. "Showing every run on the machine,
    // unscoped, is not an acceptable default" is a recorded decision, and this panel was quietly
    // exempt from it — you switched workspace in the tree and the dashboard kept describing the
    // folder you left.
    const runs = scopeStore()?.runs() ?? readAgentRuns();
    // Never awaited by a paint: the board draws at once and repaints when the measure lands.
    this.ensureCompetence(runs as readonly { model?: string | null }[]);
    if (runs.length === 0) {
      return {
        id: "agents",
        title: "Agent supervision",
        content: [
          {
            kind: "empty",
            message:
              "No agent runs yet. Once a team is working, every run appears here as a lane on a " +
              "shared clock \u2014 who is running, on what, for how long, and who launched whom.",
            hint: 'interact agents run "<task>"',
          },
        ],
      };
    }

    const now = Date.now();
    const lanes: AgentLane[] = withDepth(runs).map(({ run, depth }) => {
      // Python stamps epoch SECONDS; the board works in milliseconds. A record with no start (an
      // older writer) is pinned to `now` so it still draws instead of flying off the axis.
      const cwd = run.cwd || undefined;
      return {
        id: run.run_id,
        name: run.name,
        provider: run.provider,
        model: run.model || undefined,
        // "compare the most competents and trust one agent more than another in some situations".
        competence: this.measured.competence(run.model ?? undefined),
        resolvedModel: this.measured.resolvedId(run.model ?? undefined),
        score: this.measured.scoreOf(run.model ?? undefined),
        project: cwd ? cwd.split(/[\\/]/).filter(Boolean).pop() : undefined,
        cwd,
        task: run.task || undefined,
        // `foreign` is provenance rather than lifecycle, but it is the read that matters here: a
        // session interact did not spawn is drawn as a ghost, never as our own work.
        status: run.foreign ? "foreign" : run.status,
        parentId: run.parent_run_id ?? null,
        depth,
        startedAt: run.started_at ? run.started_at * 1000 : now,
        // An absent `finished_at` stays null all the way to the renderer, which draws an explicit
        // unknown end. Substituting `now` here would invent a duration nothing ever recorded.
        endedAt: run.finished_at != null ? run.finished_at * 1000 : null,
        costUsd: run.cost_usd ?? null,
        chargePath: run.charge_path ?? "unknown",
        costCertainty: run.cost_certainty ?? "unknown",
        last: run.last || undefined,
      };
    });

    const { live } = summarise(runs);
    const billing = billingPresentation(lanes);
    // At least a minute of window, so a team that all started seconds ago still gets an axis.
    const windowStart = Math.min(...lanes.map((l) => l.startedAt), now - 60_000);
    return {
      id: "agents",
      title: "Agent supervision",
      content: [
        {
          kind: "agent-board",
          groupBy: this.agentGroupBy,
          lanes,
          windowStart,
          now,
          billing,
          ariaSummary: `${live} of ${lanes.length} agents running. ${billing.ariaSummary}`,
        },
      ],
    };
  }

  /** Live model metadata, with its age on the label.
   *
   *  The panel used to render `models.json` — baked into the bundle at BUILD time, so prices and
   *  context windows silently aged and nothing said so. This shows the live catalog and, when it
   *  is NOT live (offline, or an old cache), says how old it is rather than presenting a snapshot
   *  as today's truth. No API key: OpenRouter's catalog endpoint is public.
   */
  private async modelsCell(): Promise<CellUpdate> {
    let cat: Catalog | null = null;
    try {
      cat = await loadCatalog();
    } catch {
      cat = null;
    }
    if (!cat || cat.models.length === 0) {
      return {
        id: "models",
        title: "Models",
        content: [
          {
            kind: "empty",
            message:
              "No live catalog yet — needs one network call to openrouter.ai (no API key). " +
              "It will populate on the next refresh when you are online.",
          },
        ],
      };
    }
    const fresh = isLive(cat);
    const age = describeAge(ageSeconds(cat));
    // The top of the board, not a highlight reel: picking N by context length and calling them
    // "worth showing first" meant this comparison table's own leader was whatever had a million
    // tokens, and it dropped every Anthropic model while the panel's hero named three of them.
    const SHOWN = 24;
    const seeing = seeingModels(cat);
    // ONE MODEL PER ROW. The catalog lists a model's variants separately — "(batch)" beside the
    // plain id — so six distinct models filled twelve of the twenty-four rows on a table whose
    // whole purpose is comparing them. Same reduction the benchmarks tab already applies, and the
    // best-measured spelling is the one kept.
    const distinct = new Map<string, typeof seeing[number]>();
    for (const m of seeing) {
      const key = bareModelName(m.id);
      const held = distinct.get(key);
      if (!held || (this.measured.scoreOf(m.id) ?? -1) > (this.measured.scoreOf(held.id) ?? -1)) {
        distinct.set(key, m);
      }
    }
    const ranked = [...distinct.values()].sort(
      (x, y) => (this.measured.scoreOf(y.id) ?? -1) - (this.measured.scoreOf(x.id) ?? -1));
    const rows = ranked.slice(0, SHOWN).map((m) => [
      m.name,
      // "Models should also spell out their intelligence score": this table is the product's own
      // side-by-side comparison and it was the one surface saying nothing about capability.
      shortCompetence(this.measured.competence(m.id))
        ?? (this.measured.unavailable ? "not known here" : "—"),
      m.context_length ? `${Math.round(m.context_length / 1000)}k` : "—",
      m.input_cost_per_token ? `$${(m.input_cost_per_token * 1e6).toFixed(2)}/M` : "—",
      (m.input_modalities || []).filter((x) => x !== "text").join(", ") || "text",
    ]);
    return {
      id: "models",
      // The count says what is ON SCREEN. "Models — 436" over 24 rendered rows is a header
      // restating the list, and it reads as 412 rows that failed to load.
      title: `Models \u2014 top ${Math.min(SHOWN, ranked.length)} of ${ranked.length} distinct models that can see`
        + ` \u00b7 ${fresh ? `live, ${age}` : `${cat.source}, ${age}`}`,
      content: [
        ...(fresh
          ? []
          : ([
              {
                kind: "row",
                label: "Not live",
                value: `showing ${cat.source} data from ${age}`,
                dot: "missing",
              },
            ] as CellContent[])),
        { kind: "table",
          headers: ["Model", "aa.intelligence", "Context", "Input", "Sees"], rows },
      ],
    };
  }

  private async consumptionCell(): Promise<CellUpdate> {
    const all = await readUsageLog(this.usageLogPath());
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

    // Only observed/estimated metered API cost is recorded in USD. Session account impact is
    // unknown because vendor CLIs cannot expose whether a plan allowance or credits were used.
    const currency = cfg().get<string>("display.currency") || "USD";
    const rate = await usdRateTo(currency);
    const sym = currencySymbol(currency);
    const money = (usd: number) => formatMoney(usd, currency, rate);
    const usageSummary = summarizeUsage(entries);

    // a) Spend by provider — horizontal bar
    const provAgg = aggregateByProvider(entries, this.modelsData);
    const provTotal = provAgg.reduce((s, p) => s + p.cost, 0);
    // The label is just the name now: the renderer prints the value and the share itself, so
    // stuffing them into the label duplicated what the bar already says.
    const provBars = provAgg.map((p) => ({
      label: p.provider,
      value: p.cost * rate,
      color: colorFor(p.provider),
    }));
    const topProv = provAgg[0];
    const topPct =
      provTotal > 0 && topProv ? (topProv.cost / provTotal) * 100 : 0;
    const providerCell: CellContent = {
      kind: "bar-h",
      bars: provBars,
      valuePrefix: sym,
      ariaSummary: topProv
        ? `${topProv.provider} accounts for ${topPct.toFixed(0)}% of observed or estimated metered API cost over ${rangeLabel}, ${money(topProv.cost)} of ${money(provTotal)} total.`
        : `No observed metered API cost over ${rangeLabel}.`,
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
      ariaSummary: `Daily observed or estimated metered API cost across ${stacked.series.length} models over the last ${stackedDays} days, total ${money(stackedTotal)}.`,
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
        color: DIM_FOREGROUND,
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
        {
          kind: "row",
          label: "Observed/estimated metered API cost",
          value: money(usageSummary.observedMeteredApiCost),
        },
        ...(usageSummary.sessionUsageCalls
          ? [{
              kind: "row" as const,
              label: "Session usage",
              value: `${usageSummary.sessionUsageCalls} call(s) · account impact unknown`,
            }]
          : []),
        ...(usageSummary.unknownMeteredApiCostCalls
          ? [{
              kind: "row" as const,
              label: "Metered API attempts with unknown cost",
              value: `${usageSummary.unknownMeteredApiCostCalls} failed/cancelled or unpriced call(s)`,
            }]
          : []),
        { kind: "row", label: "Observed/estimated API cost by provider" },
        providerCell,
        { kind: "row", label: `Observed/estimated API cost by model — last ${stackedDays}d` },
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
    // The live leaderboard first: the bundled file describes the benchmarks, but its SCORES age
    // immediately, and a stale ranking presented as current is the defect this fixes.
    const board = readLeaderboard();
    if (board) {
      content.push({ kind: "heading", text: "Model intelligence — Artificial Analysis" });
      content.push({
        kind: "row",
        label: board.isLive ? "live" : "stale",
        value: `${board.scores.length} models · ${describeBoardAge(board.ageSeconds)}`,
        dot: board.isLive ? "ok" : "missing",
        tooltip: "Fetched with your own ARTIFICIAL_ANALYSIS_API_KEY. Their index, their methodology.",
      });
      content.push({
        kind: "table",
        headers: ["Model", "Creator", "Index"],
        rows: board.scores.slice(0, 12).map((s) => [s.name, s.creator, s.intelligence.toFixed(1)]),
      });
    } else {
      content.push({
        kind: "row",
        label: "Model intelligence",
        value: "set ARTIFICIAL_ANALYSIS_API_KEY to fetch live scores (not redistributable, so they cannot ship)",
        dot: "missing",
      });
    }
    for (const cat of BENCHMARK_CATEGORIES) {
      const benches = this.benchmarksData.benchmarks.filter(
        (b) => b.category === cat.id,
      );
      if (!benches.length) continue;
      content.push({ kind: "heading", text: cat.label });
      for (const bench of benches) {
        // What the benchmark measures, and WHEN these scores are from. The date used to live in
        // the tooltip only, so a months-old leaderboard read as today's truth until you hovered.
        const { provenanceLabel, selectionExplanation, trustedBenchmarkSource } = require("./benchmarkTables");
        const asOf = provenanceLabel(bench.published?.retrieved);
        const source = trustedBenchmarkSource(bench.url);
        const scoreReceipt = trustedBenchmarkSource(bench.score_url ?? "");
        const methodology = trustedBenchmarkSource(bench.methodology_url ?? "");
        const scoreMeaning = bench.score_range && bench.higher_is_better !== undefined
          ? `${bench.metric} ${bench.score_range[0]}–${bench.score_range[1]}; ${
            bench.higher_is_better ? "higher" : "lower"
          } is better`
          : "score semantics unavailable";
        content.push({
          kind: "row",
          label: bench.name,
          value: `${bench.description} · ${scoreMeaning} · ${asOf}`,
          tooltip: `${bench.url} · ${asOf}`,
          actions: [
            ...(source ? [{ type: "openBenchmarkSource", label: "Open evaluation",
              data: { benchmarkId: bench.id, sourceRole: "evaluation" } }] : []),
            ...(scoreReceipt ? [{ type: "openBenchmarkSource", label: "Score receipt",
              data: { benchmarkId: bench.id, sourceRole: "score" } }] : []),
            ...(methodology ? [{ type: "openBenchmarkSource", label: "Methodology",
              data: { benchmarkId: bench.id, sourceRole: "methodology" } }] : []),
          ],
        });
        const rows: string[][] = [];
        for (const e of (bench.published?.entries ?? []).slice(0, 5)) {
          const authority = selectionExplanation(e.status ?? "unmapped");
          const freshness = bench.published?.freshness === "current"
            ? "" : ` ${selectionExplanation("stale")}`;
          rows.push([e.model_name, e.score.toFixed(3), `${authority}${freshness}`]);
        }
        for (const [modelId, score] of Object.entries(bench.measured)) {
          rows.push([modelId, score.toFixed(3)]);
        }
        if (rows.length) {
          content.push({ kind: "table", headers: ["Models", "Score", "Eligibility"], rows });
        } else {
          content.push({
            kind: "row",
            label: "  ↳ scores",
            value: bench.refresh_supported && bench.source_auth
              ? `${selectionExplanation("missing")} Configure ${bench.source_auth} in Configuration → Benchmark data.`
              : `${selectionExplanation("missing")} Current data unavailable; this source is informational only.`,
          });
        }
      }
    }
    if (content.length === 0) {
      content.push({ kind: "empty", message: "No benchmark data available" });
    }
    return { id: "benchmarks", title: "Benchmarks by capability", content };
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
    const eligible = bench.recommendations.filter((r) => knownModels.has(r.model_id));
    const excluded = bench.recommendations.filter((r) => !knownModels.has(r.model_id));
    const top = eligible.slice(0, 3);
    if (!top.length) {
      return {
        id: "recommendations",
        title: "Recommendations",
        content: [{ kind: "empty", message: "No matching models in registry; unmapped identities are excluded, never guessed." }],
      };
    }
    const tableRows = top.map((r) => [
      r.model_id,
      r.score.toFixed(3),
      r.source,
      r.cost_per_million != null ? `$${r.cost_per_million.toFixed(2)}` : "—",
      r.quality_per_dollar != null ? r.quality_per_dollar.toFixed(3) : "—",
    ]);
    const content: CellContent[] = [
      {
        kind: "table",
        headers: ["Model", "Score", "Source", "$/M", "Quality/$"],
        rows: tableRows,
      },
    ];
    if (excluded.length) {
      const { selectionExplanation } = require("./benchmarkTables");
      content.push({
        kind: "row", label: "Excluded",
        value: `${excluded.map((item) => item.model_id).join(", ")} — ${selectionExplanation("unmapped")}`,
      });
    }
    return {
      id: "recommendations",
      title: `Recommendations (${bench.name})`,
      content,
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
