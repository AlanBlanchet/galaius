# Releasing interact

Releases are **mechanical and CI-driven**. You bump one number and push; CI tags, builds, and
publishes. This doc is the source of truth for how versions, branches, tags, and publishing work.

## Version — single source of truth

`pyproject.toml` `[project].version` **is** the version. `clients/vscode/package.json` `version`
must match it (the pre-commit hook and CI both fail on drift). Change them only via:

```bash
uv run python -m interact.versioning bump <patch|minor|major>   # writes both files
uv run python -m interact.versioning check                       # verify they agree
uv run python -m interact.versioning current                     # print the version
```

- **patch** — a bug fix, no behaviour change (`0.2.0 → 0.2.1`).
- **minor** — a backward-compatible feature (`0.2.1 → 0.3.0`).
- **major** — a breaking change (`0.3.0 → 1.0.0`).

The package is named **`interact`** throughout — import package, CLI command, and distribution.
`interact.DIST_NAME` is the single source for that name in code (a test asserts it matches
`pyproject.toml`). interact is **not on PyPI** (the bare `interact` name is taken there by an
unrelated package); it's distributed from GitHub via the installer + `uv`, and the extension via
the Marketplace / Open VSX.

## The flow

1. Land your changes on `main` (or a `release/X.Y` branch — see below).
2. Bump: `uv run python -m interact.versioning bump <part>`.
3. Note it in `CHANGELOG.md` (move items out of *Unreleased*).
4. Commit and push.
5. CI does the rest: runs the full matrix + desktop tests, and **only if they pass** —
   - creates and pushes the tag `vX.Y.Z` (no-op if it already exists, so re-pushes are safe),
   - cuts a GitHub Release with auto-generated notes,
   - publishes the extension to the Marketplace / Open VSX (see *Publishing targets* for the gates).

Tags are **never** created by hand. You never `git tag`.

## Branches & tags

Trunk-based: `main` is the release line. Tags are `vX.Y.Z`.

Cut a **`release/X.Y` maintenance branch only when you must patch an older line** while `main` has
moved on — e.g. `main` is on `0.4.x` but `0.3.x` users need a fix:

```bash
git switch --detach v0.3.0 && git switch -c release/0.3
git cherry-pick <fix-commit>
uv run python -m interact.versioning bump patch     # 0.3.0 → 0.3.1
git push -u origin release/0.3
```

CI treats `release/**` exactly like `main` (test → tag → publish). Most of the time you never need
one — bump on `main` and push.

## Publishing targets

| Target | What ships | Gate (so the build stays green until set up) |
| --- | --- | --- |
| **GitHub Release** | tag + auto notes; the CLI installs from here (`uv tool install git+…`) | always (only needs the built-in token) |
| **VS Code Marketplace** | the extension | repo **secret** `VSCE_PAT` present |
| **Open VSX** (Cursor / Windsurf / VSCodium) | the extension | repo **secret** `OVSX_PAT` present |

Until each gate is satisfied, that publish step **skips cleanly** — releases keep tagging and
cutting GitHub Releases with no red builds.

### One-time setup (accounts / keys)

- **VS Code Marketplace** — the publisher `AlanBlanchet` must exist (Azure DevOps → Marketplace
  publisher). Create a **Personal Access Token** with the *Marketplace → Manage* scope and add it
  as repo secret `VSCE_PAT`.
- **Open VSX** — create an account at open-vsx.org, generate an access token, add it as repo
  secret `OVSX_PAT`. (First publish under a new namespace may require claiming the `AlanBlanchet`
  namespace once.)
- **Integration tests** (optional, paid) — add `OPENAI_API_KEY` (and any of `GEMINI_API_KEY`,
  `ZAI_API_KEY`) as repo secrets to run the real-model job on a tag / manual dispatch.

## Distribution channels we deliberately skip (for now)

- **PyPI** — the bare `interact` name is taken on PyPI by an unrelated package, and the name stays
  `interact`, so we install from GitHub instead (`uv`/`pipx` handle git sources on every platform).
  `pip install interact` would mean claiming the name via PyPI's PEP 541 process — pursue only if
  you specifically want the PyPI entry.
- **Standalone `.exe`** — interact is a Python CLI + MCP server; `uv` installs it cleanly on
  Windows already, and the desktop-control value-add is Linux/X11-only, so a frozen Windows binary
  adds maintenance (PyInstaller, signing) for little gain. Revisit if there's demand.
- **Ubuntu PPA / `.deb`** — Launchpad + GPG signing + `debian/` packaging is heavy upkeep; `uv`
  covers Linux. A **Homebrew tap** (`brew install AlanBlanchet/tap/interact`) is the lighter native
  option if/when wanted — it also covers macOS.
- **macOS native** — the `curl | sh` installer and `pipx`/`uvx` already work on macOS (browser +
  CLI + MCP). Native *desktop* control there is not implemented yet (errors clearly).
