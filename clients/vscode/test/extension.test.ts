/** Acceptance checks that need a REAL VS Code — the ones a unit test structurally cannot make.
 *
 *  Every failure here is one the user would otherwise have to report: a panel with no way in, a
 *  command that isn't registered, a view whose provider never attached. Two of those actually
 *  shipped before this file existed.
 */
import * as assert from "assert";
import * as vscode from "vscode";

const EXT = "AlanBlanchet.interact";

suite("interact extension", () => {
  test("the extension activates without throwing", async () => {
    const ext = vscode.extensions.getExtension(EXT);
    assert.ok(ext, `${EXT} is not installed in this test host`);
    await ext!.activate();
    assert.strictEqual(ext!.isActive, true);
  });

  test("it contributes a view container the user can actually click", () => {
    // The defect this pins: the agent work shipped with NO viewsContainers at all, so there was
    // nothing in the activity bar and the feature was unreachable however well it worked.
    const pkg = vscode.extensions.getExtension(EXT)!.packageJSON;
    const containers = pkg.contributes?.viewsContainers?.activitybar ?? [];
    assert.ok(containers.length > 0, "no activity-bar container — the panel has no entry point");
    assert.ok(containers.some((c: { id: string }) => c.id === "interact"));
  });

  test("the Agents view is declared inside that container", () => {
    const pkg = vscode.extensions.getExtension(EXT)!.packageJSON;
    const views = pkg.contributes?.views?.interact ?? [];
    assert.ok(
      views.some((v: { id: string }) => v.id === "interact.agentsView"),
      "the Agents view is not in the interact container",
    );
  });

  test("every command the UI references is registered", async () => {
    // A menu entry pointing at an unregistered command fails silently at click time — exactly the
    // class of defect the user ends up reporting instead of a test.
    const registered = new Set(await vscode.commands.getCommands(true));
    for (const id of [
      "interact.openDashboard",
      "interact.agents.refresh",
      "interact.agents.groupBy",
      "interact.agents.stop",
      "interact.agents.showEvents",
      "interact.agents.openConversation",
    ]) {
      assert.ok(registered.has(id), `command not registered: ${id}`);
    }
  });

  test("focusing the Agents view works — the path a user takes to reach it", async () => {
    // VS Code auto-generates `<viewId>.focus`; if the view were mis-declared this rejects.
    await vscode.commands.executeCommand("interact.agentsView.focus");
  });

  test("opening a conversation does not throw, even for an unknown run", async () => {
    // The panel must degrade rather than explode: a stale id from a pruned registry is normal.
    await vscode.commands.executeCommand("interact.agents.openConversation", "no-such-run-id");
  });
});
