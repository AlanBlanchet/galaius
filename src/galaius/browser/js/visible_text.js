(a, b) => {
  // One script, two call shapes: page.evaluate(js, opts) hands the OPTIONS first (root → body);
  // locator.evaluate(js, opts) hands the scoped ELEMENT first, then the options.
  const root = a instanceof Node ? a : null;
  const { cap = 2000 } = (root ? b : a) || {};

  // Why not innerText: it drops display:none / visibility:hidden text but NOT opacity:0 — own or
  // inherited — so the summary listed text nobody can see, and a rest-state check read a transparent
  // element as "visible at rest" (#128). This walker reads what a user can SEE.
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "HEAD", "META", "LINK", "TITLE", "SVG"]);

  // No rendered box: display:none, a closed <details> body, a content-visibility:hidden subtree.
  // checkVisibility sees all three where a computed `display` read sees only the first (a folded
  // <details> child still computes display:block). It also answers false for display:contents,
  // whose CHILDREN do render, so that one is exempt; engines without the API fall back to display.
  const unrendered = (el, s) =>
    s.display === "none" ||
    (s.display !== "contents" && typeof el.checkVisibility === "function" && !el.checkVisibility());
  // opacity:0 composites the WHOLE subtree invisible whatever the descendants declare — pruning the
  // subtree at the element is what makes the inherited case fall out.
  const hidesSubtree = (el, s) => unrendered(el, s) || parseFloat(s.opacity) === 0;

  // A raw SVG document has no body (#108); documentElement is the truthful (empty) answer there.
  const start = root || document.body || document.documentElement;
  if (!start) return "";
  for (let n = start.parentElement; n; n = n.parentElement) {
    if (hidesSubtree(n, getComputedStyle(n))) return ""; // an ancestor above the scope hides it whole
  }

  const parts = [];
  let length = 0; // stop walking once past the cap — this runs on big pages
  const walk = (el) => {
    if (length > cap || SKIP.has(el.tagName.toUpperCase())) return;
    if (el.tagName === "BR") {
      parts.push("\n");
      return;
    }
    const s = getComputedStyle(el);
    if (hidesSubtree(el, s)) return;
    if (el.tagName === "SELECT") {
      // A closed dropdown renders only its selected option; a listbox (multiple / size>1) shows them
      // all. innerText lists every option regardless — text not visible at rest, the class this
      // walker exists to drop — while the closed options have no box, so the generic walk sees none.
      const shown = el.multiple || el.size > 1 ? el.options : el.selectedOptions;
      for (const option of shown) {
        const text = option.label.replace(/\s+/g, " ");
        parts.push("\n", text, "\n");
        length += text.length;
      }
      return;
    }
    const block = !(s.display.startsWith("inline") || s.display === "contents");
    // visibility is judged per text node on its parent, never pruning the subtree: a
    // visibility:visible child inside a hidden parent DOES show (innerText agrees).
    const textShown = s.visibility !== "hidden" && s.visibility !== "collapse";
    if (block) parts.push("\n");
    for (const node of el.childNodes) {
      if (length > cap) break;
      if (node.nodeType === Node.ELEMENT_NODE) walk(node);
      else if (node.nodeType === Node.TEXT_NODE && textShown) {
        const text = node.data.replace(/\s+/g, " ");
        parts.push(text);
        length += text.length;
      }
    }
    if (block) parts.push("\n");
  };
  walk(start);
  return parts
    .join("")
    .replace(/ {2,}/g, " ") // adjacent whitespace-only nodes
    .replace(/ ?\n[ \n]*/g, "\n") // one newline per block boundary, no stray spaces around it
    .trim()
    .slice(0, cap);
};
