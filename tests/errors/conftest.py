"""Shared pytest fixtures for the `errors` test suite.

The cross-cutting `_clear_fatal_event` autouse isolation fixture lives in the
root `tests/conftest.py` and applies here automatically. Every scenario that
actually sets `FATAL_EVENT` is exercised in an isolated subprocess (see
`test_err_handling.py`'s `-O` helper), so that guard should never legitimately
fire for tests in this package.
"""
