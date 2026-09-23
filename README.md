# TradELATIN VR1 Processing V4.2

Standalone processing repository for TradELATIN VR1 — OBSERVE.

## Implementation status and scope

- Existing C1–C8 Processing is implemented.
- Public validation is primarily emulator/synthetic/replay based.
- A live-provider acquisition path exists for the frozen C1–C8 providers.
- Live operation requires external provider credentials and applicable data access.
- This does **not** mean C1–C8 are a continuously-live public production service.
- C9/Stacks is not implemented yet.
- C9 implementation is proposed grant work and remains outside the current runtime.

The C9 pre-implementation catalog freezes 14 proposed logical source surfaces: 6 C9 Core and 8 transversal. None is implemented in the current runtime.

The current C1–C8 inventory, runtime family names, acquisition routing, endpoint
allowlists, processing pipelines, contract builders, and sample/runtime contracts
remain unchanged. Stacks is not part of the current runtime inventory.

Pre-implementation C9/Stacks documentation:

- [C9/Stacks Technical Specification](docs/c9/stacks/C9_STACKS_TECHNICAL_SPEC.md)
- [C9/Stacks Endpoint Catalog](docs/c9/stacks/C9_STACKS_ENDPOINT_CATALOG.md)

## Automatic runtime

Only these families run automatically:

- `prices_ohlcv`: every 5 s
- `liquidity_microstructure`: KPI/snapshot/table fast lane every 5 s and
  structural graph rebuilds every 10 s
- `cvd_volume_orderflow`: every 15 s

CVD public timeframes: `5m`, `15m`, `4h` only.
For the Futures `Price ↔ CVD Divergence`, native Futures OHLC is preferred when
available; Emulator mode correctly falls back to the canonical Spot BTC price
instead of emitting an all-null series.

The other five families run only when a refresh request is written by HMI:

- `open_interest_and_funding`
- `etf_exchange_flows`
- `on_chain_miners`
- `volatility_market_regimes`
- `long_short_liquidations`

The three automatic structural pipelines use one independent persistent worker
each, so NumPy, pandas and Processing are imported once instead of once per
cycle. Workers recycle after 120 jobs and the scheduler falls back to a
one-shot child if a resident worker cannot start. Manual-only families remain
one-shot processes. This preserves process isolation and Windows concurrency.

## Configuration

- `TRADELATIN_CONTRACT_DIR`: directory where final HMI JSON contracts are written. Default: `./data/contracts`.
- `TRADELATIN_REFRESH_DIR`: directory watched for HMI manual refresh request JSON files. Default: `./data/refresh_requests`.
- `TRADELATIN_EMULATOR_BASE_URL`: Emulator URL. Default: `http://127.0.0.1:8000`.
- `TRADELATIN_PERSISTENT_WORKERS`: set to `0` to use the previous one-shot
  scheduler for automatic families. Default: `1`.
- `TRADELATIN_WORKER_MAX_JOBS`: jobs completed before a worker is recycled.
  Default: `120`.

## Run

Start the standalone Emulator first (or set `TRADELATIN_EMULATOR_BASE_URL` to
another compatible service). On Windows, `start.bat` or `./start.ps1` creates
the local virtual environment and starts Processing. Manual setup:

```powershell
python -m pip install -e .
python main.py --source emulator
```

Single family:

```powershell
python main.py --once --families prices_ohlcv --source emulator
```

### Liquidations manual-refresh scheduling

Liquidations keeps the same frozen endpoint inventory and data contract. In Emulator mode requests remain sequential by default because the synthetic engine is CPU-bound. In live-provider mode independent requests use a bounded worker pool (default 6). Set `TRADELATIN_LIQUIDATIONS_FETCH_WORKERS` to override the worker count (1..16).
