/** Runs the suite inside a REAL VS Code, headless.
 *
 *  This is the harness whose absence let a panel ship that the user could not reach: it asserts
 *  the extension's CONTRIBUTIONS and COMMANDS from inside the extension host, so "can you get
 *  there" is answered mechanically — no screenshots, which matters because VS Code's own window
 *  captures black under X11 (interact #111).
 */
import { defineConfig } from "@vscode/test-cli";

export default defineConfig({
  files: "out/test/**/*.test.js",
  mocha: { ui: "tdd", timeout: 60_000 },
});
