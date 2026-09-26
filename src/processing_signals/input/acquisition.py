"""External RAW acquisition for TradELATIN Input.

This is the only module in Processing allowed to talk to the Emulator or to
real providers. Family extractors describe *what* they need (provider,
endpoint_id, path and params); Acquisition decides *where* the request goes.
"""
from __future__ import annotations

import atexit
import hashlib
import http.client
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from processing_signals.main.atomic_replace import replace_with_retry

SOURCE_MODES = {"emulator", "live"}
EMULATOR_RECORD_LIMIT = 500

# Frozen VR1 external surface: 20 CoinGlass + 4 CryptoQuant + 9 Glassnode.
ALLOWED_ENDPOINTS = frozenset({
    # CoinGlass — 20
    ("coinglass", "spot_ohlcv"),
    ("coinglass", "spot_aggregated_cvd"),
    ("coinglass", "futures_aggregated_cvd"),
    ("coinglass", "spot_footprint"),
    ("coinglass", "aggregated_open_interest_ohlc"),
    ("coinglass", "oi_weighted_funding_rate_ohlc"),
    ("coinglass", "bitcoin_etf_flows"),
    ("coinglass", "bitcoin_etf_list"),
    ("coinglass", "aggregated_liquidation_history"),
    ("coinglass", "aggregated_liquidation_map"),
    ("coinglass", "pair_liquidation_map"),
    ("coinglass", "liquidation_order_events"),
    ("coinglass", "top_position_long_short_ratio"),
    ("coinglass", "top_account_long_short_ratio"),
    ("coinglass", "global_account_long_short_ratio"),
    ("coinglass", "spot_orderbook_heatmap"),
    ("coinglass", "perpetual_orderbook_heatmap"),
    ("coinglass", "futures_footprint"),
    ("coinglass", "spot_large_limit_orders"),
    ("coinglass", "perpetual_large_limit_orders"),
    # CryptoQuant — 4
    ("cryptoquant", "exchange_inflow"),
    ("cryptoquant", "exchange_outflow"),
    ("cryptoquant", "exchange_reserve"),
    ("cryptoquant", "mpi"),
    # Glassnode — 9
    ("glassnode", "marketcap_usd"),
    ("glassnode", "futures_estimated_leverage_ratio"),
    ("glassnode", "balance_miners_sum"),
    ("glassnode", "sopr"),
    ("glassnode", "hash_rate_mean"),
    ("glassnode", "difficulty_latest"),
    ("glassnode", "transfers_volume_from_miners_sum"),
    ("glassnode", "revenue_sum"),
    ("glassnode", "dvol_ohlc"),
})

# spot_footprint is intentionally shared by CVD and Liquidity.  The set has
# 33 unique (provider, logical endpoint) pairs despite that cross-family reuse.
if len(ALLOWED_ENDPOINTS) != 33:
    raise RuntimeError(f"VR1 endpoint allowlist must contain exactly 33 endpoints, got {len(ALLOWED_ENDPOINTS)}")

LIVE_BASE_URLS = {
    "coinglass": "https://open-api-v4.coinglass.com",
    "cryptoquant": "https://api.cryptoquant.com/v1",
    "glassnode": "https://api.glassnode.com",
}

# Emulator requests are frequent and target one local service.  Reuse one
# connection per origin/timeout inside a process, including across short-lived
# AcquisitionClient instances.  LIVE providers deliberately keep the existing
# urllib path and are never stored in this pool.
_EMULATOR_CONNECTIONS: dict[
    tuple[str, str, int, float], http.client.HTTPConnection
] = {}
_EMULATOR_CONNECTION_LOCK = threading.RLock()


def _close_connection(connection: http.client.HTTPConnection) -> None:
    try:
        connection.close()
    except (OSError, http.client.HTTPException):
        # Cleanup is best-effort and must stay safe during interpreter shutdown.
        pass


def _close_emulator_connections() -> None:
    """Close and forget all pooled Emulator connections.

    The operation is idempotent and intentionally private: normal callers do
    not manage the pool, while tests and process lifecycle hooks can force a
    deterministic shutdown.
    """
    with _EMULATOR_CONNECTION_LOCK:
        connections = tuple(_EMULATOR_CONNECTIONS.values())
        _EMULATOR_CONNECTIONS.clear()
    for connection in connections:
        _close_connection(connection)


atexit.register(_close_emulator_connections)


def _emulator_origin(url: str, timeout_seconds: float) -> tuple[
    tuple[str, str, int, float], str
]:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("invalid_emulator_url")
    port = parsed.port or (443 if scheme == "https" else 80)
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    return (scheme, parsed.hostname, port, float(timeout_seconds)), target


def _new_emulator_connection(
    key: tuple[str, str, int, float]
) -> http.client.HTTPConnection:
    scheme, host, port, timeout = key
    connection_type = (
        http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    )
    return connection_type(host, port, timeout=timeout)


def _drop_emulator_connection(
    key: tuple[str, str, int, float], connection: http.client.HTTPConnection
) -> None:
    if _EMULATOR_CONNECTIONS.get(key) is connection:
        _EMULATOR_CONNECTIONS.pop(key, None)
    _close_connection(connection)


def _fetch_emulator(
    *, provider: str, url: str, headers: Mapping[str, str], timeout_seconds: float
) -> bytes:
    """GET an Emulator payload, retrying once after a broken connection."""
    key, target = _emulator_origin(url, timeout_seconds)
    with _EMULATOR_CONNECTION_LOCK:
        for attempt in range(2):
            connection = _EMULATOR_CONNECTIONS.get(key)
            if connection is None:
                connection = _new_emulator_connection(key)
                _EMULATOR_CONNECTIONS[key] = connection
            try:
                connection.request("GET", target, headers=dict(headers))
                response = connection.getresponse()
                body = response.read()
                status = int(response.status)
                if status >= 400:
                    detail = body[:512].decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"provider_http_error:{provider}:{status}:{detail}"
                    )
                return body
            except RuntimeError:
                raise
            except (http.client.HTTPException, OSError) as exc:
                _drop_emulator_connection(key, connection)
                if attempt == 1:
                    raise RuntimeError(f"provider_connection_error:emulator:{exc}") from exc
        raise AssertionError("unreachable_emulator_retry_state")


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _query(params: Mapping[str, Any]) -> str:
    clean = {str(key): value for key, value in params.items() if value is not None}
    return urlencode(clean, doseq=True)


def _provider_base(provider: str) -> str:
    env_name = f"TRADELATIN_{provider.upper()}_BASE_URL"
    return os.environ.get(env_name, LIVE_BASE_URLS[provider]).rstrip("/")


def _auth(provider: str, params: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    headers = {"Accept": "application/json", "User-Agent": "TradELATIN-VR1/1.0"}
    query = dict(params)
    if provider == "coinglass":
        key = os.environ.get("COINGLASS_API_KEY", "").strip()
        if not key:
            raise RuntimeError("COINGLASS_API_KEY is required in live mode")
        headers["CG-API-KEY"] = key
    elif provider == "cryptoquant":
        key = os.environ.get("CRYPTOQUANT_API_KEY", "").strip()
        if not key:
            raise RuntimeError("CRYPTOQUANT_API_KEY is required in live mode")
        headers["Authorization"] = f"Bearer {key}"
    elif provider == "glassnode":
        key = os.environ.get("GLASSNODE_API_KEY", "").strip()
        if not key:
            raise RuntimeError("GLASSNODE_API_KEY is required in live mode")
        query["api_key"] = key
    return headers, query


@dataclass(frozen=True)
class AcquisitionClient:
    repo_root: Path
    family: str
    source_mode: str = "emulator"
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.source_mode not in SOURCE_MODES:
            raise ValueError(f"unsupported_source_mode:{self.source_mode}")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds_must_be_positive")

    @property
    def raw_root(self) -> Path:
        return self.repo_root / "data" / "contracts" / "input_raw"

    def _effective_params(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Return the exact query parameters sent to the selected source.

        Emulator integration is intentionally deterministic: every one of the
        33 logical endpoints is requested with ``limit=500``.  Live providers
        keep the family-specific request plan untouched because their limits,
        pagination and window semantics differ.
        """
        effective = dict(params)
        if self.source_mode == "emulator":
            effective["limit"] = EMULATOR_RECORD_LIMIT
        return effective

    def _url_and_headers(self, provider: str, endpoint_id: str, path: str, params: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
        if self.source_mode == "emulator":
            base = os.environ.get("TRADELATIN_EMULATOR_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
            headers = {
                "Accept": "application/json",
                "User-Agent": "TradELATIN-VR1/1.0",
                "X-TradELATIN-Provider": provider,
                "X-TradELATIN-Endpoint": endpoint_id,
                "X-TradELATIN-Family": self.family,
            }
            query_params = dict(params)
        else:
            base = _provider_base(provider)
            headers, query_params = _auth(provider, params)
        query = _query(query_params)
        return f"{base}{path}{'?' + query if query else ''}", headers

    def _persist(self, *, provider: str, endpoint_id: str, path: str, params: Mapping[str, Any], response: Any) -> None:
        identity = json.dumps({"path": path, "params": dict(params)}, sort_keys=True, separators=(",", ":"), default=str)
        request_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        root = self.raw_root / provider / self.family / endpoint_id
        _atomic_json(root / "latest.json", response)
        _atomic_json(root / "request.json", {
            "provider": provider,
            "family": self.family,
            "endpoint_id": endpoint_id,
            "path": path,
            "params": dict(params),
            "request_hash": request_hash,
            "source_mode": self.source_mode,
        })

    def fetch(self, **request_data: Any) -> Any:
        provider = str(request_data.get("provider", "")).strip().lower()
        endpoint_id = str(request_data.get("endpoint_id", "")).strip()
        path = str(request_data.get("path", "")).strip()
        params = request_data.get("params") or {}
        if provider not in LIVE_BASE_URLS:
            raise ValueError(f"unsupported_provider:{provider}")
        if (provider, endpoint_id) not in ALLOWED_ENDPOINTS:
            raise ValueError(f"endpoint_not_in_vr1_allowlist:{provider}:{endpoint_id}")
        if not path.startswith("/"):
            raise ValueError("provider_path_must_be_absolute")
        if not isinstance(params, Mapping):
            raise TypeError("request_params_must_be_mapping")

        effective_params = self._effective_params(params)
        url, headers = self._url_and_headers(provider, endpoint_id, path, effective_params)
        if self.source_mode == "emulator":
            body = _fetch_emulator(
                provider=provider,
                url=url,
                headers=headers,
                timeout_seconds=float(self.timeout_seconds),
            )
        else:
            try:
                with urlopen(Request(url, headers=headers, method="GET"), timeout=float(self.timeout_seconds)) as response:
                    body = response.read()
            except HTTPError as exc:
                detail = exc.read(512).decode("utf-8", errors="replace")
                raise RuntimeError(f"provider_http_error:{provider}:{exc.code}:{detail}") from exc
            except URLError as exc:
                raise RuntimeError(f"provider_connection_error:{provider}:{exc.reason}") from exc
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"provider_invalid_json:{provider}:{endpoint_id}") from exc
        self._persist(provider=provider, endpoint_id=endpoint_id, path=path, params=effective_params, response=payload)
        return payload


def build_family_fetcher(*, repo_root: str | Path, family: str, source_mode: str, timeout_seconds: float = 30.0):
    return AcquisitionClient(Path(repo_root), family, source_mode, timeout_seconds).fetch
