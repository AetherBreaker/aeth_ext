---
name: release-preflight
description: Use before running `poe release`, `poe release-and-pin`, or `poe rescind-release`, or when asked to cut/publish/pin a release — checks for dirty git state, wrong branch, tag collisions, failing tests/lint/types, or missing SFTPyPI credentials that would make the release fail destructively or publish a broken artifact.
---

# Release Pre-flight

## Overview

`poe release`/`poe release-and-pin` bump the version, commit, tag, build, and publish to GitHub
and the private SFTPyPI index in one shot. A bad project state at that moment produces a bad
release that's expensive to unwind (`poe rescind-release` removes the package/tag/GitHub release,
but doesn't un-publish from anyone who already pulled it). Catch the bad state first.

## When to use

- Before running `poe release <bump>` or `poe release-and-pin <bump>` yourself.
- Whenever asked to cut, publish, or pin a release.
- NOT for running the release itself — this only checks preconditions and reports pass/fail.

## Checks (run all, report every failure — don't stop at the first)

| # | Check | Command | Fails if |
|---|-------|---------|----------|
| 1 | Clean working tree | `git status --porcelain` | Any output — uncommitted or untracked files would get swept into the release commit |
| 2 | Correct branch | `git rev-parse --abbrev-ref HEAD` | Not `main` |
| 3 | In sync with remote | `git fetch && git status -sb` | Local is ahead/behind/diverged from `origin/main` |
| 4 | No tag collision | `git tag -l "v<pending-version>"` | Tag for the version about to be created already exists |
| 5 | Tests pass | `uv run pytest` | Non-zero exit |
| 6 | Lint clean | `uv run ruff check .` | Non-zero exit |
| 7 | Types clean | `uv run pyright` | Non-zero exit |
| 8 | SFTPyPI credentials present | check `.env` has both `UV_INDEX_SFTPYPI_USERNAME` and `UV_INDEX_SFTPYPI_PASSWORD` set (existence only — never print the values) | Either missing |
| 9 | Docker pin target exists (only for `release-and-pin`) | `grep -n "GIT_TAG" docker/compose.yaml` | No match — the field `docker-pin-latest` rewrites is `GIT_TAG` under `build.args`, not `PACKAGE_VERSION` despite the task description |

The pending version for check 4 comes from `[project].version` in `pyproject.toml`, bumped by
whatever bump type (`patch`/`minor`/`major`/etc.) is about to be passed to `poe release`.

## Output

Report a pass/fail line per check. If anything fails, say so plainly and do **not** run
`poe release`/`poe release-and-pin` — surface the failures and let the user decide how to fix
them (e.g. commit/stash changes, switch branch, fix failing tests). Never auto-fix a failure by
committing or stashing on the user's behalf.
