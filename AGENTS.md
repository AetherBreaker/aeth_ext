# aeth_ext

Python 3.14 package (`uv`-managed) providing shared infrastructure: a structured logging system with a
central log server (Textual TUI + web viewer), FTP/SFTP helpers, pydantic-based settings/types, and error
alerting.

## Commands

- Install deps: `uv sync`
- Run tests: `uv run pytest` (coverage, `--strict-markers`, `--strict-config` are on by default via `addopts`)
- Lint: `uv run ruff check .`
- Type check: `uv run pyright`
- Release (bump, commit, tag, build, publish to GitHub + SFTPyPI): `uv run poe release <bump-type>`
- Textual dev console / dev-mode app: see tasks in `.vscode/tasks.json` (`Textual Console`, `Launch Textual in Dev Mode WT/Browser`)

## Pydantic Dataclass Conventions

**All pydantic dataclasses in this project must inherit from `aeth_ext.types.IsPydantic`.**

- `pyproject.toml` configures `[tool.ruff.lint.flake8-type-checking] runtime-evaluated-base-classes`
  to include `aeth_ext.types.IsPydantic` (among others like `pydantic.main.BaseModel`).
- `IsPydantic` is an empty marker class with `__slots__ = ()` that signals to Ruff that field-type
  imports are evaluated at runtime (by pydantic's validator building) and must **NOT** be moved into a
  `TYPE_CHECKING` block.
- Moving type imports to `TYPE_CHECKING` causes a runtime error:
  ```
  PydanticUserError: '<Cls>' is not fully defined; you should define '<type>'...
  ```
  because pydantic needs the annotation resolved at validator build time, unlike plain
  dataclasses/TypedDicts.
- **Subclasses of an `IsPydantic`-inheriting base** (e.g. `MiscDef` in `protocol.py`) do **not** need to
  repeat the inheritance — Ruff and pydantic resolve it transitively through the MRO.

```python
from pydantic.dataclasses import dataclass
from aeth_ext.types import IsPydantic

@dataclass(config=...)
class MyDataClass(IsPydantic):
    field: SomeType  # Keep SomeType import outside TYPE_CHECKING
```

## Annotation Conventions

**Do NOT use `from __future__ import annotations` anywhere in this project.**

- Explicit project rule to ensure annotations are evaluated eagerly at class definition time.
- Python 3.14's lazy `__annotate__` still resolves on first access (e.g. `__annotations__`, or during
  pydantic's validator build).
- **Consequence:** any type used in a real (non-dataclass) annotation must be an actual runtime import,
  not just `TYPE_CHECKING`-guarded, if something will eventually force evaluation (pydantic validators,
  `dataclasses.fields` introspection, etc.).
- **Exception:** plain unused-at-runtime annotations (e.g. function param types checked only by Pyright)
  are fine to keep under `TYPE_CHECKING`.

## Exception Handling (PEP 758, Python 3.14+)

`except` clauses can list multiple exception types without parentheses **unless** capturing with `as e`,
in which case parentheses are required:

```python
except A, B, C:          # valid — no parentheses needed
except (A, B, C) as e:   # valid — parentheses required with `as e`
except A, B, C as e:     # INVALID syntax
```

Do not flag bare `except A, B, C:` (no `as e`) as Python 2 syntax or an error — this project targets
Python 3.14 and PEP 758 is in effect.

## `__pycache__` Prevention

Always ensure `PYTHONPYCACHEPREFIX` is set. Under pytest it's auto-loaded from `.env`. When running
scripts directly, export it explicitly first.

## Commit Message Conventions

Follow Conventional Commits: `<type>(<scope>): <short summary>`.

- Types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `chore`.
- Scope: the affected module/package (e.g. `types`, `protocol`); omit only for truly project-wide changes.
- For `fix` commits, the body must describe: (1) what the bug was, (2) what caused it, (3) how this
  commit fixes it.

## Tests Do Not Define Intent

**The test suite is your domain, not mine.** I don't review tests as a statement of what I want, so
an existing assertion is evidence only that some earlier session wrote it — never evidence of my
intent.

- **Never cite a test as justification** for how production code should behave, and never treat a
  failing test as proof the implementation is wrong. Establish the intended behavior from my
  instructions, the design/plan docs (`.claude/plans/`, `PLAN-*.md`), or by asking — then fix
  whichever side is actually wrong.
- **Never preserve a behavior solely because a test covers it.** If a change makes an assertion
  obsolete, rewrite or delete the test; don't contort the implementation to keep it green.
- You have standing authority to add, rewrite, restructure, or delete tests without asking. Test
  churn is not a cost worth trading production-code quality for.

## Testing Workflow

Don't run the full test suite eagerly while iterating on a feature branch — it wastes time, especially
since in-progress changes often require test rewrites later in the branch's lifetime anyway.

- On `main`/`master`: run the full suite (`uv run pytest`) normally.
- On any other branch: only run tests for (1) specific/targeted individual tests relevant to what you're
  debugging, or (2) once at the end of a task, immediately before stopping.
- The full suite is reviewed and run by hand before a branch is merged via PR — reserve thorough,
  whole-suite runs for that point in the branch's lifetime, not every intermediate step.

## Docstring and Comment Conventions

**Docstrings use Google style, not Sphinx/RST.** No `:param:`/`:returns:`/`:raises:` fields and no
`:func:`/`:class:`/`:meth:`/`:attr:`/`:data:`/`:mod:` cross-reference roles. Use `Args:`, `Returns:`,
`Raises:` blocks for parameters, and plain double-backtick names (` ``thing`` `) instead of role
cross-references — this project does not build Sphinx docs, so RST roles buy nothing and only add
punctuation noise. `aeth_ext/errors/shutdown.py` is the reference example for this style; new/edited
docstrings elsewhere should be brought in line with it opportunistically rather than in a dedicated
sweep.

**Comments and docstrings should carry reasoning, but stay dense.** The *why* behind a non-obvious
decision (a lock ordering, a deadlock avoided, a rejected alternative) is worth keeping — it protects
against a future edit silently reintroducing the bug it prevents. But default to the fewest words that
still convey the full reasoning:

- Don't restate the same point from two angles in the same docstring — say it once, precisely.
- Don't re-derive a fact already established elsewhere in the file (e.g. re-explaining copy-on-write
  semantics at every call site when the defining line already documents it) — reference it briefly or
  omit it.
- Prefer compact phrasing over hedged, multi-clause sentences: cut scaffolding like "And consistency:",
  "It is worth being exact about", "the reason that carries the choice on its own" — state the reason
  directly instead of announcing that a reason is coming.
- This trades against LLM context cost directly: a file a coding agent must read in full pays for every
  restated sentence on every session, and stale prose that drifts from the code it describes actively
  misleads future edits. When in doubt, cut elaboration before cutting the one sentence that states the
  actual constraint.

## Secrets

`.env` contains live credentials (SMTP password, SFTPyPI credentials) — never print its contents back in
full, commit it, or suggest committing it.

<!-- devkit:begin -->
## Environment

This project is managed by `uv`. Run Python, and anything that depends on it (pytest, ruff,
pyright, poe), under `uv run` so it resolves inside the project environment.

## Exception Handling (PEP 758, Python 3.14+)

`except` clauses can list multiple exception types without parentheses **unless** capturing with `as e`,
in which case parentheses are required:

```python
except A, B, C:          # valid — no parentheses needed
except (A, B, C) as e:   # valid — parentheses required with `as e`
except A, B, C as e:     # INVALID syntax
```

Do not flag bare `except A, B, C:` (no `as e`) as Python 2 syntax or an error — this project targets
Python 3.14 and PEP 758 is in effect.

## Tests Do Not Define Intent

**The test suite is your domain, not mine.** I don't review tests as a statement of what I want, so
an existing assertion is evidence only that some earlier session wrote it — never evidence of my
intent.

- **Never cite a test as justification** for how production code should behave, and never treat a
  failing test as proof the implementation is wrong. Establish the intended behavior from my
  instructions, the current code, or by asking — then fix whichever side is actually wrong.
- **Never preserve a behavior solely because a test covers it.** If a change makes an assertion
  obsolete, rewrite or delete the test; don't contort the implementation to keep it green.
- You have standing authority to add, rewrite, restructure, or delete tests without asking. Test
  churn is not a cost worth trading production-code quality for.

## Plan Docs Do Not Define Intent Either

**Never treat `.claude/plans/*.md`/`PLAN-*.md` as representative of my current intent.** Only my live
instructions and the current code are authoritative.

- Plan docs go stale fast: a decision made about an issue raised *after* the plan was written changes
  what was originally specified, and the doc is essentially never updated to match.
- Worse: a plan doc frequently contains decisions that were never actually made by me and never
  approved by me at all — written unilaterally by a prior agent session while planning, then left
  unreviewed. A plan doc's existence is not evidence I signed off on anything in it.
- If a plan doc's stated intent conflicts with the current code or with what I'm currently asking
  for, trust the code/my live word — don't silently follow the doc, and don't cite it back to me as
  justification for a design choice. When genuinely unsure which reflects current intent, ask me
  directly rather than defaulting to the doc.

## Testing Workflow

Don't run the full test suite eagerly while iterating on a feature branch — it wastes time, especially
since in-progress changes often require test rewrites later in the branch's lifetime anyway.

- On `main`/`master`: run the full suite (`uv run pytest`) normally.
- On any other branch: only run tests for (1) specific/targeted individual tests relevant to what you're
  debugging, or (2) once at the end of a task, immediately before stopping.
- The full suite is reviewed and run by hand before a branch is merged via PR — reserve thorough,
  whole-suite runs for that point in the branch's lifetime, not every intermediate step.

## Comment Density

**Comments and docstrings should carry reasoning, but stay dense.** The *why* behind a non-obvious
decision (a lock ordering, a deadlock avoided, a rejected alternative) is worth keeping — it protects
against a future edit silently reintroducing the bug it prevents. But default to the fewest words that
still convey the full reasoning:

- Don't restate the same point from two angles in the same docstring — say it once, precisely.
- Don't re-derive a fact already established elsewhere in the file (e.g. re-explaining copy-on-write
  semantics at every call site when the defining line already documents it) — reference it briefly or
  omit it.
- Prefer compact phrasing over hedged, multi-clause sentences: cut scaffolding like "And consistency:",
  "It is worth being exact about", "the reason that carries the choice on its own" — state the reason
  directly instead of announcing that a reason is coming.
- This trades against LLM context cost directly: a file a coding agent must read in full pays for every
  restated sentence on every session, and stale prose that drifts from the code it describes actively
  misleads future edits. When in doubt, cut elaboration before cutting the one sentence that states the
  actual constraint.

## Abstraction Conventions

**Avoid extracting small or single-use helper functions/methods.** Only extract when:

- A lint rule forces it after an initial write pass (too-many-statements, too-many-branches, too-long,
  etc.) — a mechanical response to a real constraint, not a judgment call, or
- The code has a genuine, significant responsibility that sees reuse across multiple call sites. The
  larger the candidate body (lines/statements/branches), the fewer reuse sites are needed to justify
  extraction; the smaller the body, the more sites are needed.

Be especially hesitant to extract a function whose body is 4 lines of code or fewer — a short helper's
name and parameter list almost always carry less information than its own implementation, so wrapping
it tends to obscure rather than clarify. Before writing one, stop and ask whether inlining the body at
the call site would actually be easier to read. This applies to planning and design docs too: don't
pre-split multi-step logic into named helper methods "for clarity" before a lint rule or real reuse
demands it.

## Commit Message Conventions

Follow Conventional Commits: `<type>(<scope>): <short summary>`, with types `feat`, `fix`, `docs`,
`style`, `refactor`, `perf`, `test`, `chore` and the affected module as scope (omit only for truly
project-wide changes). A `fix` body must describe what the bug was, what caused it, and how this commit
fixes it.

## Secrets

`.env` contains live credentials — never print its contents back in full, commit it, or suggest
committing it.

## Pydantic Dataclass Conventions

**All pydantic dataclasses in this project must inherit from `aeth_ext.types.IsPydantic`.**

- `pyproject.toml` configures `[tool.ruff.lint.flake8-type-checking] runtime-evaluated-base-classes`
  to include `aeth_ext.types.IsPydantic` (among others like `pydantic.main.BaseModel`).
- `IsPydantic` is an empty marker class with `__slots__ = ()` that signals to Ruff that field-type
  imports are evaluated at runtime (by pydantic's validator building) and must **NOT** be moved into a
  `TYPE_CHECKING` block.
- Moving type imports to `TYPE_CHECKING` causes a runtime error:
  ```
  PydanticUserError: '<Cls>' is not fully defined; you should define '<type>'...
  ```
  because pydantic needs the annotation resolved at validator build time, unlike plain
  dataclasses/TypedDicts.
- **Subclasses of an `IsPydantic`-inheriting base** do **not** need to repeat the inheritance — Ruff
  and pydantic resolve it transitively through the MRO.

```python
from pydantic.dataclasses import dataclass
from aeth_ext.types import IsPydantic

@dataclass(config=...)
class MyDataClass(IsPydantic):
    field: SomeType  # Keep SomeType import outside TYPE_CHECKING
```
<!-- devkit:end -->
