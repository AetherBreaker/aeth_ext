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

## Testing Workflow

Don't run the full test suite eagerly while iterating on a feature branch — it wastes time, especially
since in-progress changes often require test rewrites later in the branch's lifetime anyway.

- On `main`/`master`: run the full suite (`uv run pytest`) normally.
- On any other branch: only run tests for (1) specific/targeted individual tests relevant to what you're
  debugging, or (2) once at the end of a task, immediately before stopping.
- The full suite is reviewed and run by hand before a branch is merged via PR — reserve thorough,
  whole-suite runs for that point in the branch's lifetime, not every intermediate step.

## Secrets

`.env` contains live credentials (SMTP password, SFTPyPI credentials) — never print its contents back in
full, commit it, or suggest committing it.
