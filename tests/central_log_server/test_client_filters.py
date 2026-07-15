"""Tests for `aeth_ext.central_log_server.client.filters.RemoteReachability`."""

# Standard library imports
import logging
from typing import Any

# First party imports
from aeth_ext.central_log_server.client.filters import UNREACHABLE, RemoteReachability


def _config(**sections: Any) -> dict[str, Any]:
  return {"version": 1, **sections}


class TestRemoteReachability:
  def test_root_level_and_handler_level_combine(self):
    reachability = RemoteReachability(
      _config(
        handlers={"remote": {"class": "logging.NullHandler", "level": "WARNING"}},
        root={"level": "INFO", "handlers": ["remote"]},
      )
    )

    # INFO records pass the root level but no handler accepts below WARNING.
    assert reachability.threshold_for("any.logger") == logging.WARNING
    assert reachability.threshold_for("root") == logging.WARNING

  def test_handler_level_below_effective_level(self):
    reachability = RemoteReachability(
      _config(
        handlers={"remote": {"class": "logging.NullHandler", "level": "DEBUG"}},
        root={"level": "ERROR", "handlers": ["remote"]},
      )
    )

    assert reachability.threshold_for("any.logger") == logging.ERROR

  def test_no_handlers_anywhere_is_unreachable(self):
    reachability = RemoteReachability(_config(root={"level": "DEBUG"}))

    assert reachability.threshold_for("any.logger") == UNREACHABLE

  def test_non_propagating_logger_without_handlers_is_unreachable(self):
    reachability = RemoteReachability(
      _config(
        handlers={"remote": {"class": "logging.NullHandler"}},
        loggers={"quiet": {"level": "DEBUG", "propagate": False}},
        root={"level": "DEBUG", "handlers": ["remote"]},
      )
    )

    assert reachability.threshold_for("quiet") == UNREACHABLE
    assert reachability.threshold_for("quiet.child") == UNREACHABLE
    # Other loggers still reach the root handler.
    assert reachability.threshold_for("other") == logging.DEBUG

  def test_logger_own_handlers_bypass_propagation(self):
    reachability = RemoteReachability(
      _config(
        handlers={"own": {"class": "logging.NullHandler", "level": "INFO"}},
        loggers={"direct": {"level": "DEBUG", "propagate": False, "handlers": ["own"]}},
        root={"level": "DEBUG"},
      )
    )

    assert reachability.threshold_for("direct") == logging.INFO

  def test_unknown_level_name_is_permissive(self):
    reachability = RemoteReachability(
      _config(
        handlers={"remote": {"class": "logging.NullHandler", "level": "NOT_A_LEVEL"}},
        root={"level": "ALSO_NOT_A_LEVEL", "handlers": ["remote"]},
      )
    )

    assert reachability.threshold_for("any.logger") == 0

  def test_unresolved_handler_list_string_is_permissive(self):
    reachability = RemoteReachability(_config(root={"level": "INFO", "handlers": "runtime://socket_handlers"}))

    # The handler list could not be analysed, so only the level applies.
    assert reachability.threshold_for("any.logger") == logging.INFO

  def test_unconfigured_chain_defaults_to_warning(self):
    reachability = RemoteReachability(
      _config(
        handlers={"remote": {"class": "logging.NullHandler"}},
        root={"handlers": ["remote"]},
      )
    )

    assert reachability.threshold_for("any.logger") == logging.WARNING

  def test_dotted_prefix_matching_for_unconfigured_children(self):
    reachability = RemoteReachability(
      _config(
        handlers={"remote": {"class": "logging.NullHandler"}},
        loggers={"app": {"level": "ERROR"}},
        root={"level": "DEBUG", "handlers": ["remote"]},
      )
    )

    assert reachability.threshold_for("app.sub.module") == logging.ERROR
    assert reachability.threshold_for("apple") == logging.DEBUG

  def test_broken_config_falls_back_to_send_everything(self):
    class _Explosive:
      """Mapping-ish object whose items() raises during analysis."""

      def get(self, key: str, default: object = None) -> object:
        raise RuntimeError("boom")

    reachability = RemoteReachability(_Explosive())  # pyright: ignore[reportArgumentType]

    assert reachability.threshold_for("any.logger") == 0

  def test_threshold_results_are_cached(self):
    reachability = RemoteReachability(_config(root={"level": "DEBUG"}))

    first = reachability.threshold_for("some.logger")
    second = reachability.threshold_for("some.logger")

    assert first == second == UNREACHABLE
    assert reachability._cache["some.logger"] == UNREACHABLE  # pyright: ignore[reportPrivateUsage]
