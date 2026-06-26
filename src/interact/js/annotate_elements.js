({ scope, limit, nextRef }) => {
  // Refs are STABLE within a session: a node that already carries a data-interact-ref KEEPS it
  // across scans and re-renders, and only a genuinely NEW node gets a fresh ref from a monotonic
  // counter (nextRef — owned by the BrowserManager, reset only when the session closes). Because a
  // number is never reused, two nodes can never collide on one ref, so we no longer clear prior refs
  // — the clearing is exactly what made `e18` point at a different node after an SPA re-render (#35).
  // This also subsumes the #29 uniqueness fix the old clear-every-scan provided.
  let counter = nextRef || 0;

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

  const elements = candidates.slice(0, limit || 50).map(({ el, r }) => {
    let ref = el.getAttribute("data-interact-ref"); // surviving node → its existing ref (stable)
    if (!ref) {
      ref = "e" + ++counter; // a new node → the next unused ref in this session
      el.setAttribute("data-interact-ref", ref);
    }
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
  return { elements, nextRef: counter };
};
