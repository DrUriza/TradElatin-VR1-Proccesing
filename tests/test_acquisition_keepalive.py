from __future__ import annotations

import http.client
from pathlib import Path

from processing_signals.input import acquisition


class _Response:
    status = 200

    def read(self) -> bytes:
        return b'{"ok":true}'


class _ReusableConnection:
    instances: list["_ReusableConnection"] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.requests = 0
        self.closed = False
        self.instances.append(self)

    def request(self, method: str, target: str, headers: dict[str, str]) -> None:
        assert method == "GET"
        assert target.startswith("/api/")
        assert headers["X-TradELATIN-Provider"] == "coinglass"
        self.requests += 1

    def getresponse(self) -> _Response:
        return _Response()

    def close(self) -> None:
        self.closed = True


def test_emulator_connection_survives_acquisition_client_recreation(
    monkeypatch, tmp_path: Path
) -> None:
    _ReusableConnection.instances.clear()
    acquisition._close_emulator_connections()
    monkeypatch.setattr(acquisition.http.client, "HTTPConnection", _ReusableConnection)
    monkeypatch.setenv("TRADELATIN_EMULATOR_BASE_URL", "http://127.0.0.1:8000")

    first = acquisition.AcquisitionClient(tmp_path, "prices_ohlcv")
    second = acquisition.AcquisitionClient(tmp_path, "prices_ohlcv")
    first.fetch(
        provider="coinglass",
        endpoint_id="spot_ohlcv",
        path="/api/spot/price/history",
        params={},
    )
    second.fetch(
        provider="coinglass",
        endpoint_id="spot_aggregated_cvd",
        path="/api/spot/aggregated-cvd/history",
        params={},
    )

    assert len(_ReusableConnection.instances) == 1
    assert _ReusableConnection.instances[0].requests == 2
    acquisition._close_emulator_connections()


def test_emulator_connection_reconnects_once_after_remote_close(
    monkeypatch, tmp_path: Path
) -> None:
    class _ReconnectConnection(_ReusableConnection):
        instances: list["_ReconnectConnection"] = []

        def getresponse(self) -> _Response:
            if len(self.instances) == 1:
                raise http.client.RemoteDisconnected("server closed keep-alive")
            return _Response()

    acquisition._close_emulator_connections()
    monkeypatch.setattr(acquisition.http.client, "HTTPConnection", _ReconnectConnection)
    monkeypatch.setenv("TRADELATIN_EMULATOR_BASE_URL", "http://127.0.0.1:8000")

    client = acquisition.AcquisitionClient(tmp_path, "prices_ohlcv")
    payload = client.fetch(
        provider="coinglass",
        endpoint_id="spot_ohlcv",
        path="/api/spot/price/history",
        params={},
    )

    assert payload == {"ok": True}
    assert len(_ReconnectConnection.instances) == 2
    assert _ReconnectConnection.instances[0].closed is True
    acquisition._close_emulator_connections()


def test_emulator_cleanup_is_idempotent() -> None:
    connection = _ReusableConnection("127.0.0.1", 8000, 30.0)
    key = ("http", "127.0.0.1", 8000, 30.0)
    acquisition._EMULATOR_CONNECTIONS[key] = connection

    acquisition._close_emulator_connections()
    acquisition._close_emulator_connections()

    assert connection.closed is True
    assert acquisition._EMULATOR_CONNECTIONS == {}


def test_emulator_connection_failure_retries_once_and_cleans_pool(
    monkeypatch, tmp_path: Path
) -> None:
    class _FailingConnection(_ReusableConnection):
        instances: list["_FailingConnection"] = []

        def request(self, method: str, target: str, headers: dict[str, str]) -> None:
            super().request(method, target, headers)
            raise ConnectionRefusedError("emulator unavailable")

    acquisition._close_emulator_connections()
    monkeypatch.setattr(acquisition.http.client, "HTTPConnection", _FailingConnection)
    monkeypatch.setenv("TRADELATIN_EMULATOR_BASE_URL", "http://127.0.0.1:8000")

    client = acquisition.AcquisitionClient(tmp_path, "prices_ohlcv")
    try:
        client.fetch(
            provider="coinglass",
            endpoint_id="spot_ohlcv",
            path="/api/spot/price/history",
            params={},
        )
    except RuntimeError as exc:
        assert "provider_connection_error:emulator" in str(exc)
    else:
        raise AssertionError("connection failure should be reported")

    assert len(_FailingConnection.instances) == 2
    assert all(connection.closed for connection in _FailingConnection.instances)
    assert acquisition._EMULATOR_CONNECTIONS == {}


def test_live_acquisition_keeps_existing_non_pooled_path(
    monkeypatch, tmp_path: Path
) -> None:
    class _LiveResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok":true}'

    def _unexpected_connection(*args, **kwargs):
        raise AssertionError("LIVE acquisition must not use Emulator pool")

    monkeypatch.setattr(acquisition.http.client, "HTTPConnection", _unexpected_connection)
    monkeypatch.setattr(acquisition, "urlopen", lambda request, timeout: _LiveResponse())
    monkeypatch.setenv("COINGLASS_API_KEY", "test-key")

    client = acquisition.AcquisitionClient(
        tmp_path, "prices_ohlcv", source_mode="live"
    )
    payload = client.fetch(
        provider="coinglass",
        endpoint_id="spot_ohlcv",
        path="/api/spot/price/history",
        params={},
    )

    assert payload == {"ok": True}
    assert acquisition._EMULATOR_CONNECTIONS == {}
