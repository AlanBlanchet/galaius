/** Everything a worker carries — its name, what it says it is doing, the project it came from —
 *  is agent output, which means it is arbitrary bytes off a disk or a web page. Escaped once,
 *  here, rather than at each of the dozen places the scene interpolates a string. */

export function esc(value: string): string {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** JSON for a <script> body. JSON.stringify alone isn't enough: a </script> inside a worker's
 *  activity string would close the tag and the rest of the state would land on the page as
 *  markup. Line separators are escaped too — they're literal newlines to a JS parser. */
export function jsonInScript(value: unknown): string {
  return JSON.stringify(value)
    .replace(/</g, "\\u003c")
    .replace(/>/g, "\\u003e")
    .replace(/&/g, "\\u0026")
    .replace(/\u2028/g, "\\u2028")
    .replace(/\u2029/g, "\\u2029");
}

/** Clip a phrase for a fixed-width plate without cutting mid-word where it can be helped. */
export function clip(text: string, max: number): string {
  const clean = text.replace(/\s+/g, " ").trim();
  if (clean.length <= max) return clean;
  const cut = clean.slice(0, max - 1);
  const space = cut.lastIndexOf(" ");
  return (space > max * 0.6 ? cut.slice(0, space) : cut) + "…";
}
