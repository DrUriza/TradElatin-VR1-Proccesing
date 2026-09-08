"""Normalization/persistence merge for Volatility Market Regimes Input.

Long/short positioning is intentionally absent: Liquidations owns it.
"""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from typing import Any

from .volatility_market_regimes_data_raw_extract import (
    BASE_INTERVAL, BOOTSTRAP_HISTORY_DAYS, GLASSNODE_PROVIDER,
    GLASSNODE_DVOL_ENDPOINT_ID, INTERVAL_SECONDS, VALID_MODES,
    VOLATILITY_MARKET_REGIMES_FAMILY,
)

STALE_TOLERANCE = 2 * INTERVAL_SECONDS
DATASETS = {(GLASSNODE_PROVIDER, GLASSNODE_DVOL_ENDPOINT_ID): ("glassnode", "dvol")}
DATASET_IDS = ("glassnode.dvol",)


def _sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _finite(value: Any, name: str, *, non_negative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"invalid_{name}")
    result = float(value)
    if non_negative and result < 0:
        raise ValueError(f"negative_{name}")
    return 0.0 if result == 0 else result


def _timestamp(value: Any, name: str) -> int:
    numeric = _finite(value, name, non_negative=True)
    return int(numeric)


def determine_volatility_market_regimes_input_mode(*, existing_contract: Mapping[str, Any] | None = None,
                                                    recovery_requests: Sequence[Mapping[str, Any]] | None = None,
                                                    requested_mode: str | None = None) -> str:
    if requested_mode is not None:
        if requested_mode not in VALID_MODES:
            raise ValueError("unsupported_mode")
        if requested_mode == "recovery" and not recovery_requests:
            raise ValueError("recovery_requests_required")
        return requested_mode
    if recovery_requests:
        return "recovery"
    providers = existing_contract.get("providers", {}) if isinstance(existing_contract, Mapping) else {}
    dvol = providers.get("glassnode", {}).get("dvol", {}) if isinstance(providers, Mapping) else {}
    return "incremental" if isinstance(dvol, Mapping) and dvol.get("records") else "bootstrap"


def unwrap_glassnode_response(response: Any) -> list[Any]:
    if not _sequence(response):
        raise ValueError("invalid_envelope")
    return list(response)


def normalize_glassnode_dvol_record(record: Any) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise ValueError("invalid_record")
    if isinstance(record.get("v"), Mapping):
        payload = record["v"]
    elif isinstance(record.get("o"), Mapping):
        payload = record["o"]
    else:
        payload = record
    o = _finite(payload.get("o"), "dvol_open", non_negative=True)
    h = _finite(payload.get("h"), "dvol_high", non_negative=True)
    l = _finite(payload.get("l"), "dvol_low", non_negative=True)
    c = _finite(payload.get("c"), "dvol_close", non_negative=True)
    if h < max(o, c) or l > min(o, c):
        raise ValueError("invalid_dvol_ohlc")
    return {"timestamp": _timestamp(record.get("t"), "timestamp"), "open": o, "high": h, "low": l, "close": c}


def upsert_timestamp_records(existing_records: Sequence[Mapping[str, Any]], incoming_records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    values = {int(row["timestamp"]): copy.deepcopy(dict(row)) for row in existing_records}
    values.update({int(row["timestamp"]): copy.deepcopy(dict(row)) for row in incoming_records})
    return [values[key] for key in sorted(values)]


def detect_hourly_gaps(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ranges=[]
    for previous,current in zip(records,records[1:]):
        diff=int(current["timestamp"])-int(previous["timestamp"])
        if diff>INTERVAL_SECONDS:
            ranges.append({"after_timestamp":int(previous["timestamp"]),"before_timestamp":int(current["timestamp"]),"missing_intervals":diff//INTERVAL_SECONDS-1})
    return {"gap_count":len(ranges),"gap_ranges":ranges}


def _previous(existing: Mapping[str, Any] | None, name: str) -> Mapping[str, Any]:
    payload = existing.get("providers", {}).get("glassnode", {}).get(name, {}) if isinstance(existing, Mapping) else {}
    return payload if isinstance(payload, Mapping) else {}


def _dataset(*, requests: Sequence[Mapping[str, Any]], existing: Mapping[str, Any], endpoint_id: str,
             reference_timestamp: int, execution_timestamp: int) -> dict[str, Any]:
    normalizer = normalize_glassnode_dvol_record
    incoming_by_ts: dict[int,dict[str,Any]]={}
    failed=invalid_count=successful=empty=0
    warnings=[]; errors=[]
    for request in requests:
        if request.get("status") != "ok":
            failed += 1; warnings.append(f"{request.get('request_id')}:request_failed"); continue
        try:
            rows=unwrap_glassnode_response(request.get("response")); successful += 1
            if not rows: empty += 1
            for raw in rows:
                try:
                    row=normalizer(raw); incoming_by_ts[row["timestamp"]]=row
                except (TypeError,ValueError): invalid_count += 1
        except (TypeError,ValueError):
            errors.append(f"{request.get('request_id')}:invalid_envelope")
    incoming=[incoming_by_ts[k] for k in sorted(incoming_by_ts)]
    prior=copy.deepcopy(existing.get("records",[])) if isinstance(existing.get("records",[]),list) else []
    records=upsert_timestamp_records(prior,incoming)
    gaps=detect_hourly_gaps(records)
    if not records:
        status="invalid" if errors or invalid_count else "unavailable"; reason="invalid_response" if status=="invalid" else "empty_response"
    elif errors or failed or invalid_count or gaps["gap_count"]:
        status="partial"; reason="latest_refresh_partial"
    elif records[-1]["timestamp"] < reference_timestamp - STALE_TOLERANCE:
        status="partial"; reason="stale_latest_record"
    else:
        status="available"; reason=None
    return {"status":status,"reason":reason,"interval":BASE_INTERVAL,"interval_seconds":INTERVAL_SECONDS,
            "records":records,"incoming_records":incoming,"records_available":len(records),
            "first_available_timestamp":records[0]["timestamp"] if records else None,
            "last_available_timestamp":records[-1]["timestamp"] if records else None, **gaps,
            "source_data_as_of":records[-1]["timestamp"] if records else None,
            "latest_attempt":{"request_ids":[str(x.get("request_id")) for x in requests],"successful":successful,"failed":failed,
                              "invalid_envelopes":len(errors),"invalid_records":invalid_count,"empty_responses":empty},
            "provenance":{"provider":"glassnode","endpoint_id":endpoint_id,"reference_timestamp":reference_timestamp,
                          "execution_timestamp":execution_timestamp},"warnings":sorted(set(warnings)),"errors":sorted(set(errors))}


def evaluate_volatility_market_regimes_input_quality(providers: Mapping[str, Any], *, mode: str,
                                                     required_datasets: Sequence[str] = DATASET_IDS) -> dict[str, Any]:
    statuses={"glassnode.dvol": providers["glassnode"]["dvol"]["status"]}
    required=list(required_datasets)
    missing=[x for x in required if statuses[x]=="unavailable"]
    partial=[x for x in required if statuses[x]=="partial"]
    invalid=[x for x in required if statuses[x]=="invalid"]
    status="invalid" if invalid or (mode=="bootstrap" and missing) else "partial" if missing or partial else "ok"
    return {"status":status,"required_datasets":required,"missing_required_datasets":missing,"partial_datasets":partial,
            "invalid_datasets":invalid,"recovery_required":bool(missing or partial or invalid),
            "warnings":sorted([f"{x}:{statuses[x]}" for x in required if statuses[x] in {"partial","unavailable"}]),
            "errors":sorted([f"{x}:invalid" for x in invalid])}


class VolatilityMarketRegimesInputPreprocessor:
    def __init__(self, *, existing_contract: Mapping[str, Any] | None = None) -> None:
        self.existing_contract=copy.deepcopy(existing_contract)

    def run(self, raw_bundle: Mapping[str, Any]) -> dict[str, Any]:
        if (not isinstance(raw_bundle,Mapping) or raw_bundle.get("family")!=VOLATILITY_MARKET_REGIMES_FAMILY
                or raw_bundle.get("stage")!="extracted_raw" or raw_bundle.get("mode") not in VALID_MODES
                or not isinstance(raw_bundle.get("requests"),list)):
            raise ValueError("invalid_raw_bundle")
        mode=raw_bundle["mode"]; reference=raw_bundle.get("reference_timestamp"); execution=raw_bundle.get("execution_timestamp")
        if type(reference) is not int or type(execution) is not int: raise ValueError("invalid_raw_timestamps")
        grouped={key:[] for key in DATASETS}
        for req in raw_bundle["requests"]:
            key=(req.get("provider"),req.get("endpoint_id")) if isinstance(req,Mapping) else None
            if key not in DATASETS: raise ValueError("invalid_raw_request")
            grouped[key].append(req)
        providers={"glassnode":{}}
        targeted=[]
        for key,(_,name) in DATASETS.items():
            requests=grouped[key]; previous=_previous(self.existing_contract,name)
            if mode in {"incremental","recovery"} and not requests and previous:
                payload=copy.deepcopy(previous); payload["latest_attempt"]={"request_ids":[],"successful":0,"failed":0,"invalid_envelopes":0,"invalid_records":0,"empty_responses":0,"skipped":"not_scheduled_this_cycle"}
            else:
                payload=_dataset(requests=requests,existing=previous,endpoint_id=key[1],reference_timestamp=reference,execution_timestamp=execution)
                if requests: targeted.append(f"glassnode.{name}")
            providers["glassnode"][name]=payload
        required=targeted if mode=="recovery" else list(DATASET_IDS)
        quality=evaluate_volatility_market_regimes_input_quality(providers,mode=mode,required_datasets=required)
        return {"schema":{"id":"trad_elatin.volatility_market_regimes.input.v1","version":"1.1.0"},
                "family":VOLATILITY_MARKET_REGIMES_FAMILY,"stage":"input","mode":mode,
                "reference_timestamp":reference,"execution_timestamp":execution,
                "dimensions":{"asset":"BTC","symbol":"BTCUSDT","interval":BASE_INTERVAL},
                "providers":providers,"quality":quality}


def preprocess_volatility_market_regimes_input(raw_bundle: Mapping[str, Any], *, existing_contract: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return VolatilityMarketRegimesInputPreprocessor(existing_contract=existing_contract).run(raw_bundle)


def run_volatility_market_regimes_input(*, fetcher, reference_timestamp: int,
                                        existing_contract: Mapping[str, Any] | None = None,
                                        requested_mode: str | None = None,
                                        recovery_requests: Sequence[Mapping[str, Any]] | None = None,
                                        clock=None, bootstrap_history_days: int = BOOTSTRAP_HISTORY_DAYS,
                                        incremental_hours: int = 2) -> dict[str, Any]:
    from .volatility_market_regimes_data_raw_extract import VolatilityMarketRegimesRawExtractor
    mode = determine_volatility_market_regimes_input_mode(existing_contract=existing_contract,
        recovery_requests=recovery_requests, requested_mode=requested_mode)
    raw = VolatilityMarketRegimesRawExtractor(fetcher, clock=clock).run(
        mode=mode, reference_timestamp=reference_timestamp, recovery_requests=recovery_requests,
        bootstrap_history_days=bootstrap_history_days, incremental_hours=incremental_hours,
        existing_contract=existing_contract)
    return VolatilityMarketRegimesInputPreprocessor(existing_contract=existing_contract).run(raw)
