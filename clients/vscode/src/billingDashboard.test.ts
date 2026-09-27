/** Billing truth from persisted AgentRun facts through the real dashboard projection and webview.
 *
 * The conversation renderer already exposed charge paths, but its earlier test was falsely named
 * as main-board coverage. This crosses the compiled DashboardPanel mapping and the bundled primary
 * webview so dropped wire fields or dishonest visible/ARIA copy fail together.
 */
import { strict as assert } from "node:assert";
import { createRequire } from "node:module";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

import type { AgentRun } from "./generated/types.ts";
import type { CellUpdate } from "./shared.ts";
import { billingPresentation } from "./billingPresentation.ts";

const require_ = createRequire(import.meta.url);

test("the Agents sidebar delegates its tooltip to the shared billing presenter", () => {
  const source = fs.readFileSync(
    path.resolve(path.dirname(fileURLToPath(import.meta.url)), "agentsView.ts"),
    "utf8",
  );
  assert.match(source, /new vscode\.MarkdownString\(runTooltip\(run\)\)/,
    "the sidebar tooltip must use the shared charge-path wording");
  assert.doesNotMatch(source, /subscription run already paid/i,
    "the prior unsupported account-impact claim must not return beside the shared presenter");
});

class TestNode {
  readonly children: TestNode[] = [];
  private value = "";

  appendChild(child: TestNode): TestNode {
    this.children.push(child);
    return child;
  }

  replaceChildren(...children: TestNode[]): void {
    this.children.splice(0, this.children.length, ...children);
    this.value = "";
  }

  get textContent(): string {
    return this.value + this.children.map((child) => child.textContent).join("");
  }

  set textContent(value: string) {
    this.value = value;
    this.children.splice(0);
  }
}

class TestElement extends TestNode {
  readonly attributes = new Map<string, string>();
  readonly style: Record<string, string> = {};
  readonly tagName: string;
  className = "";

  constructor(tagName: string) {
    super();
    this.tagName = tagName;
  }

  set id(value: string) { this.setAttribute("id", value); }
  get id(): string { return this.getAttribute("id") ?? ""; }

  setAttribute(name: string, value: string): void {
    this.attributes.set(name, value);
    if (name === "class") this.className = value;
  }

  getAttribute(name: string): string | null {
    return this.attributes.get(name) ?? null;
  }

  addEventListener(): void {}

  querySelector(selector: string): TestElement | null {
    return selector.startsWith("#") ? this.findId(selector.slice(1)) ?? null : null;
  }

  querySelectorAll(): TestElement[] { return []; }

  contains(candidate: TestNode): boolean {
    return this === candidate || this.children.some((child) =>
      child === candidate || child instanceof TestElement && child.contains(candidate));
  }

  findClass(name: string): TestElement | undefined {
    if (this.className.split(/\s+/).includes(name)) return this;
    for (const child of this.children) {
      if (child instanceof TestElement) {
        const found = child.findClass(name);
        if (found) return found;
      }
    }
    return undefined;
  }

  findId(id: string): TestElement | undefined {
    if (this.id === id) return this;
    for (const child of this.children) {
      if (child instanceof TestElement) {
        const found = child.findId(id);
        if (found) return found;
      }
    }
    return undefined;
  }
}

class TestHtmlElement extends TestElement {}
class TestSvgElement extends TestElement {}

function installDom(): {
  root: TestHtmlElement;
  dispatch(update: CellUpdate): void;
} {
  const root = new TestHtmlElement("div");
  root.id = "root";
  const body = new TestHtmlElement("body");
  body.appendChild(root);
  const listeners = new Map<string, ((event: { data: unknown }) => void)[]>();
  const document = {
    body,
    activeElement: null,
    createDocumentFragment: () => new TestNode(),
    createElement: (tag: string) => new TestHtmlElement(tag),
    createElementNS: (_namespace: string, tag: string) => new TestSvgElement(tag),
    createTextNode: (text: string) => {
      const node = new TestNode();
      node.textContent = text;
      return node;
    },
    getElementById: (id: string) => body.findId(id) ?? null,
  };
  const window = {
    addEventListener: (kind: string, listener: (event: { data: unknown }) => void) => {
      listeners.set(kind, [...(listeners.get(kind) ?? []), listener]);
    },
    setTimeout,
    clearTimeout,
  };
  Object.assign(globalThis, {
    Node: TestNode,
    HTMLElement: TestHtmlElement,
    SVGElement: TestSvgElement,
    document,
    window,
    acquireVsCodeApi: () => ({ postMessage: () => undefined }),
  });
  return {
    root,
    dispatch(update: CellUpdate): void {
      for (const listener of listeners.get("message") ?? []) {
        listener({ data: { type: "cellUpdate", cell: update } });
      }
    },
  };
}

const runs = [
  { run_id: "session", provider: "provider-a", name: "session", status: "done",
    started_at: 100, finished_at: 105, charge_path: "subscription_quota",
    cost_certainty: "unknown", cost_usd: 2.4 },
  { run_id: "metered", provider: "provider-b", name: "metered", status: "done",
    started_at: 101, finished_at: 104, charge_path: "metered_api",
    cost_certainty: "known", cost_usd: 0.32 },
  { run_id: "credit", provider: "provider-c", name: "credit", status: "done",
    started_at: 102, finished_at: 104, charge_path: "usage_credit",
    cost_certainty: "known", cost_usd: 0.18 },
  { run_id: "local", provider: "local", name: "local", status: "done",
    started_at: 103, finished_at: 106, charge_path: "local_compute",
    cost_certainty: "unknown", cost_usd: null },
  { run_id: "unclassified", provider: "provider-d", name: "unknown", status: "running",
    started_at: 104, finished_at: null, charge_path: "unknown",
    cost_certainty: "unknown", cost_usd: null },
] satisfies AgentRun[];

test("billing copy distinguishes every charge path without inventing zero or coverage", () => {
  const presentation = billingPresentation(runs.map((run) => ({
    chargePath: run.charge_path,
    costCertainty: run.cost_certainty,
    costUsd: run.cost_usd,
  })));
  const text = presentation.lines.map((line) => line.text).join(" ");
  assert.equal(presentation.heading, "Mixed billing paths");
  for (const expected of [
    /subscription quota.*impact not reported/i,
    /\$0\.3200 observed metered API charge/i,
    /\$0\.1800 usage-credit value/i,
    /local hardware and energy cost not measured/i,
    /unknown charge path and account impact/i,
  ]) assert.match(text, expected);
  assert.match(
    billingPresentation([{
      chargePath: "metered_api", costCertainty: "unknown", costUsd: 0.12,
    }]).ariaSummary,
    /~\$0\.1200 metered API estimate.*final provider charge not reported/i,
  );
  assert.doesNotMatch(text, /already covered|\$0(?:\.0+)?(?![\d.])/i);
});

test("the primary board keeps every billing path honest in visible and aria copy", () => {
  const built = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "out");
  const Module = require_("node:module") as {
    _load: (request: string, parent: unknown, isMain: boolean) => unknown;
  };
  const originalLoad = Module._load;
  const vscodeStub = new Proxy({}, {
    get: () => new Proxy(() => undefined, { get: () => undefined }),
  });
  Module._load = (request, parent, isMain) => {
    if (request === "vscode") return vscodeStub;
    if (request === "./scopeStore") return { scopeStore: () => ({ runs: () => runs }) };
    return originalLoad(request, parent, isMain);
  };
  try {
    const dashboardPath = path.join(built, "dashboard.js");
    delete require_.cache[require_.resolve(dashboardPath)];
    const { DashboardPanel } = require_(dashboardPath);
    const panel = Object.create(DashboardPanel.prototype) as {
      agentGroupBy: "project";
      agentsCell(): CellUpdate;
    };
    panel.agentGroupBy = "project";
    const cell = panel.agentsCell();
    const board = cell.content.find((item) => item.kind === "agent-board");
    assert.ok(board && board.kind === "agent-board");

    const dom = installDom();
    const webviewPath = path.join(built, "webview.js");
    delete require_.cache[require_.resolve(webviewPath)];
    require_(webviewPath);
    dom.dispatch(cell);
    const boardElement = dom.root.findClass("board");
    assert.ok(boardElement);
    const metered = board.lanes.find((lane) => lane.id === "metered") as typeof board.lanes[number] & {
      chargePath?: string;
      costCertainty?: string;
    };
    const text = boardElement.textContent;
    const aria = boardElement.getAttribute("aria-label") ?? "";

    assert.deepEqual({
      chargePath: metered.chargePath ?? null,
      costCertainty: metered.costCertainty ?? null,
      falselyCovered: /already covered/i.test(text),
      visibleHasMetered: /metered api/i.test(text),
      ariaHasMetered: /metered api/i.test(aria),
      ariaHasMixed: /mixed/i.test(aria),
      visibleHasSubscription: /subscription quota/i.test(text),
      visibleHasCredit: /usage-credit/i.test(text),
      visibleHasLocal: /local hardware/i.test(text),
      visibleHasUnknown: /unknown charge path/i.test(text),
      inventedZero: /\$0(?:\.0+)?(?![\d.])/i.test(text),
      ariaInventedZero: /\$0(?:\.0+)?(?![\d.])/i.test(aria),
    }, {
      chargePath: "metered_api",
      costCertainty: "known",
      falselyCovered: false,
      visibleHasMetered: true,
      ariaHasMetered: true,
      ariaHasMixed: true,
      visibleHasSubscription: true,
      visibleHasCredit: true,
      visibleHasLocal: true,
      visibleHasUnknown: true,
      inventedZero: false,
      ariaInventedZero: false,
    });
  } finally {
    Module._load = originalLoad;
  }
});
