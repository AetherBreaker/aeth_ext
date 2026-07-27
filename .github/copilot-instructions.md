# aeth_ext Project Conventions

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
