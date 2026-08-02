"""Tests for `aeth_ext.central_log_server.web_viewer.server.InLoopServer`."""

# Standard library imports
import asyncio
from typing import TYPE_CHECKING, override

# Third party imports
import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

# First party imports
from aeth_ext.central_log_server.web_viewer import server as server_mod
from aeth_ext.central_log_server.web_viewer.server import InLoopServer

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import AsyncIterator


def _make_server() -> InLoopServer:
  return InLoopServer(command="unused-command-in-these-tests")


class TestOnStartup:
  async def test_does_not_print_the_vendor_banner(self, capsys: pytest.CaptureFixture[str]):
    """The base `Server.on_startup` prints an ASCII banner via `self.console`.

    Overridden to a no-op (see the docstring on `InLoopServer.on_startup`)
    rather than redirected, so nothing should land on either stream --
    unlike the redirect-to-stderr approach this replaced, which would have
    left `captured.err` non-empty here.
    """
    server = _make_server()

    await server.on_startup(web.Application())

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


class TestFavicon:
  async def test_returns_a_file_response_pointing_at_the_favicon_path(self):
    server = _make_server()
    app = web.Application()
    app.router.add_get("/favicon.ico", server.favicon)

    async with TestClient(TestServer(app)) as client:
      resp = await client.get("/favicon.ico")

      assert resp.status == web.HTTPOk.status_code
      await resp.release()


class _RaisingAppService:
  """Stand-in for `SessionAppService` that fails as soon as it's started."""

  def __init__(self, *args: object, **kwargs: object) -> None:
    self.stopped = False

  async def start(self, width: int, height: int) -> None:
    raise ValueError("simulated app-service startup failure")

  async def stop(self) -> None:
    self.stopped = True


class _CancellingAppService(_RaisingAppService):
  @override
  async def start(self, width: int, height: int) -> None:
    raise asyncio.CancelledError


@pytest.fixture
async def ws_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TestClient]:
  server = _make_server()
  app = web.Application()
  app.router.add_get("/ws", server.handle_websocket)

  async with TestClient(TestServer(app)) as client:
    yield client


class TestHandleWebsocketGenericExceptionPath:
  async def test_logged_and_alerted_but_connection_still_closes_cleanly(
    self, ws_client: TestClient, monkeypatch: pytest.MonkeyPatch
  ):
    monkeypatch.setattr(server_mod, "SessionAppService", _RaisingAppService)
    alert_calls: list[tuple[str, BaseException]] = []
    monkeypatch.setattr(server_mod, "alert_exception", lambda label, exc: alert_calls.append((label, exc)))

    async with ws_client.ws_connect("/ws") as ws:
      msg = await ws.receive()
      assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING)

    assert len(alert_calls) == 1
    label, exc = alert_calls[0]
    assert label == "InLoopServer.handle_websocket"
    assert isinstance(exc, ValueError)


class TestHandleWebsocketCancelledErrorPath:
  async def test_closes_the_websocket_without_alerting(self, ws_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server_mod, "SessionAppService", _CancellingAppService)
    alert_calls: list[object] = []
    monkeypatch.setattr(server_mod, "alert_exception", lambda *a: alert_calls.append(a))

    async with ws_client.ws_connect("/ws") as ws:
      msg = await ws.receive()
      assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING)

    assert alert_calls == []
