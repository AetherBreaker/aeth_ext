"""Fixture package with no `get_logging_configs`, only ALL-CAPS constants for the fallback path.

`parse_and_grab_constants` only ever reads `__main__.py`/`__init__.py` at each
ancestry level, so these constants must live here rather than in an arbitrary
submodule for the constant search to find them.
"""

PROJECT_NAME = "constant-only-program"
TESTING = True
