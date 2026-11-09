# Temporal Contract v1.0.0 — reference adapter
Status: **CONTRACT ACCEPTED / INTEGRATION PENDING**, additive and isolated.
This is the Python counterpart of the frozen [Screen contract](https://github.com/DrUriza/TradElatinVR_Screen/blob/feature/screen-emulator-dynamic/docs/TRADELATIN_TEMPORAL_CONTRACT_v1.0.0.md).

The cross-component version `1.0.0` specifies allowable timeframes for every VR and VR1 C1–C8 family, plus a mandatory distinction between source candles and daily aggregation windows. Both Input and Processing must validate incoming `temporalContract` packets. `1m,5m,15m,1H,4H,1D` are valid source candle intervals; `7D,30D,90D` are not source candles and map to daily observations + calendar-day windows.

## Contract adoption
- `contracts/temporal_contract_v1.py`: dependency-free Python mirror of the Screen `temporalContract.js` (source of truth).
- `tests/test_temporal_contract_v1.py`: strict validation and window semantics.
- This branch changes **no** existing endpoints, Acquisition routing, provider configuration, historical frozen schema, data normalizers or runtime launches.
- To wire this into operational ingestion/processing, introduce a separate integration PR once adapters and schemas have been individually reviewed. Until then, the validator is importable but **not yet invoked by every existing path**.
- Input must retain source timestamps, status, actual market observation cadence and original source frame. Processing must compute 7D/30D/90D from daily observations without fake new candles and propagate the contract. The Screen consumer already uses it in its working PR.
- External project versions, VR1 Source Emulator, and Processing remain separately versioned; this is a shared temporal vocabulary, not a new real data provider.

Smoke test: `python -m unittest discover -s tests -p test_temporal_contract_v1.py -v`.

No proprietary sources or paid providers added. The existing C1–C8 runtime remains unchanged.
