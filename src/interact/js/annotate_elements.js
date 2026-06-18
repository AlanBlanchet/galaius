({ scope, limit }) => {
  // Clear every ref from a prior scan FIRST (whole document, not just this scope). An SPA can keep
  // a node alive across re-renders/navigations with its old data-interact-ref still on it; if this
  // scan then hands the same eN to a new node, `[data-interact-ref="eN"]` would match BOTH and a
  // click-by-ref hits Playwright's strict-mode "resolved to 2 elements". Clearing guarantees each
  // ref is unique to the current snapshot (#29).
  document
    .querySelectorAll("[data-interact-ref]")
    .forEach((el) => el.removeAttribute("data-interact-ref"));

  const root = (scope ? document.querySelector(scope) : document.body) || document.body;
  const tags =
    "a,button,input,select,textarea,[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=combobox],[role=textbox],[draggable=true],[role=listitem][aria-grabbed],[role=option]";
  const vw = window.innerWidth;
  const vh = window.innerHeight;

  // Keep only elements a user could actually act on: visible, enabled, not aria-hidden,
  // not collapsed. (display:none yields a 0×0 rect so it's caught by the size gate.)
  const actionable = (el, r) => {
    if (r.width <= 4 || r.height <= 4) return false;
    if (el.disabled === true || el.getAttribute("aria-disabled") === "true") return false;
    if (el.closest('[aria-hidden="true"]')) return false;
    const s = getComputedStyle(el);
    if (s.visibility === "hidden" || s.visibility === "collapse") return false;
    if (s.pointerEvents === "none") return false;
    if (parseFloat(s.opacity) === 0) return false;
    return true;
  };

  // True only when the element's own centre is the topmost hit — i.e. nothing (cookie
  // banner, modal, sticky header) is covering it. Skipped for off-screen centres, where
  // elementFromPoint can't report. Used for ranking, never to drop: a hit-test miss must
  // not hide a real control.
  const unoccluded = (el, r) => {
    const cx = r.x + r.width / 2;
    const cy = r.y + r.height / 2;
    if (cx < 0 || cy < 0 || cx > vw || cy > vh) return false;
    const hit = document.elementFromPoint(cx, cy);
    return !!hit && (hit === el || el.contains(hit) || hit.contains(el));
  };

  const accessibleName = (el) => {
    const img = el.querySelector("img");
    return (
      el.getAttribute("aria-label") ||
      el.value ||
      el.getAttribute("placeholder") ||
      el.getAttribute("title") ||
      el.textContent ||
      (img && (img.getAttribute("alt") || img.getAttribute("aria-label"))) || // icon/logo links
      ""
    )
      .trim()
      .replace(/\s+/g, " ")
      .slice(0, 60);
  };

  const candidates = Array.from(root.querySelectorAll(tags))
    .map((el) => ({ el, r: el.getBoundingClientRect() }))
    .filter(({ el, r }) => actionable(el, r))
    .map(({ el, r }) => {
      const inView = r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw;
      // tier 0: visible & clickable now · 1: visible but covered · 2: off-screen (scroll to it)
      const tier = inView ? (unoccluded(el, r) ? 0 : 1) : 2;
      return { el, r, tier };
    });

  // Rank by tier, then reading order — so a fixed `limit` spends on the controls the user
  // can actually reach, not whatever happened to come first in document order.
  candidates.sort((a, b) => a.tier - b.tier || a.r.top - b.r.top || a.r.left - b.r.left);

  return candidates.slice(0, limit || 50).map(({ el, r }, i) => {
    const ref = "e" + (i + 1); // assigned in FINAL order: ref eN === returned index N
    el.setAttribute("data-interact-ref", ref);
    return {
      ref,
      tag: el.tagName.toLowerCase(),
      name: accessibleName(el),
      x: r.x,
      y: r.y,
      width: r.width,
      height: r.height,
    };
  });
};
