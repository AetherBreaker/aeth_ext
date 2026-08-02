"""Tests for `aeth_ext.ftp.adapter.FTPAdapter` (the protocol-dispatching factory)."""

# Standard library imports
from contextvars import ContextVar
from typing import TYPE_CHECKING, override

# Third party imports
import pytest

# First party imports
from aeth_ext.ftp.adapter import AdaptedFTP, AdaptedSFTP, FTPAdapter
from aeth_ext.ftp.types import FTPProtocol, SFTPProtocol

if TYPE_CHECKING:
  # Standard library imports
  from ftplib import FTP

  # Third party imports
  from paramiko import SFTPClient


class _NoOpFTPProtocol(FTPProtocol):
  @override
  def get_conn_handler(self) -> FTP:
    raise NotImplementedError

  @override
  def close_conn_handler(self) -> None:
    pass


class _NoOpSFTPProtocol(SFTPProtocol):
  @override
  def get_conn_handler(self) -> SFTPClient:
    raise NotImplementedError

  @override
  def close_conn_handler(self) -> None:
    pass


class TestProtocolResolution:
  def test_ftp_protocol_subclass_resolves_to_adapted_ftp(self):
    adapter = FTPAdapter(_NoOpFTPProtocol)

    assert adapter.protocol_handler is AdaptedFTP

  def test_sftp_protocol_subclass_resolves_to_adapted_sftp(self):
    adapter = FTPAdapter(_NoOpSFTPProtocol)

    assert adapter.protocol_handler is AdaptedSFTP

  def test_unrelated_type_raises_type_error(self):
    class NotAProtocol:
      pass

    with pytest.raises(TypeError):
      FTPAdapter(NotAProtocol)  # pyright: ignore[reportArgumentType]


class TestContainerClsResolution:
  def test_plain_string_is_used_directly(self):
    adapter = FTPAdapter(_NoOpFTPProtocol, container_cls="explicit-name")

    session = adapter.start_session()

    assert session.container_cls == "explicit-name"

  def test_contextvar_is_preferred_when_set(self):
    cvar: ContextVar[str] = ContextVar("test_container_cvar")
    cvar.set("from-contextvar")
    adapter = FTPAdapter(_NoOpFTPProtocol, container_cls="fallback-name", container_cvar=cvar)

    session = adapter.start_session()

    assert session.container_cls == "from-contextvar"

  def test_falls_back_to_plain_string_when_contextvar_is_unset(self):
    cvar: ContextVar[str] = ContextVar("test_container_cvar_unset")
    adapter = FTPAdapter(_NoOpFTPProtocol, container_cls="fallback-name", container_cvar=cvar)

    session = adapter.start_session()

    assert session.container_cls == "fallback-name"


class TestTestConnectionDelegation:
  def test_delegates_to_a_fresh_sessions_test_connection(self, monkeypatch: pytest.MonkeyPatch):
    """`FTPAdapter.test_connection` should be a thin delegate to
    `start_session().test_connection(logit)` -- exercised here via a spy
    rather than a real connection, since the real-connection path is already
    covered end-to-end by `AdaptedFTP`/`AdaptedSFTP`'s own test suites."""
    calls: list[bool] = []

    class _FakeSession:
      def test_connection(self, logit: bool = False) -> bool:
        calls.append(logit)
        return True

    adapter = FTPAdapter(_NoOpFTPProtocol)
    # FTPAdapter is __slots__-based, so an instance can't shadow a class method --
    # patch the class itself instead.
    monkeypatch.setattr(FTPAdapter, "start_session", lambda self: _FakeSession())

    result = adapter.test_connection(logit=True)

    assert result is True
    assert calls == [True]
