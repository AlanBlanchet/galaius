import { strict as assert } from "node:assert";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

// Owner 2026-10-25 (dictated): « elle aura un CMD qui va s'ouvrir … C'est vraiment un red flag de fou. » The extension
// host has no console: on Windows, a console program it starts without `windowsHide` opens a cmd window on the
// person's desktop. Every `execFile` / `spawn` / `exec` / `fork` of child_process in the extension says
// `windowsHide: true` (`galaiusCli` is the one way to run the galaius CLI); a call without it is named here.

const SPAWN = /\b(execFile|execFileSync|spawn|spawnSync|exec|execSync|fork)\(/g;

test("every program the extension starts hides its window on Windows", () => {
  const folder = new URL(".", import.meta.url).pathname;
  const offenders: string[] = [];
  for (const name of readdirSync(folder).filter((file) => file.endsWith(".ts") && !file.endsWith(".test.ts"))) {
    const source = readFileSync(join(folder, name), "utf8");
    if (!/child_process/.test(source)) continue;
    for (const match of source.matchAll(SPAWN)) {
      // The call's own argument list: up to its matching closing parenthesis.
      let depth = 0, end = match.index! + match[0].length - 1;
      for (; end < source.length; end++) {
        if (source[end] === "(") depth++;
        else if (source[end] === ")" && --depth === 0) break;
      }
      const call = source.slice(match.index!, end + 1);
      if (source[match.index! - 1] === ".") continue;  // a method of another object (RegExp.exec), never child_process
      if (!/windowsHide:\s*true/.test(call)) {
        offenders.push(`${name}:${source.slice(0, match.index).split("\n").length}: ${call.split("\n")[0].trim()}`);
      }
    }
  }
  assert.deepEqual(offenders, []);
});
