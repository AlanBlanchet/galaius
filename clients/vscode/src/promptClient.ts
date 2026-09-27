import { execFile } from "node:child_process";
import type { ConversationBackend } from "./conversationBackend";

export interface PromptEditorState {
  files: string[];
  selected?: string;
  content?: string;
  digest?: string;
  status?: string;
  revision?: number;
}

function promptSpec(backend: Extract<ConversationBackend, { available: true }>, args: string[]): [string, string[]] {
  if (backend.origin === "local_path") return [backend.command, ["prompts", ...args]];
  const interact = backend.args.indexOf("interact");
  return [backend.command, [...backend.args.slice(0, interact + 1), "prompts", ...args]];
}

export function promptRequest(
  backend: Extract<ConversationBackend, { available: true }>,
  args: string[],
  stdin = "",
): Promise<{ ok: boolean; output: string }> {
  const [command, commandArgs] = promptSpec(backend, args);
  return new Promise((resolve) => {
    const child = execFile(command, commandArgs, {
      encoding: "utf8", timeout: 60_000, maxBuffer: 1 << 20, windowsHide: true,
    }, (error, stdout) => {
      if (!error) {
        resolve({ ok: true, output: stdout.slice(0, 1 << 20) });
        return;
      }
      try {
        const failure: unknown = JSON.parse(stdout);
        if (typeof failure === "object" && failure !== null && "code" in failure) {
          if (failure.code === "git_identity" && "error" in failure && typeof failure.error === "string") {
            resolve({ ok: false, output: failure.error });
            return;
          }
          if (failure.code === "conflict") {
            resolve({ ok: false, output: JSON.stringify({ code: "conflict",
              error: "Prompt source changed; preserve the editor buffer and reload from disk explicitly." }) });
            return;
          }
        }
      } catch { /* Only the finite editor protocol is safe to surface. */ }
      resolve({ ok: false,
        output: "Prompt action could not complete; review the local prompt status and try the explicit action again." });
    });
    if (stdin) child.stdin?.end(stdin); else child.stdin?.end();
  });
}
