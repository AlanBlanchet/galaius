/** What each model is measured at — asked once, for every surface that shows a number.
 *
 *  Four surfaces (dashboard board, Models table, rail's roster, pickers) each held their own
 *  copy of this: same "agents models --json-out" call, same 60-name slice, same get(m) ??
 *  get(bareModelName(m)) written ten times, same silent "if (error) return". They drifted the
 *  way four copies do — one cached a failure forever, none fell back to the board on disk.
 *
 *  Two facts this holds that a plain map can't:
 *
 *  - "could not ASK" is not "not RANKED". They read identically before, so a machine whose CLI
 *    couldn't answer showed "no ranking carries this model" for models the board ranks — beside
 *    a Benchmarks tab drawing 633 rows from a file this same process can read.
 *  - a failed ask must be RETRIED. The cache key was set before awaiting, so one failure at
 *    startup meant no surface ever asked again for the life of the window.
 */
import { bareModelName, competenceIndex, competenceOf, resolvedIndex, scoreIndex,
  type ScoredRow } from "./competence.ts";
import { boardMatch, boardScores } from "./boardScores.ts";
import { interactCli } from "./interactCli.ts";

/** How many names one ask carries. The CLI resolves each by name and a very long argv is its own
 *  failure mode; all four surfaces chose 60 independently, so it is stated once here. */
const ASK_LIMIT = 60;

export class CompetenceStore {
  /** Its two sources, both injectable. Defaulted to the real ones; a test reaching the real CLI
   *  and the reader's own board would pass or fail on the machine it ran on — the behaviours
   *  worth pinning here are precisely what happens when one of the two is missing. */
  private readonly ask: typeof interactCli;
  private readonly board: typeof boardScores;

  constructor(
    ask: typeof interactCli = interactCli,
    board: typeof boardScores = boardScores,
  ) {
    this.ask = ask;
    this.board = board;
  }

  private line = new Map<string, string>();
  private score = new Map<string, number>();
  private resolved = new Map<string, string>();
  /** The keys the CLI itself answered for. The board fallback adds SCORES for models nobody here
   *  can reach, so "is it measured" and "can I run it" stopped being the same question the moment
   *  that fallback existed — and one surface marks rows "routable" on exactly this distinction. */
  private viaCli = new Set<string>();
  private asked: string | undefined;
  /** What the last answer LOOKED like, so "changed" is a comparison and not a guess. */
  private shown = "";
  /** Why no number could be fetched, in the reader's terms, or null when all is well. */
  unavailable: string | null = null;

  /** Ask about these models, unless the same set was already answered. Resolves to whether
   *  anything is known now, so a caller can repaint only when it would change something. */
  async ensure(models: readonly string[]): Promise<boolean> {
    const names = [...new Set(models.filter(Boolean))].sort();
    const key = names.join(" ");
    // FALSE, not "do we know anything". The one caller repaints when true, and a repaint asks
    // again — so returning "known" on a cache hit closed a loop: roster() → ensureCompetence() →
    // ensure() → true → refreshIfOpen() → render() → roster(). Measured at 12.5 wholesale roster
    // swaps/sec, ~1.0 MB/s, extension host pegged at 104% even with the tab CLOSED. The question
    // here is "is there something NEW to show" — a cache hit is precisely where there provably
    // is not.
    if (this.asked === key && this.unavailable === null) return false;
    this.asked = key;
    let rows: ScoredRow[] = [];
    let failure: string | null = null;
    try {
      const { stdout, error } = await this.ask([
        "agents", "models", "--json-out", "--limit", "0",
        ...names.slice(0, ASK_LIMIT).flatMap((m) => ["--names", m]),
      ]);
      if (error) failure = whyUnavailable(error);
      else rows = JSON.parse(stdout) as ScoredRow[];
    } catch (err) {
      failure = whyUnavailable(err instanceof Error ? err.message : String(err));
    }
    // A failure must not be remembered as an answer: leave the key unset so the next surface to
    // ask tries again rather than inheriting a permanent silence.
    if (failure) this.asked = undefined;

    this.line = competenceIndex(rows);
    this.score = scoreIndex(rows);
    this.resolved = resolvedIndex(rows);
    this.viaCli = new Set(this.line.keys());
    this.fillFromBoard(names);
    this.unavailable = this.line.size > 0 ? null : failure;
    // "Did anything CHANGE", not "do we know anything". The cache-hit guard above wasn't enough:
    // a FAILING CLI clears asked on purpose so the next surface retries, making every render a
    // cache miss that falls through to here — and fillFromBoard() has meanwhile filled line from
    // the board on disk, so "do we know anything" is permanently true and the repaint loop
    // re-arms. Measured with the CLI off PATH: 98.1% of a core with the tab CLOSED.
    const fingerprint = `${[...this.line].sort().join("|")}~${this.unavailable ?? ""}`;
    const changed = fingerprint !== this.shown;
    this.shown = fingerprint;
    return changed;
  }

  /** Whatever the CLI did not answer for, the board on disk may still measure — and an INEXACT
   *  match keeps its mark, because asserting a measured score for a model nobody measured is
   *  worse than saying nothing. */
  private fillFromBoard(names: readonly string[]): void {
    const board = this.board();
    if (!board.size) return;
    const population = [...board.values()].map((v) => ({ intelligence_score: v }));
    for (const name of names) {
      const hit = boardMatch(name, board, bareModelName);
      if (!hit) continue;
      const bare = bareModelName(name);
      if (!this.resolved.has(name) && !this.resolved.has(bare)) this.resolved.set(bare, hit.key);
      if (this.score.has(name) || this.score.has(bare)) continue;
      this.score.set(bare, hit.score);
      const sentence = competenceOf({ intelligence_score: hit.score }, population);
      this.line.set(bare, hit.approximate ? `closest ranked ${hit.key} · ${sentence}` : sentence);
    }
  }

  /** Normalised INSIDE, so that no caller writes the two-step lookup for an eleventh time. */
  private read<T>(index: Map<string, T>, model: string | undefined): T | undefined {
    if (!model) return undefined;
    return index.get(model) ?? index.get(bareModelName(model));
  }

  competence(model: string | undefined): string | undefined { return this.read(this.line, model); }
  /** Whether a configured provider can actually be pointed at this model — the CLI's answer only,
   *  never the board's, which measures plenty of models nobody here can run. */
  routable(model: string | undefined): boolean {
    if (!model) return false;
    return this.viaCli.has(model) || this.viaCli.has(bareModelName(model));
  }
  scoreOf(model: string | undefined): number | undefined { return this.read(this.score, model); }
  resolvedId(model: string | undefined): string | undefined {
    return this.read(this.resolved, model);
  }
  get size(): number { return this.line.size; }
}

/** Why the ranking could not be reached, in the reader's terms. An installed CLI too old to answer
 *  is the common case and worth naming, because the fix is a version, not a missing model. */
export function whyUnavailable(error: string): string {
  return /unknown command|no such command/i.test(error)
    ? "your installed interact cannot report scores — it needs a newer version"
    : "interact could not be asked for scores";
}
