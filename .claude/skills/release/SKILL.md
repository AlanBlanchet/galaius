---
name: release
description: Bump the interact version safely (semver, pyproject.toml + extension in sync) so CI tags the release on the remote. Use whenever shipping a feature/fix or asked to cut/bump a version.
---

# Releasing interact

`pyproject.toml` `[project].version` is the **single source of truth**; the VS Code
extension `vscode-extension/package.json` `version` must match. You never create or push
git tags by hand — pushing `main` triggers `.github/workflows/release.yml`, which verifies
the two versions agree and then creates + pushes `vX.Y.Z` and a GitHub release.

## Batched — bump only when the user says to release

Do **not** bump on every change. While working, just *assess* each change's impact and note
it. Bump **once** when the user asks to cut/push a release, choosing the highest accumulated
impact since the last release:

- **patch** (`0.0.x`) — bug fix, no API change.
- **minor** (`0.x.0`) — backward-compatible feature (new tool, CLI command, config field).
- **major** (`x.0.0`) — breaking change (removed/renamed tool, changed defaults, moved config).

(Pre-1.0 reality: a "breaking" change may bump minor rather than major — confirm with the user.)

## Steps

1. Bump **both files at once** (don't hand-edit one):

       uv run python -m interact.versioning bump <major|minor|patch>

   It rewrites `pyproject.toml` and `vscode-extension/package.json` and prints the new
   version.

2. Verify they agree (the pre-commit hook runs this too, and CI refuses to tag on a
   mismatch):

       uv run python -m interact.versioning check

3. Commit the version bump **with** the change it ships (one commit per user message).
   The tracked `.githooks/pre-commit` re-checks the sync.

4. When the user asks to push: `git push origin main`. CI tags `vX.Y.Z` and publishes the
   release. Do **not** `git tag`/`git push --tags` yourself.

5. After a release, `interact update` (and the TUI banner) will offer it to installed users
   from GitHub.

## Guardrails

- Bumping only one of the two files → pre-commit fails and CI refuses to tag. Always use
  `versioning bump`.
- Editable installs cache the version; `uv tool install --force --editable .` refreshes
  `interact --version` locally after a bump.
- Tag already exists for the current version → CI no-ops (safe to re-push).
