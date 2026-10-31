/** The middle depth: an AGENT, and the tasks it was given.
 *
 *  "on the sidepanel, we should be able to view what TASKS an agent was given, and then proceed to
 *  view the conversation we want etc..."
 *
 *  The sidebar held exactly one depth — a single conversation — so an agent that had run six
 *  errands showed as six unrelated rows somewhere else, with nothing saying they belonged to the
 *  same colleague and no way to pick between them. This is that missing middle, and it is also
 *  where the two things you MANAGE about an agent live: the file that defines it, and the model it
 *  runs on. Both were declared in the company file and neither was reachable.
 *
 *  Pure and host-free: no vscode import, so it's unit-testable and the panel can be restyled
 *  without touching what any of it MEANS.
 */

export type AgentIdentity = {
  id: string;
  title?: string | null;
  department?: string | null;
  /** The model actually in force — your override, when you made one. */
  model?: string | null;
  /** What the company file declares. Shown so an override reads AS an override. */
  declaredModel?: string | null;
  /** The file holding this agent's system prompt, when the company file names one. */
  definitionPath?: string | null;
  /** What it LAST actually ran on. Most agents declare inherit — the vendor picks at spawn — so
   *  the declaration alone says nothing about them, and this panel showed nothing. */
  ranModel?: string | null;
  /** What that model is measured at, spelled out. This is the surface you open to judge ONE
   *  agent, and it carried no number while the roster beside it carried one on every row. */
  competence?: string | null;
};

export type AgentTask = {
  run_id: string;
  task?: string;
  status?: string;
  started_at?: number;
  /** What THIS run actually ran on. An agent's runs are not one model — the panel asserted one
   *  score for six errands and contradicted the row that was clicked to open it. */
  model?: string | null;
  /** And what that model is measured at, spelled out. */
  competence?: string | null;
};

/** The comparable half of a measure, for a place that sits beside a name. Same rule as the
 *  board's: the denominator repeats on every row, so it goes to the title. */
function brieflyMeasured(competence: string | null | undefined): string {
  const text = (competence ?? "").trim();
  if (!text) return "";
  return text.replace(/^closest ranked \S+ · aa\.intelligence /, "≈ ")
    .replace(/^aa\.intelligence /, "").replace(/ of \d+ scored$/, "").replace(/ of \d+$/, "");
}

function esc(v: string): string {
  return v.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

const TITLE_MAX = 72;

function label(t: AgentTask): string {
  const text = (t.task ?? "").replace(/\s+/g, " ").trim();
  if (!text) return t.run_id.slice(0, 8);
  return text.length > TITLE_MAX ? text.slice(0, TITLE_MAX - 1).trimEnd() + "…" : text;
}

/** The agent, its identity, and its errands newest-first. nonce is the host's CSP nonce. */
export function agentView(who: AgentIdentity, tasks: AgentTask[], nonce: string): string {
  const chosen = (who.model ?? "").trim();
  const declared = (who.declaredModel ?? "").trim();
  // An override must READ as an override — a choice you cannot see is one you forget you made,
  // and this exact drift (a pin reverted in the prompt repo) went unnoticed for two days.
  const ran = (who.ranModel ?? "").trim();
  const modelText = chosen || (declared && declared !== "inherit" ? declared : "") || ran
    || "the session's own model";
  // How many models these errands ACTUALLY used. A row on the roster aggregates ×N runs; opening
  // it showed one model and one score, which simply contradicted the number that was clicked
  // whenever those runs had not agreed. The chip says which case this is, in one clause.
  const ranModels = [...new Set(tasks.map((t) => (t.model ?? "").trim()).filter(Boolean))];
  const spread = ranModels.length > 1
    ? `${ranModels.length} models across ${tasks.length} runs`
    : ranModels.length === 1 && tasks.length > 1 ? `all ${tasks.length} runs` : "";
  const modelNote = chosen && declared && chosen !== declared
    ? `overrides ${declared}`
    : chosen ? "chosen here"
    : spread && ranModels.length > 1 ? spread
    : ran && (!declared || declared === "inherit")
      ? `last ran on ${ran}${spread ? ` · ${spread}` : ""}`
    : "declared by the company file";
  // The measure, spelled out on the chip itself: short form beside the model, whole sentence in
  // the title. An approximate resolution keeps its mark here exactly as it does on the board.
  const measured = (who.competence ?? "").trim();
  const brief = measured
    ? measured.replace(/^closest ranked \S+ · aa\.intelligence /, "≈ ")
      .replace(/^aa\.intelligence /, "").replace(/ of \d+$/, "")
    : "";
  const modelTitle = measured
    ? modelText + " — " + measured + " — one measure, never a verdict"
    : "Choose the model " + who.id + " runs on";

  const file = (who.definitionPath ?? "").trim();
  const fileName = file ? file.split("/").pop() ?? file : "";

  const errands = [...tasks]
    .sort((a, b) => (b.started_at ?? 0) - (a.started_at ?? 0))
    .map((t) => `
      <li class="task" data-run="${esc(t.run_id)}" tabindex="0" role="button"${
        t.competence ? ` title="${esc(`${t.model ?? "this run"} — ${t.competence} — one measure, never a verdict`)}"` : ""}>
        <span class="task-what">${esc(label(t))}</span>
        <span class="task-meta">
          <span class="task-run">${esc(t.run_id.slice(0, 8))}</span>
          ${t.model ? `<span class="task-model">${esc(t.model)}${
            brieflyMeasured(t.competence) ? `<span class="task-score">${esc(brieflyMeasured(t.competence))}</span>` : ""
          }</span>` : ""}
          <span class="task-state">${esc(t.status ?? "")}</span>
        </span>
      </li>`).join("");

  const list = errands
    ? `<ul class="tasks">${errands}</ul>`
    : `<p class="none">No tasks yet — nothing has been asked of ${esc(who.id)}.</p>`;

  return `
<section class="agent">
  <header class="who">
    <span class="name">${esc(who.id)}</span>
    ${who.title ? `<span class="role">${esc(who.title)}</span>` : ""}
    ${who.department ? `<span class="dept">${esc(who.department)}</span>` : ""}
  </header>
  <div class="chips">
    <button class="chip" data-action="model" data-agent="${esc(who.id)}"
      title="${esc(modelTitle)}">◈ ${esc(modelText)}${
        brief ? `<span class="score">${esc(brief)}</span>` : ""}<span class="sub">${esc(modelNote)}</span></button>
    ${file ? `<button class="chip" data-action="definition" data-agent="${esc(who.id)}" data-path="${esc(file)}"
      title="Open the file that defines ${esc(who.id)}">◱ ${esc(fileName)}</button>` : ""}
  </div>
  <h3 class="tasks-title">Tasks it was given</h3>
  ${list}
</section>`.trim();
}

/** The styles for the block above. Kept beside it for the same reason the rail's are: one surface,
 *  one place to change how it reads. */
export const AGENT_STYLE = `
.agent { padding: .6em .8em 1em; }
.agent .who { display: flex; flex-wrap: wrap; align-items: baseline; gap: .45em; }
.agent .name { font-weight: 700; font-size: 1.08em; }
.agent .role { color: var(--wp-dim); }
.agent .dept {
  font-size: .8em; letter-spacing: .05em; text-transform: uppercase;
  color: var(--wp-dim); border: 1px solid var(--vscode-panel-border, transparent);
  border-radius: 8px; padding: 0 6px;
}
.agent .chips { display: flex; flex-wrap: wrap; gap: 6px; margin: .7em 0 .2em; }
.agent .chip {
  font: inherit; text-align: left; cursor: pointer;
  display: flex; flex-direction: column; gap: 1px;
  padding: 3px 9px; border-radius: 6px;
  color: var(--vscode-foreground);
  background: var(--vscode-button-secondaryBackground, transparent);
  border: 1px solid var(--vscode-panel-border, var(--wp-dim));
}
.agent .chip:hover { background: var(--vscode-list-hoverBackground); }
.agent .chip:focus-visible { outline: 1px solid var(--vscode-focusBorder); }
.agent .chip .sub { font-size: .8em; color: var(--wp-dim); }
.agent .tasks-title {
  font-size: .82em; letter-spacing: .06em; text-transform: uppercase;
  color: var(--wp-dim); margin: 1.1em 0 .3em; font-weight: 600;
}
.agent .tasks { list-style: none; margin: 0; padding: 0; }
/* A COLUMN, not a row of competitors. Laid out as one flex row, the errand text shared width
   with a run id, a model name and a status and lost: at the sidebar's real ~380px it rendered
   one character per line ("Rev/iew/the/unc/om/mit/ted"). The errand is the row's subject and
   gets full width; everything identifying the run sits under it and wraps as a group. */
.agent .task {
  display: flex; flex-direction: column; gap: 2px;
  padding: 4px 6px; border-radius: 5px; cursor: pointer;
}
.agent .task-meta {
  display: flex; flex-wrap: wrap; align-items: baseline; gap: .1em .55em;
}
.agent .task:hover { background: var(--vscode-list-hoverBackground); }
.agent .task:focus-visible { outline: 1px solid var(--vscode-focusBorder); }
.agent .task-what { overflow-wrap: break-word; }
.agent .task-run {
  font-family: var(--vscode-editor-font-family, monospace);
  font-size: .78em; color: var(--wp-dim); letter-spacing: .02em;
}
.agent .task-model {
  display: inline-flex; align-items: baseline; gap: .35em; min-width: 0;
  font-size: .8em; color: var(--wp-dim);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.agent .task-score { color: var(--vscode-foreground); font-variant-numeric: tabular-nums; }
.agent .task-state { color: var(--wp-dim); font-size: .85em; white-space: nowrap; }
.agent .none { color: var(--wp-dim); }
`;


/** The agent depth as a whole document, for the side panel that hosts it. */
export function agentDocument(nonce: string, body: string, dim: string): string {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>
body { margin: 0; font-family: var(--vscode-font-family); font-size: var(--vscode-font-size, 13px);
       color: var(--vscode-foreground); background: var(--vscode-sideBar-background, var(--vscode-editor-background));
       overflow-x: hidden; }
:root, body { --wp-dim: ${dim}; }
.crumbs { display: flex; align-items: baseline; gap: 5px; padding: .6em .8em 0; font-size: .92em; }
.crumb { font: inherit; cursor: pointer; padding: 0 5px; border-radius: 4px;
         color: var(--vscode-textLink-foreground); background: transparent; border: 1px solid transparent; }
.crumb:hover { background: var(--vscode-list-hoverBackground); }
${AGENT_STYLE}
</style>
</head>
<body>
<div class="crumbs"><button class="crumb" id="team">‹ Team</button></div>
${body}
<script nonce="${nonce}">
  const api = acquireVsCodeApi();
  document.getElementById("team").addEventListener("click", () => api.postMessage({ type: "back" }));
  document.querySelectorAll(".task").forEach((li) => {
    const open = () => api.postMessage({ type: "openRun", runId: li.dataset.run });
    li.addEventListener("click", open);
    li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
  });
  document.querySelectorAll(".chip").forEach((b) => {
    b.addEventListener("click", () => api.postMessage({
      type: "agentAction", action: b.dataset.action, agent: b.dataset.agent, path: b.dataset.path,
    }));
  });
</script>
</body>
</html>`;
}
