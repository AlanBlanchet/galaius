import { execFile } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";

export type ConversationBackend =
  | { available: true; command: string; args: string[]; origin: "project_checkout" | "local_path" }
  | { available: false; reason: string };

export interface ConversationBackendOptions {
  projectPath?: string;
  extensionVersion: string;
  workspaceRoot: string;
  path?: string;
  platform?: NodeJS.Platform;
  pathExt?: string;
}

export function conversationExtensionVersion(): string {
  try {
    const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"));
    return typeof manifest.version === "string" ? manifest.version : "";
  } catch { return ""; }
}

const versionOf = (command: string): Promise<string | undefined> => new Promise((resolve) => {
  execFile(command, ["--version"], { timeout: 3_000, windowsHide: true }, (error, stdout) => {
    if (error) resolve(undefined);
    else resolve(stdout.match(/\d+\.\d+\.\d+/)?.[0]);
  });
});

function canonicalDirectory(value: string): string | undefined {
  try {
    const canonical = fs.realpathSync(value);
    return fs.statSync(canonical).isDirectory() ? canonical : undefined;
  } catch { return undefined; }
}

function executableCandidates(
  searchPath: string,
  platform: NodeJS.Platform,
  pathExt: string,
): string[] {
  const extensions = platform === "win32" ? pathExt.split(";").filter(Boolean) : [""];
  const delimiter = platform === "win32" ? ";" : ":";
  const candidates: string[] = [];
  const seen = new Set<string>();
  for (const directory of searchPath.split(delimiter).filter(path.isAbsolute)) {
    for (const extension of extensions) {
      const candidate = path.join(directory, `interact${extension}`);
      try {
        fs.accessSync(candidate, fs.constants.X_OK);
        const canonical = fs.realpathSync(candidate);
        if (!fs.statSync(canonical).isFile() || seen.has(canonical)) continue;
        fs.accessSync(canonical, fs.constants.X_OK);
        seen.add(canonical);
        candidates.push(canonical);
      } catch { /* keep searching the inherited executable path */ }
    }
  }
  return candidates;
}

export async function resolveConversationBackend(
  options: ConversationBackendOptions,
): Promise<ConversationBackend> {
  const workspaceRoot = canonicalDirectory(options.workspaceRoot);
  if (!workspaceRoot) {
    return { available: false, reason: "The configured workspace directory is unavailable. Choose an existing directory, then reload the window." };
  }
  const args = ["agents", "console", "--workspace-root", workspaceRoot];
  if (options.projectPath?.trim()) {
    const projectPath = canonicalDirectory(options.projectPath.trim());
    if (!projectPath) {
      return { available: false, reason: "The configured project directory is unavailable. Choose an existing directory, then reload the window." };
    }
    return {
      available: true,
      command: "uv",
      args: ["run", "--directory", projectPath, "interact", ...args],
      origin: "project_checkout",
    };
  }
  const candidates = executableCandidates(
    options.path ?? process.env.PATH ?? "",
    options.platform ?? process.platform,
    options.pathExt ?? process.env.PATHEXT ?? ".EXE;.CMD;.BAT",
  );
  if (candidates.length === 0) {
    return { available: false, reason: `Install interact ${options.extensionVersion}, then reload the window.` };
  }
  let observedVersion: string | undefined;
  for (const command of candidates) {
    const foundVersion = await versionOf(command);
    observedVersion ??= foundVersion;
    if (foundVersion === options.extensionVersion) return { available: true, command, args, origin: "local_path" };
  }
  const found = observedVersion ? `Found ${observedVersion}; ` : "The installed version could not be verified; ";
  return {
    available: false,
    reason: `${found}interact ${options.extensionVersion} is required. Update it, then reload the window.`,
  };
}
