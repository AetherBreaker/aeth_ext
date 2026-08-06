"""Shared pytest fixtures for the `errors` test suite.

The cross-cutting `_clear_shutdown_state` autouse isolation fixture lives in
the root `tests/conftest.py` and applies here automatically. Every scenario
that actually requests a shutdown is exercised in an isolated subprocess (see
`test_err_handling.py`'s `-O` helper), so that guard should never legitimately
fire for tests in this package.
"""
