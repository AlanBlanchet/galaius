({ scope, limit }) => {
  const root = scope ? document.querySelector(scope) : document.body;
  const tags =
    "a,button,input,select,textarea,[role=button],[role=link],[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=combobox],[role=textbox],[draggable=true],[role=listitem][aria-grabbed],[role=option]";
  return Array.from((root || document.body).querySelectorAll(tags))
    .filter((el) => {
      const r = el.getBoundingClientRect();
      return r.width > 4 && r.height > 4;
    })
    .slice(0, limit || 50)
    .map((el, i) => {
      const r = el.getBoundingClientRect();
      const ref = "e" + (i + 1);
      el.setAttribute("data-interact-ref", ref);
      const name = (
        el.textContent ||
        el.value ||
        el.getAttribute("aria-label") ||
        el.getAttribute("placeholder") ||
        el.getAttribute("title") ||
        ""
      )
        .trim()
        .replace(/\s+/g, " ")
        .slice(0, 60);
      return {
        ref,
        tag: el.tagName.toLowerCase(),
        name,
        x: r.x,
        y: r.y,
        width: r.width,
        height: r.height,
      };
    });
};
