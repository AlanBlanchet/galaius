import { strict as assert } from "node:assert";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { test } from "node:test";

const chrome = process.env.CHROME_BIN ?? "/usr/bin/google-chrome";
const root = path.resolve("../../out/tests/20260906-panels-conversation-workspace/r7-controls");
const bundle = path.resolve("out/webview.js").replaceAll("'", "%27");

const promptCell = (revision = 1, content = "Loaded Mixed CASE prompt", digest = "d") => ({
  id: "prompt-workspace", title: "Prompt workspace", content: [{ kind: "prompt-workspace",
    files: ["instructions.md"], selected: "instructions.md", content, digest, revision }],
});

const configurationCells = [
  { id: "cfg-models", title: "Models", content: [
    { kind: "setting", key: "backend", label: "Backend", description: "", input: "enum", value: "auto",
      defaultValue: "auto", options: [{ label: "Auto", value: "auto" }, { label: "API", value: "api" }] },
    { kind: "setting", key: "billing", label: "Billing", description: "", input: "enum", value: "session_only",
      defaultValue: "session_only", options: [{ label: "Session only", value: "session_only" }, { label: "API", value: "api_allowed" }] },
    { kind: "setting", key: "criterion", label: "Criterion", description: "", input: "enum", value: "",
      defaultValue: "", options: [{ label: "None", value: "" }, { label: "Fast", value: "fast" }] } ] },
  { id: "cfg-desktop", title: "Desktop", content: [
    { kind: "setting", key: "headless", label: "Headless", description: "", input: "bool", value: "true", defaultValue: "true" } ] },
  { id: "cfg-browser", title: "Browser", content: [
    { kind: "setting", key: "browser", label: "Browser", description: "", input: "enum", value: "chromium",
      defaultValue: "chromium", options: [{ label: "Chromium", value: "chromium" }, { label: "Firefox", value: "firefox" }] },
    { kind: "setting", key: "browser.profile", label: "Profile path", description: "", input: "path",
      value: "/loaded/profile", defaultValue: "" } ] },
];

function render(name: string, cells: unknown[], body: string): unknown {
  const directory = path.join(root, name);
  fs.rmSync(directory, { recursive: true, force: true });
  fs.mkdirSync(directory, { recursive: true });
  const page = path.join(directory, "controls.html");
  fs.writeFileSync(page, `<!doctype html><div id="root"></div>
    <script>window.sent=[];window.acquireVsCodeApi=()=>({postMessage:(message)=>window.sent.push(message)})</script>
    <script src="file://${bundle}"></script><script>
    for (const cell of ${JSON.stringify(cells)}) window.dispatchEvent(new MessageEvent('message',{data:{type:'cellUpdate',cell}}));
    ${body}
    </script>`);
  try {
    const dom = execFileSync(chrome, ["--headless=new", "--no-sandbox", "--disable-gpu",
      `--user-data-dir=${path.join(directory, "profile")}`, "--dump-dom", page], { encoding: "utf8" });
    const encoded = /data-result="([^"]+)"/.exec(dom)?.[1].replaceAll("&quot;", '"');
    assert.ok(encoded, dom);
    return JSON.parse(encoded);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
}

test("setting selections and loaded values render exactly once", () => {
  const result = render("selection", [...configurationCells, promptCell()], `
    [...document.querySelectorAll('button')].find(button=>button.textContent==='Configuration').click();
    const selections=[...document.querySelectorAll('select')].map(select=>({value:select.value,
      selected:[...select.options].filter(option=>option.hasAttribute('selected')).length,
      falseSelected:[...select.options].some(option=>option.getAttribute('selected')==='false')}));
    const profile=document.querySelector('input[aria-label="Profile path"]').value;
    [...document.querySelectorAll('button')].find(button=>button.textContent==='Prompts').click();
    document.body.dataset.result=JSON.stringify({selections,profile,prompt:document.getElementById('prompt-buffer').value});`);
  assert.deepEqual(result, { selections: [
    { value: "auto", selected: 1, falseSelected: false },
    { value: "session_only", selected: 1, falseSelected: false },
    { value: "", selected: 1, falseSelected: false },
    { value: "true", selected: 1, falseSelected: false },
    { value: "chromium", selected: 1, falseSelected: false },
  ], profile: "/loaded/profile", prompt: "Loaded Mixed CASE prompt" });
});

test("a passive setting refresh preserves value focus and selection", () => {
  const browser = configurationCells[2];
  const result = render("setting-focus", configurationCells, `
    [...document.querySelectorAll('button')].find(button=>button.textContent==='Configuration').click();
    const setting=document.querySelector('input[aria-label="Profile path"]');
    setting.focus(); setting.value='/operator/draft'; setting.setSelectionRange(10,15);
    window.dispatchEvent(new MessageEvent('message',{data:{type:'cellUpdate',cell:${JSON.stringify(browser)}}}));
    const refreshed=document.querySelector('input[aria-label="Profile path"]');
    document.body.dataset.result=JSON.stringify({value:refreshed.value,focused:document.activeElement===refreshed,
      start:refreshed.selectionStart,end:refreshed.selectionEnd});`);
  assert.deepEqual(result, { value: "/operator/draft", focused: true, start: 10, end: 15 });
});

test("bool and enum Cancel stay isolated before serial valid saves", () => {
  const result = render("setting-cancel", configurationCells, `
    [...document.querySelectorAll('button')].find(button=>button.textContent==='Configuration').click();
    const backend=document.querySelector('select[aria-label="Backend"]');
    const headless=document.querySelector('select[aria-label="Headless"]');
    backend.value='api'; headless.value='false';
    backend.closest('form').querySelector('button[type="reset"]').click();
    const cancelled={backend:backend.value,headless:headless.value,sent:window.sent.length};
    backend.value='api'; backend.closest('form').requestSubmit();
    headless.value='false'; headless.closest('form').requestSubmit();
    document.body.dataset.result=JSON.stringify({cancelled,saves:window.sent});`);
  assert.deepEqual(result, { cancelled: { backend: "auto", headless: "false", sent: 0 }, saves: [
    { type: "saveSetting", setting: "backend", value: "api" },
    { type: "saveSetting", setting: "headless", value: "false" },
  ] });
});

test("the real bundled viewport constraint blocks zero and submits one positive integer", () => {
  const settings = JSON.parse(fs.readFileSync(path.resolve("src/settings.json"), "utf8"));
  const width = settings.settings.find((item: { key: string }) => item.key === "browser.viewportWidth");
  const cell = { id: "cfg-browser", title: "Browser", content: [{ kind: "setting", key: width.key,
    label: width.label, description: width.description, input: width.kind, value: width.default,
    defaultValue: width.default, minimum: width.minimum }] };
  const result = render("viewport", [cell], `
    [...document.querySelectorAll('button')].find(button=>button.textContent==='Configuration').click();
    const input=document.querySelector('input[aria-label="Viewport width"]');
    input.value='0'; input.closest('form').requestSubmit();
    const invalid={min:input.min,step:input.step,valid:input.checkValidity(),aria:input.getAttribute('aria-invalid'),
      sent:window.sent.filter(message=>message.setting==='browser.viewportWidth').length};
    input.value='1024'; input.dispatchEvent(new Event('input',{bubbles:true})); input.closest('form').requestSubmit();
    document.body.dataset.result=JSON.stringify({invalid,valid:input.checkValidity(),
      saves:window.sent.filter(message=>message.setting==='browser.viewportWidth')});`);
  assert.deepEqual(result, { invalid: { min: "1", step: "1", valid: false, aria: "true", sent: 0 },
    valid: true, saves: [{ type: "saveSetting", setting: "browser.viewportWidth", value: "1024" }] });
});

test("the real bundled Nested size constraint blocks garbage and submits one valid geometry", () => {
  const settings = JSON.parse(fs.readFileSync(path.resolve("src/settings.json"), "utf8"));
  const size = settings.settings.find((item: { key: string }) => item.key === "desktop.nestedSize");
  const cell = { id: "cfg-desktop", title: "Desktop", content: [{ kind: "setting", key: size.key,
    label: size.label, description: size.description, input: size.kind, value: size.default,
    defaultValue: size.default, pattern: size.pattern }] };
  const result = render("nested-size", [cell], `
    [...document.querySelectorAll('button')].find(button=>button.textContent==='Configuration').click();
    const input=document.querySelector('input[aria-label="Nested size"]');
    input.value='garbage'; input.closest('form').requestSubmit();
    const invalid={pattern:input.pattern,valid:input.checkValidity(),message:input.validationMessage,
      aria:input.getAttribute('aria-invalid'),sent:window.sent.filter(message=>message.setting==='desktop.nestedSize').length};
    input.value='1024x768'; input.dispatchEvent(new Event('input',{bubbles:true})); input.closest('form').requestSubmit();
    document.body.dataset.result=JSON.stringify({invalid,valid:input.checkValidity(),
      saves:window.sent.filter(message=>message.setting==='desktop.nestedSize')});`);
  assert.equal((result as any).invalid.pattern, "^[1-9]\\d*x[1-9]\\d*$");
  assert.equal((result as any).invalid.valid, false);
  assert.ok((result as any).invalid.message);
  assert.equal((result as any).invalid.aria, "true");
  assert.equal((result as any).invalid.sent, 0);
  assert.deepEqual((result as any).saves,
    [{ type: "saveSetting", setting: "desktop.nestedSize", value: "1024x768" }]);
});

test("quiet Reload replaces content and base while returning focus", () => {
  const result = render("quiet-reload", [promptCell()], `
    [...document.querySelectorAll('button')].find(button=>button.textContent==='Prompts').click();
    const prompt=document.getElementById('prompt-buffer'); prompt.value='UNSAVED DRAFT';
    [...document.querySelectorAll('button')].find(button=>button.textContent==='Reload from disk').click(); prompt.focus();
    window.dispatchEvent(new MessageEvent('message',{data:{type:'cellUpdate',cell:${JSON.stringify(promptCell(2, "EXTERNAL DISK CONTENT", "external-digest"))}}}));
    const refreshed=document.getElementById('prompt-buffer'); document.body.dataset.result=JSON.stringify({
      value:refreshed.value,digest:refreshed.closest('.prompt-workspace').dataset.digest,
      focused:document.activeElement===refreshed});`);
  assert.deepEqual(result, { value: "EXTERNAL DISK CONTENT", digest: "external-digest", focused: true });
});

test("typing after Reload dispatch preserves new text and the old base", () => {
  const result = render("reload-race", [promptCell()], `
    [...document.querySelectorAll('button')].find(button=>button.textContent==='Prompts').click();
    const prompt=document.getElementById('prompt-buffer');
    [...document.querySelectorAll('button')].find(button=>button.textContent==='Reload from disk').click();
    prompt.value='TYPED AFTER RELOAD'; prompt.dispatchEvent(new Event('input',{bubbles:true}));
    window.dispatchEvent(new MessageEvent('message',{data:{type:'cellUpdate',cell:${JSON.stringify(promptCell(2, "EXTERNAL DISK CONTENT", "external-digest"))}}}));
    const refreshed=document.getElementById('prompt-buffer'); document.body.dataset.result=JSON.stringify({
      value:refreshed.value,digest:refreshed.closest('.prompt-workspace').dataset.digest});`);
  assert.deepEqual(result, { value: "TYPED AFTER RELOAD", digest: "d" });
});

test("typing after Save dispatch preserves new text with the returned base", () => {
  const result = render("save-race", [promptCell()], `
    [...document.querySelectorAll('button')].find(button=>button.textContent==='Prompts').click();
    const prompt=document.getElementById('prompt-buffer'); prompt.value='SUBMITTED';
    [...document.querySelectorAll('button')].find(button=>button.textContent==='Save').click();
    prompt.value='TYPED AFTER SAVE'; prompt.dispatchEvent(new Event('input',{bubbles:true}));
    window.dispatchEvent(new MessageEvent('message',{data:{type:'cellUpdate',cell:${JSON.stringify(promptCell(2, "SUBMITTED", "saved-digest"))}}}));
    const refreshed=document.getElementById('prompt-buffer'); document.body.dataset.result=JSON.stringify({
      value:refreshed.value,digest:refreshed.closest('.prompt-workspace').dataset.digest});`);
  assert.deepEqual(result, { value: "TYPED AFTER SAVE", digest: "saved-digest" });
});
