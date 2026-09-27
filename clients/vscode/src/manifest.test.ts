/** The manifest, validated the way VS Code validates it.
 *
 *  A contribution point is not "wired unless it throws" — VS Code runs its own validator over
 *  `contributes.*` at every extension-host activation, and a rejected entry is DISCARDED SILENTLY
 *  as far as the product is concerned. The only trace is one line in the extension-host log:
 *
 *      [error] [AlanBlanchet.interact]: Expected 'label' to be a non-empty string.
 *
 *  That is exactly what `mcpServerDefinitionProviders: [{ id: "interact" }]` produced — no `label`,
 *  so the whole entry failed validation, the generated `onMcpCollection:` activation event was
 *  never wired, and MCP collection discovery never registered. The extension appeared to work only
 *  because `activate()` also calls `registerMcpServerDefinitionProvider` imperatively, which masks
 *  the dead contribution.
 *
 *  A malformed manifest cannot fail a compile or a runtime test — nothing imports it — so it needs
 *  a test that reads the real file and applies VS Code's own rules, or the next contribution added
 *  by hand regresses the same way with no signal.
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const manifest = JSON.parse(
  readFileSync(join(import.meta.dirname, "..", "package.json"), "utf8"),
) as { contributes?: Record<string, unknown> };

/** VS Code's rule for these fields: present, a string, and not blank after trimming. */
const nonEmpty = (v: unknown): boolean => typeof v === "string" && v.trim().length > 0;

test("every mcpServerDefinitionProviders entry passes VS Code's validator", () => {
  const entries = manifest.contributes?.mcpServerDefinitionProviders;
  assert.ok(Array.isArray(entries), "contributes.mcpServerDefinitionProviders must be an array");
  assert.ok(entries.length > 0, "declaring an empty array contributes nothing");

  for (const entry of entries as Array<Record<string, unknown>>) {
    assert.ok(nonEmpty(entry.id), `entry ${JSON.stringify(entry)} needs a non-empty 'id'`);
    // The field whose absence discarded the contribution.
    assert.ok(nonEmpty(entry.label), `entry ${JSON.stringify(entry)} needs a non-empty 'label'`);
    // `when` is optional, but a blank one is rejected exactly like a blank label.
    if (entry.when !== undefined) {
      assert.ok(nonEmpty(entry.when), `entry ${JSON.stringify(entry)} has a blank 'when'`);
    }
  }
});
