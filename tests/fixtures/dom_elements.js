() => {
  const els = document.querySelectorAll('button, input, a, select, textarea');
  return Array.from(els).map(el => {
    const r = el.getBoundingClientRect();
    return {
      name: el.textContent?.trim() || el.placeholder || el.tagName,
      role: el.tagName.toLowerCase(),
      x: Math.round(r.x), y: Math.round(r.y),
      w: Math.round(r.width), h: Math.round(r.height)
    };
  }).filter(e => e.w > 0 && e.h > 0);
}
