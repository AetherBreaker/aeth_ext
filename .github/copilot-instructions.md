# Python Conventions

## PEP 758 (Python 3.14+)

`except` clauses can list multiple exception types without parentheses **unless** capturing
with `as e`, in which case parentheses are required. Valid forms in Python 3.14+:

```python
except A, B, C:          # valid — no parentheses needed
except (A, B, C) as e:   # valid — parentheses required with `as e`
except A, B, C as e:     # INVALID syntax
```

Do **not** flag bare `except A, B, C:` (no `as e`) as Python 2 syntax or as an error —
this project targets Python 3.14 and PEP 758 is in effect.
