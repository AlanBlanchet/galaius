import { strict as assert } from "node:assert";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";
import { transcriptFragment } from "./conversationFormat.ts";
import { buildRail } from "./rail.ts";
import { railHtml } from "./railHtml.ts";
import { voiceOf } from "./statusLanguage.ts";

const chrome = process.env.CHROME_BIN ?? "/usr/bin/google-chrome";

function render(page: string, profile: string): string {
  return execFileSync(chrome, ["--headless=new", "--no-sandbox", "--disable-gpu",
    `--user-data-dir=${profile}`, "--dump-dom", page], { encoding: "utf8", maxBuffer: 32 << 20 });
}

test("real browser keeps long transcript and Team interactions responsive and fully reachable", () => {
  assert.ok(fs.existsSync(chrome));
  const root = path.resolve("../out/tests/20260906-panels-conversation-workspace/browser-perf");
  fs.rmSync(root, { recursive: true, force: true });
  fs.mkdirSync(root, { recursive: true });
  const turns = Array.from({ length: 300 }, (_, index) => ({ kind: "text" as const,
    text: `${index}: ${"payload ".repeat(100)}` }));
  turns.splice(150, 0, { kind: "tool", tool_id: "middle-tool", tool: "read_file",
    tool_input: "middle exact input" } as never, { kind: "tool_result", tool_id: "middle-tool",
    text: "middle exact full output" } as never);
  const fragment = transcriptFragment({ turns, name: "long", awaitingReply: false });
  const encodedFragment = Buffer.from(fragment + '<p id="appended">appended exact tail</p>').toString("base64");
  const chat = `<!doctype html><meta charset="utf-8"><main id="transcript"><div id="transcript-content">${fragment}</div></main><script>
try { const perfMeasureStart=performance.now();
const perfBeforeHeight=document.getElementById('transcript').scrollHeight;
const perfBytes=Uint8Array.from(atob('${encodedFragment}'),character=>character.charCodeAt(0));
document.getElementById('transcript-content').innerHTML=new TextDecoder().decode(perfBytes);
const perfTool=document.querySelector('details[data-tool-id="middle-tool"]'); perfTool.open=true;
const perfAfterHeight=document.getElementById('transcript').scrollHeight;
document.body.dataset.perf=String(performance.now()-perfMeasureStart);
document.body.dataset.complete=String(Boolean(perfBeforeHeight>0&&perfAfterHeight>0&&perfTool.open&&document.body.textContent.includes('middle exact full output')&&document.getElementById('appended')));
} catch(error) { document.body.dataset.perfError=String(error); }
</script>`;
  const runs = Array.from({ length: 300 }, (_, index) => ({ run_id: `run-${index}`,
    name: `agent-${index}`, provider: index % 2 ? "claude" : "codex", status: "running",
    started_at: 100 + index, task: `realistic task ${index}` })) as never[];
  const rail = railHtml(buildRail(runs, "workspace", () => 0), "perf", voiceOf,
    (run) => run.task ?? run.name, (run) => ({ id: run.name, label: run.name }))
    .replace("const api = (window.__wpApi ||", "const perfRailStart=performance.now(); const api = (window.__wpApi ||")
    .replace("</script>", `document.body.dataset.perf=String(performance.now()-perfRailStart);
document.body.dataset.complete=String(document.body.textContent.includes('agent-150')&&document.body.textContent.includes('realistic task 299'));</script>`);
  const chatPage = path.join(root, "chat.html"); const teamPage = path.join(root, "team.html");
  fs.writeFileSync(chatPage, chat); fs.writeFileSync(teamPage, rail);
  try {
    for (const [page, name] of [[chatPage, "chat"], [teamPage, "team"]] as const) {
      const dom = render(page, path.join(root, `${name}-profile`));
      assert.match(dom, /data-complete="true"/, /data-perf-error="([^"]+)/.exec(dom)?.[1]);
      const elapsed = Number(/data-perf="([0-9.]+)"/.exec(dom)?.[1]);
      assert.ok(Number.isFinite(elapsed) && elapsed < 500, `${name} DOM interaction took ${elapsed}ms`);
      process.stdout.write(`# ${name} DOM interaction ${elapsed.toFixed(2)}ms\n`);
    }
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
