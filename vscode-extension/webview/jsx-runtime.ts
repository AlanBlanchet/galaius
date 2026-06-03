declare global {
  namespace JSX {
    type Element = Node;
    interface IntrinsicElements {
      [tag: string]: Record<string, unknown>;
    }
  }
}

const SVG_TAGS = new Set([
  "svg",
  "path",
  "circle",
  "line",
  "polyline",
  "text",
  "g",
  "rect",
  "defs",
  "linearGradient",
  "stop",
  "use",
  "symbol",
  "clipPath",
  "ellipse",
  "polygon",
]);
const SVG_NS = "http://www.w3.org/2000/svg";

type Props = Record<string, unknown> | null;
type Child =
  | HTMLElement
  | SVGElement
  | DocumentFragment
  | string
  | number
  | boolean
  | null
  | undefined;

function appendChild(parent: Node, child: Child | Child[]): void {
  if (child == null || typeof child === "boolean") return;
  if (Array.isArray(child)) {
    for (const c of child) appendChild(parent, c);
  } else if (typeof child === "string" || typeof child === "number") {
    parent.appendChild(document.createTextNode(String(child)));
  } else {
    parent.appendChild(child);
  }
}

function applyChildren(el: Node, children: unknown): void {
  if (Array.isArray(children)) {
    for (const c of children) appendChild(el, c as Child);
  } else {
    appendChild(el, children as Child);
  }
}

export function jsx(
  tag: string | typeof Fragment | ((props: Props) => Node),
  props: Props,
): Node {
  if (typeof tag === "function") {
    return tag(props);
  }

  if (tag === Fragment) {
    const frag = document.createDocumentFragment();
    if (props?.children) applyChildren(frag, props.children);
    return frag;
  }

  const el = SVG_TAGS.has(tag as string)
    ? document.createElementNS(SVG_NS, tag as string)
    : document.createElement(tag as string);

  if (!props) return el;

  const { children, ...rest } = props;

  for (const [key, value] of Object.entries(rest)) {
    if (value == null) continue;
    if (key === "className") {
      if (el instanceof SVGElement) el.setAttribute("class", value as string);
      else (el as HTMLElement).className = value as string;
    } else if (key === "textContent") {
      el.textContent = value as string;
    } else if (key === "style" && typeof value === "object") {
      Object.assign((el as HTMLElement).style, value);
    } else if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2).toLowerCase(), value as EventListener);
    } else {
      el.setAttribute(key, String(value));
    }
  }

  if (children != null) applyChildren(el, children);

  return el;
}

export { jsx as jsxs };

export function Fragment(_props: Props): DocumentFragment {
  return document.createDocumentFragment();
}
