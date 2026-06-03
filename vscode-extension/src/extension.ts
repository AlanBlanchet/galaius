import * as vscode from "vscode";
import { DashboardPanel } from "./dashboard";
import {
  KeyManager,
  formatLabel,
  resolveCommand,
  ModelsData,
  ModelInfo,
  ProviderInfo,
} from "./shared";

const SETTING_SECTION = "interact";
const IS_SECRET_RE = /KEY|SECRET|TOKEN/i;

interface ModelSettingItem extends vscode.QuickPickItem {
  settingKey: string;
}

const SETTING_ENV_MAP: Record<string, string> = {
  "image.model": "INTERACT_IMAGE_MODEL",
  "video.model": "INTERACT_VIDEO_MODEL",
  "video.fps": "INTERACT_VIDEO_FPS",
  "video.duration": "INTERACT_VIDEO_DURATION",
  "component.model": "INTERACT_COMPONENT_MODEL",
  "browser.headless": "INTERACT_HEADLESS",
  "browser.type": "INTERACT_BROWSER_TYPE",
  "browser.viewportWidth": "INTERACT_VIEWPORT_WIDTH",
  "browser.viewportHeight": "INTERACT_VIEWPORT_HEIGHT",
  "browser.slowMo": "INTERACT_SLOW_MO",
  "vlm.maxTokens": "INTERACT_MAX_TOKENS",
  "vlm.waitTimeout": "INTERACT_WAIT_TIMEOUT",
  "debug.dir": "INTERACT_SCREENSHOT_DUMP_DIR",
  "desktop.target": "INTERACT_DESKTOP_TARGET",
  "desktop.nestedDisplay": "INTERACT_NESTED_DISPLAY",
  "desktop.nestedSize": "INTERACT_NESTED_SIZE",
};

const SETTING_TO_TASK: Record<string, string> = {
  "image.model": "image",
  "video.model": "video",
  "component.model": "component",
};

function cfg() {
  return vscode.workspace.getConfiguration(SETTING_SECTION);
}

function buildEnv(
  keyManager: KeyManager,
  allEnvKeys: Set<string>,
  modelsData: ModelsData,
): Record<string, string> {
  const c = cfg();
  const env: Record<string, string> = {};

  for (const [k, v] of Object.entries(process.env)) {
    if (v !== undefined && !allEnvKeys.has(k)) {
      env[k] = v;
    }
  }

  for (const [settingKey, envKey] of Object.entries(SETTING_ENV_MAP)) {
    const value = c.get(settingKey);
    if (value === undefined || value === null || value === "") continue;
    env[envKey] =
      typeof value === "boolean" ? (value ? "true" : "false") : String(value);
  }

  for (const [k, v] of keyManager.entries()) {
    env[k] = v;
    const alias = modelsData.keyAliases?.[k];
    if (alias && !env[alias]) {
      env[alias] = v;
    }
  }

  // Ollama cloud: auto-set API base when key is present but base is not
  if (env["OLLAMA_API_KEY"] && !env["OLLAMA_API_BASE"]) {
    env["OLLAMA_API_BASE"] = "https://api.ollama.com";
  }

  // Determine which providers are fully configured
  const configuredProviders: string[] = [];
  for (const [provider, info] of Object.entries(modelsData.providers)) {
    if (info.envKeys.length === 0) continue;
    if (info.envKeys.every((k: string) => env[k])) {
      configuredProviders.push(provider);
    }
  }
  if (configuredProviders.length > 0) {
    env["INTERACT_CONFIGURED_PROVIDERS"] = configuredProviders.join(",");
  }

  env["INTERACT_MODELS_JSON"] = JSON.stringify(modelsData);

  if (modelsData.defaults) {
    for (const [setting, envKey] of Object.entries(SETTING_ENV_MAP)) {
      if (
        setting in SETTING_TO_TASK &&
        !env[envKey] &&
        modelsData.defaults[setting]
      ) {
        env[envKey] = modelsData.defaults[setting];
      }
    }
  }

  return env;
}

function providerOf(model: string, modelsData: ModelsData): string | undefined {
  for (const [provider, info] of Object.entries(modelsData.providers)) {
    if (model in info.models) return provider;
  }
}

async function ensureKeys(
  provider: string,
  modelsData: ModelsData,
  keyManager: KeyManager,
  emitter: vscode.EventEmitter<void>,
): Promise<boolean> {
  const info = modelsData.providers[provider];
  if (!info) return true;
  const missing = keyManager.missingKeys(info.envKeys);
  for (const key of missing) {
    if (process.env[key]) {
      const use = await vscode.window.showInformationMessage(
        `Found ${key} in your environment. Use it?`,
        "Yes",
        "No",
      );
      if (use === "Yes") {
        await keyManager.set(key, process.env[key]!);
        emitter.fire();
        continue;
      }
    }
    const value = await vscode.window.showInputBox({
      prompt: `Enter your ${key}`,
      password: IS_SECRET_RE.test(key),
      ignoreFocusOut: true,
    });
    if (!value) return false;
    await keyManager.set(key, value);
    emitter.fire();
  }
  return true;
}

async function selectModel(
  modelsData: ModelsData,
  keyManager: KeyManager,
  emitter: vscode.EventEmitter<void>,
): Promise<void> {
  const modelKeys = Object.keys(SETTING_TO_TASK);
  if (!modelKeys.length) return;

  let settingKey: string;
  if (modelKeys.length === 1) {
    settingKey = modelKeys[0];
  } else {
    const picked = await vscode.window.showQuickPick<ModelSettingItem>(
      modelKeys.map((k) => ({
        label: formatLabel(k),
        description: modelsData.taskDescriptions?.[SETTING_TO_TASK[k]] ?? "",
        settingKey: k,
      })),
      { placeHolder: "Which model to configure?" },
    );
    if (!picked) return;
    settingKey = picked.settingKey;
  }

  const task = SETTING_TO_TASK[settingKey];
  const recs = task ? (modelsData.recommendations?.[task] ?? []) : [];
  const recSet = new Set(recs);

  const items: vscode.QuickPickItem[] = [];

  const currentModel = cfg().get<string>(settingKey) || "";
  if (currentModel) {
    items.push({
      label: "Current",
      kind: vscode.QuickPickItemKind.Separator,
    });
    items.push({
      label: currentModel,
      description: "currently selected",
    });
  }

  if (recs.length) {
    items.push({
      label: "Recommended",
      kind: vscode.QuickPickItemKind.Separator,
    });
    for (const [i, model] of recs.entries()) {
      if (model === currentModel) continue;
      const provider = providerOf(model, modelsData);
      const meta = provider
        ? modelsData.providers[provider]?.models[model]
        : undefined;
      let desc = `#${i + 1}`;
      if (meta?.intelligence_score)
        desc += ` | Score: ${meta.intelligence_score}`;
      if (meta?.input_cost_per_million) {
        desc += ` | $${meta.input_cost_per_million}/M in`;
      }
      items.push({
        label: model,
        description: desc,
      });
    }
  }

  const allModels: string[] = [];
  for (const [, info] of Object.entries(modelsData.providers)) {
    for (const [name, meta] of Object.entries(info.models)) {
      if (recSet.has(name)) continue;
      allModels.push(name);
    }
  }

  // Group remaining models by provider
  const byProvider: Record<string, string[]> = {};
  for (const name of allModels) {
    const prov = providerOf(name, modelsData) ?? "other";
    (byProvider[prov] ??= []).push(name);
  }

  for (const prov of Object.keys(byProvider).sort()) {
    byProvider[prov].sort();
    items.push({
      label: prov,
      kind: vscode.QuickPickItemKind.Separator,
    });
    for (const name of byProvider[prov]) {
      const meta = modelsData.providers[prov]?.models[name];
      let desc = "";
      if (meta?.intelligence_score) desc += `Score: ${meta.intelligence_score}`;
      if (meta?.input_cost_per_million) {
        desc += desc ? " | " : "";
        desc += `$${meta.input_cost_per_million}/M in`;
      }
      items.push({ label: name, description: desc || undefined });
    }
  }

  if (!items.length) {
    vscode.window.showWarningMessage(
      "No models available. Run generate-models script.",
    );
    return;
  }

  const picked = await vscode.window.showQuickPick(items, {
    placeHolder: `Select ${formatLabel(settingKey).toLowerCase()}`,
    matchOnDescription: true,
  });

  if (!picked) return;

  await cfg().update(
    settingKey,
    picked.label,
    vscode.ConfigurationTarget.Global,
  );

  const provider = providerOf(picked.label, modelsData);
  if (provider) {
    await ensureKeys(provider, modelsData, keyManager, emitter);
  }
}

async function manageApiKeys(
  keyManager: KeyManager,
  modelsData: ModelsData,
  emitter: vscode.EventEmitter<void>,
): Promise<void> {
  const entries = keyManager.entries();
  const items: vscode.QuickPickItem[] = [];

  for (const [key, value] of entries) {
    const masked =
      value.length > 8 ? value.slice(0, 4) + "..." + value.slice(-4) : "****";
    items.push({ label: key, description: masked });
  }
  items.push({ label: "$(add) Add new API key", description: "" });

  const picked = await vscode.window.showQuickPick(items, {
    placeHolder: "Manage API keys",
  });
  if (!picked) return;

  if (picked.label.startsWith("$(add)")) {
    const allKeys = new Set<string>();
    for (const info of Object.values(modelsData.providers)) {
      for (const k of info.envKeys) allKeys.add(k);
    }
    const unconfigured = [...allKeys].filter((k) => !keyManager.get(k)).sort();
    if (!unconfigured.length) {
      vscode.window.showInformationMessage(
        "All provider API keys are already configured.",
      );
      return;
    }
    const keyName = await vscode.window.showQuickPick(unconfigured, {
      placeHolder: "Which API key?",
    });
    if (!keyName) return;
    const value = await vscode.window.showInputBox({
      prompt: `Enter ${keyName}`,
      password: IS_SECRET_RE.test(keyName),
      ignoreFocusOut: true,
    });
    if (value) {
      await keyManager.set(keyName, value);
      emitter.fire();
    }
    return;
  }

  const action = await vscode.window.showQuickPick(
    [{ label: "Update" }, { label: "Remove" }],
    { placeHolder: picked.label },
  );
  if (!action) return;
  if (action.label === "Remove") {
    await keyManager.remove(picked.label);
    emitter.fire();
    vscode.window.showInformationMessage(`Removed ${picked.label}`);
  } else {
    const value = await vscode.window.showInputBox({
      prompt: `Enter new value for ${picked.label}`,
      password: IS_SECRET_RE.test(picked.label),
      ignoreFocusOut: true,
    });
    if (value) {
      await keyManager.set(picked.label, value);
      emitter.fire();
    }
  }
}

export async function activate(
  context: vscode.ExtensionContext,
): Promise<void> {
  let modelsData: ModelsData = { providers: {} };
  try {
    const loaded = require("./models.json");
    if (loaded?.providers) modelsData = loaded;
  } catch {}

  const allEnvKeys = new Set<string>();
  for (const info of Object.values(modelsData.providers)) {
    for (const k of info.envKeys) allEnvKeys.add(k);
  }

  const keyManager = new KeyManager(context.secrets);
  await keyManager.loadAll([...allEnvKeys]);

  const log = vscode.window.createOutputChannel("Interact");
  context.subscriptions.push(log);

  const emitter = new vscode.EventEmitter<void>();
  context.subscriptions.push(
    emitter,
    context.secrets.onDidChange(async (e) => {
      if (!allEnvKeys.has(e.key)) return;
      keyManager.syncCache(e.key, await context.secrets.get(e.key));
      emitter.fire();
      DashboardPanel.refreshIfOpen();
    }),
  );

  try {
    const serverDef = (vscode.lm as any).registerMcpServerDefinitionProvider(
      "interact",
      {
        provideMcpServerDefinitions() {
          const [cmd, args] = resolveCommand(log);
          const env = buildEnv(keyManager, allEnvKeys, modelsData);
          log.appendLine(`Starting: ${cmd} ${args.join(" ")}`);
          return [
            new (vscode as any).McpStdioServerDefinition(
              "Interact",
              cmd,
              args,
              env,
            ),
          ];
        },
        onDidChangeMcpServerDefinitions: emitter.event,
      },
    );
    context.subscriptions.push(serverDef);
    log.appendLine("MCP server definition registered");
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    log.appendLine(`MCP registration failed: ${msg}`);
    vscode.window.showWarningMessage(
      `Interact: MCP server registration failed — ${msg}`,
    );
  }

  let benchmarksData: unknown = { benchmarks: [] };
  try {
    benchmarksData = require("./benchmarks.json");
  } catch {}

  const statusBar = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Right,
    100,
  );
  statusBar.text = "$(eye) Interact";
  statusBar.tooltip = "Open Interact dashboard";
  statusBar.command = "interact.openDashboard";
  statusBar.show();
  context.subscriptions.push(statusBar);

  context.subscriptions.push(
    DashboardPanel.registerSerializer(
      context.extensionUri,
      keyManager,
      modelsData,
      benchmarksData,
      emitter,
    ),
  );

  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (!e.affectsConfiguration(SETTING_SECTION)) return;
      emitter.fire();
      DashboardPanel.refreshIfOpen();
      const c = cfg();
      for (const key of Object.keys(SETTING_TO_TASK)) {
        const val = c.get<string>(key);
        if (val && !providerOf(val, modelsData)) {
          vscode.window.showWarningMessage(
            `Model '${val}' not recognized. Use the "Interact: Select Model" command to pick from available vision models.`,
          );
        }
      }
    }),
    vscode.commands.registerCommand("interact.selectModel", () =>
      selectModel(modelsData, keyManager, emitter),
    ),
    vscode.commands.registerCommand("interact.manageApiKeys", () =>
      manageApiKeys(keyManager, modelsData, emitter),
    ),
    vscode.commands.registerCommand("interact.openDashboard", () =>
      DashboardPanel.createOrShow(
        context.extensionUri,
        keyManager,
        modelsData,
        benchmarksData,
        emitter,
      ),
    ),
  );
}

export function deactivate(): void {}
