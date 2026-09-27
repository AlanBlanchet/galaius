/** The webview's JSX factory, checked on the one behaviour that was silently wrong.
 *
 *  `Fragment` is a function that returns an EMPTY DocumentFragment, and `jsx` used to test
 *  `typeof tag === "function"` BEFORE `tag === Fragment` — so a fragment was called as an ordinary
 *  component and every child was dropped on the floor. The branch that applies children sat below,
 *  unreachable. Nothing in the app uses `<>…</>` yet, so it never surfaced; the strict typecheck
 *  had been flagging it as an impossible comparison for as long as nobody ran that typecheck.
 */
import { strict as assert } from "node:assert";
import { test, before } from "node:test";

class Node_ {
  children: Node_[] = [];
  text = "";
  tag: string;
  constructor(tag: string) { this.tag = tag; }
  appendChild(c: Node_) { this.children.push(c); return c; }
  setAttribute() {}
}

before(() => {
  (globalThis as Record<string, unknown>).document = {
    createDocumentFragment: () => new Node_("#fragment"),
    createElement: (t: string) => new Node_(t),
    createElementNS: (_ns: string, t: string) => new Node_(t),
    createTextNode: (s: string) => Object.assign(new Node_("#text"), { text: s }),
  };
});

test("a fragment keeps its children", async () => {
  const { jsx, Fragment } = await import("../webview/jsx-runtime.ts");
  const frag = jsx(Fragment, { children: ["one", "two"] }) as unknown as Node_;

  assert.equal(frag.tag, "#fragment");
  assert.deepEqual(frag.children.map((c) => c.text), ["one", "two"]);
});

test("an ordinary component is still called with its props", async () => {
  const { jsx } = await import("../webview/jsx-runtime.ts");
  const Comp = (p: Record<string, unknown> | null) =>
    Object.assign(new Node_("custom"), { text: String(p?.label) }) as unknown as globalThis.Node;

  const out = jsx(Comp, { label: "hi" }) as unknown as Node_;
  assert.equal(out.tag, "custom");
  assert.equal(out.text, "hi");
});

test("an intrinsic tag still builds that element with its children", async () => {
  const { jsx } = await import("../webview/jsx-runtime.ts");
  const el = jsx("div", { children: "body" }) as unknown as Node_;

  assert.equal(el.tag, "div");
  assert.deepEqual(el.children.map((c) => c.text), ["body"]);
});
