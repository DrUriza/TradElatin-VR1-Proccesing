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
