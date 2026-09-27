/** How much autonomy a spawned agent gets, as a choice the panel can offer.
 *
 *  Supervising a team is mostly this decision, per member — the researcher reads, the refactorer
 *  writes, the unwatched one plans and touches nothing. The panel used to spawn agents without
 *  saying which: every run got the vendor's default, unshown.
 *
 *  List is READ from the CLI (interact agents modes), never hardcoded — two copies of a vendor's
 *  flag values drift, and the copy further from the binary is the one that drifts. Tab-separated,
 *  not prose, so the parser survives a reworded description.
 */

export interface PermissionMode {
  id: string;
  label: string;
  detail: string;
  /** Acts without asking. Offered, but never rendered as an unremarkable option. */
  unrestricted: boolean;
}

/** Parse the CLI's mode list. Malformed rows are dropped, never shown half-read: a row that
 *  won't parse is likelier an error message than a mode, and offering "ERROR: no such provider"
 *  as an autonomy setting is worse than one option fewer.
 */
export function parseModes(stdout: string): PermissionMode[] {
  return stdout
    .split("\n")
    .map((line) => line.split("\t"))
    .filter((f) => f.length === 4 && f[0].trim() !== "")
    .map(([id, label, detail, unrestricted]) => ({
      id: id.trim(),
      label: label.trim(),
      detail: detail.trim(),
      unrestricted: unrestricted.trim() === "yes",
    }));
}

export interface ModeChoice {
  label: string;
  description: string;
  detail: string;
  id: string | null;
}

/** Quick-pick items, with the CLI's own default kept as a real choice.
 *
 *  "Whatever your CLI is set to" leads: it's what happens today and on dismiss — a list whose
 *  first entry silently CHANGES behaviour is a trap. The last param floats the previous choice
 *  to the top, so spawning ten agents the same way is nine fewer decisions.
 *
 *  Unrestricted mode is marked in its own label, not by position or colour: a webview quick pick
 *  has no styling to lean on, so it must read as different at a glance.
 */
export function modeChoices(modes: readonly PermissionMode[], last?: string | null): ModeChoice[] {
  const inherit: ModeChoice = {
    label: "Your CLI's default",
    description: "",
    detail: "whatever the agent CLI is already configured to do",
    id: null,
  };
  const items = modes.map((m) => ({
    label: m.unrestricted ? `$(warning) ${m.label}` : m.label,
    description: m.id,
    detail: m.unrestricted ? `${m.detail} — no confirmation, ever` : m.detail,
    id: m.id,
  }));
  const all = [inherit, ...items];
  if (!last) return all;
  const previous = all.filter((c) => c.id === last);
  return previous.length ? [...previous, ...all.filter((c) => c.id !== last)] : all;
}

/** What a run's recorded mode says in the panel, or null when nobody chose one.
 *
 *  Answers "why did that stop to ask" / "why did that just do it" for an agent you're watching.
 *  Unknown id shown verbatim, never dropped: the run really ran with it, and hiding it would
 *  misreport the autonomy it's running under — the one thing this field must be honest about.
 */
export function describeMode(
  id: string | null | undefined,
  modes: readonly PermissionMode[],
): { label: string; unrestricted: boolean } | null {
  if (!id) return null;
  const known = modes.find((m) => m.id === id);
  if (known) return { label: known.label, unrestricted: known.unrestricted };
  // Unknown id shown verbatim (see doc above). Marked UNRESTRICTED regardless: this axis fails
  // CLOSED, so a newer CLI's more permissive setting never renders as unremarkable just because
  // this build hasn't heard of it.
  return { label: id, unrestricted: true };
}

/** The modes this machine's CLI offers, asked once.
 *
 *  Three call sites used to shell out separately — spawn picker, workspace default, chat panel
 *  id-to-words — each with its own error handling, only one with a timeout. Answer changes only
 *  when the CLI is upgraded, so asking per repaint was pure cost.
 *
 *  FAILURE is never cached: an empty list means "CLI exposes no verified modes", a real answer
 *  that hides the control. Caching a transient failure as that would hide it until window close.
 */
const cached = new Map<string, PermissionMode[]>();

export async function knownModes(provider = "claude"): Promise<PermissionMode[]> {
  const previous = cached.get(provider);
  if (previous) return previous;
  // Lazy import so this module stays loadable by node --test --experimental-strip-types, which
  // resolves real specifiers and would demand a ".ts" suffix tsc then refuses to emit. Pure
  // functions above are what tests exercise; this path never runs there.
  const { interactCli } = await import("./interactCli");
  const { stdout, error } = await interactCli(["agents", "modes", "--provider", provider]);
  if (error) return [];
  const modes = parseModes(stdout);
  cached.set(provider, modes);
  return modes;
}

/** Forget one provider's answer, or all answers after the CLI is upgraded under a running window. */
export function forgetModes(provider?: string): void {
  if (provider) cached.delete(provider);
  else cached.clear();
}
