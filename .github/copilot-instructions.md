# aeth_ext Project Conventions

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

## Commit Message Conventions

When generating Git commit messages, always follow the Conventional Commits specification.
Use format: <type>(<scope>): <short summary>
Types must be: feat, fix, docs, style, refactor, perf, test, or chore.
Use the affected module or package name as the scope (e.g., `types`, `protocol`). Omit the scope only when the change is truly project-wide.
When the type is "fix", the body must describe: (1) what the bug was, (2) what caused it, and (3) how this commit fixes it.

## Pydantic Dataclass Conventions

**All pydantic dataclasses in this project must inherit from `aeth_ext.types.IsPydantic`.**

- `pyproject.toml` configures `[tool.ruff.lint.flake8-type-checking] runtime-evaluated-base-classes`
  to include `aeth_ext.types.IsPydantic` (among others like `pydantic.main.BaseModel`).
- `IsPydantic` is an empty marker class with `__slots__ = ()` that signals to Ruff that
  field-type imports are evaluated at runtime (by pydantic's validator building) and must
  **NOT be moved into a `TYPE_CHECKING` block**.
- Moving type imports to `TYPE_CHECKING` causes a runtime error:
    ```
    PydanticUserError: '<Cls>' is not fully defined; you should define '<type>'...
    ```
    This happens because pydantic actually needs the annotation resolved at validator build time,
    unlike plain dataclasses/TypedDicts.
- **Subclasses of an `IsPydantic`-inheriting base** (e.g., `MiscDef` in `protocol.py`) do **not**
  need to repeat the inheritance — Ruff and pydantic resolve it transitively through the MRO.

**Example:**

```python
from pydantic.dataclasses import dataclass
from aeth_ext.types import IsPydantic

@dataclass(config=...)
class MyDataClass(IsPydantic):
    field: SomeType  # Keep SomeType import outside TYPE_CHECKING
```

## Annotation Conventions

**Do NOT use `from __future__ import annotations` anywhere in this project.**

- This is an explicit project rule to ensure annotations are evaluated eagerly at class definition time.
- Python 3.14's lazy `__annotate__` still resolves on first access (like `__annotations__` or during
  pydantic's validator build).
- **Consequence:** Any type used in a real (non-dataclass) annotation MUST be an actual runtime import,
  not just a `TYPE_CHECKING`-guarded one, if something will eventually force evaluation
  (pydantic validators, `dataclasses.fields` introspection, etc.).
- **Exception:** Plain unused-at-runtime annotations (e.g., function param types checked only by Pyright)
  are fine to keep under `TYPE_CHECKING`.

## Exception Handling (PEP 758, Python 3.14+)

`except` clauses can list multiple exception types without parentheses **unless** capturing
with `as e`, in which case parentheses are required. Valid forms in Python 3.14+:

```python
except A, B, C:          # valid — no parentheses needed
except (A, B, C) as e:   # valid — parentheses required with `as e`
except A, B, C as e:     # INVALID syntax
```

Do **not** flag bare `except A, B, C:` (no `as e`) as Python 2 syntax or as an error —
this project targets Python 3.14 and PEP 758 is in effect.

## Prevent **pycache** Pollution

Always set `PYTHONPYCACHEPREFIX`. When running via pytest, rely on the value defined in `.env` (auto-loaded by pytest). When running scripts directly, export the variable explicitly before invocation.

## Tests Do Not Define Intent

**The test suite is the project's domain, not the maintainer's stated intent.** An existing assertion is
evidence only that some earlier session wrote it — never evidence of what the maintainer actually wants.

- **Never cite a test as justification** for how production code should behave, and never treat a
  failing test as proof the implementation is wrong. Establish the intended behavior from the
  maintainer's instructions, the design/plan docs (`.claude/plans/`, `PLAN-*.md`), or by asking — then
  fix whichever side is actually wrong.
- **Never preserve a behavior solely because a test covers it.** If a change makes an assertion
  obsolete, rewrite or delete the test; don't contort the implementation to keep it green.
- Standing authority exists to add, rewrite, restructure, or delete tests without asking. Test churn is
  not a cost worth trading production-code quality for.

## Testing Workflow

Don't run the full test suite eagerly while iterating on a feature branch — it wastes time, especially
since in-progress changes often require test rewrites later in the branch's lifetime anyway.

- On `main`/`master`: run the full suite (`uv run pytest`) normally.
- On any other branch: only run tests for (1) specific/targeted individual tests relevant to what's being
  debugged, or (2) once at the end of a task, immediately before stopping.
- The full suite is reviewed and run by hand before a branch is merged via PR — reserve thorough,
  whole-suite runs for that point in the branch's lifetime, not every intermediate step.

## Code Review Scope

When performing code review on a pull request, do not review changes under `tests/` or files matching
`test_*.py` / `*_test.py`. Focus review on production source code only.

## Secrets

`.env` contains live credentials (SMTP password, SFTPyPI credentials) — never print its contents back in
full, commit it, or suggest committing it.

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
