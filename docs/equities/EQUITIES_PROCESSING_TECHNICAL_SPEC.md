# Equities Multi-Market Processing Technical Specification

**Status:** PRE-IMPLEMENTATION — PROPOSED / NOT IMPLEMENTED  
**Scope:** TradELATIN VR1 Processing only  
**Source system evaluated:** SSL Market / IBKR  
**Catalog class:** PROPOSED EQUITIES OBSERVABLES

## 1. Purpose and boundary

This document defines the proposed extension of TradELATIN VR1 Processing from
its implemented BTC/CRYPTO pipeline to an additional `EQUITIES` market class.
It freezes the design before implementation.

VR1 Processing answers: **What market data and observables can be acquired,
normalized, and calculated?** It does not interpret structural regimes, predict
direction, select trades, size positions, manage risk, or execute orders.

SSL Market is a functional source for acquisition and observable-calculation
patterns, not an architecture to copy wholesale. This change creates no IBKR
adapter, Equities acquisition path, normalizer, processor, contract builder,
runtime registration, allowlist entry, fixture, or operational Equities support.

## 2. Frozen compatibility boundary

The implemented inventory remains exactly:

> **33 logical endpoints across C1–C8**

Those endpoints remain the frozen BTC/CRYPTO operational inventory. Equities
must not delete, replace, rename, renumber, reclassify, or change ownership of an
existing endpoint. It must not change CoinGlass, CryptoQuant, Glassnode, current
BTC contracts, acquisition routing, runtime behavior, or pipelines.

Every new ID in the companion catalog is a `PROPOSED EQUITIES OBSERVABLE`. It
is not part of the 33 endpoints and cannot enter an executable allowlist without
separate approval and compatibility review.

C9 remains separate as `PRE-IMPLEMENTATION / PROPOSED / NOT IMPLEMENTED`.
Equities data from SSL/IBKR does not become C9 data.

## 3. Target multi-market architecture

```text
Provider Adapter
    → Acquisition
    → Raw Validation
    → Market Normalization
    → Observable Calculation
    → Versioned VR1 Contract
    → Screen / HMI
```

The stages are shared while provider acquisition and market normalization remain
isolated:

```text
CRYPTO   → current provider adapters → current BTC behavior (unchanged)
EQUITIES → proposed IBKR adapter     → proposed Equities observables
```

IBKR is a provider adapter, not a parallel Processing architecture. Screen must
never connect to IBKR directly.

### Stage responsibilities

1. **Provider Adapter:** preserves provider fields, entitlement/data type, venue,
   request identity, and timestamps.
2. **Acquisition:** manages read-only subscriptions, snapshots, pacing, recovery,
   and completeness without assigning financial meaning.
3. **Raw Validation:** checks prices, sizes, timestamps, depth operations,
   duplicates, stale/crossed quotes, and incomplete book state.
4. **Market Normalization:** produces canonical quote, trade, bar, depth, halt,
   borrow-availability, and reference-market events.
5. **Observable Calculation:** emits descriptive raw, derived, or explicitly
   inferred observables with formulas, windows, units, and provenance.
6. **Versioned VR1 Contract:** publishes market-aware outputs without changing
   existing BTC contract semantics.
7. **Screen/HMI:** renders Processing contracts without silently recomputing them.

## 4. SSL Market component disposition

| SSL component | VR1 classification | Treatment | Boundary |
|---|---|---|---|
| `broker/ibkr_readonly.py` | Acquisition | Adapt | Retain read-only and account/order blocking principles. |
| `broker/ibkr_market_data.py` | Acquisition | Adapt | Level I; preserve live/frozen/delayed identity. |
| `broker/ibkr_historical_ohlc.py` | Acquisition | Adapt | Normalize bars, sessions, adjustment policy, and provenance. |
| `broker/ibkr_persistent.py` | Acquisition | Rewrite | Separate connection state from events and calculations. |
| `broker/ibkr_extensions.py` | Acquisition | Adapt selectively | Depth and references; never imply full consolidated-book coverage. |
| `broker/ibkr_universe_monitor.py` | Acquisition | Reuse pattern | Subscription orchestration only. |
| `features/time_sales.py` | Normalization/calculation | Adapt | Conservative quote test with `UNKNOWN`. |
| `features/market_depth.py` | Normalization/calculation | Adapt | Visible book; displayed changes are not necessarily executions. |
| `features/microstructure.py` | Calculation | Adapt formulas | Add units, windows, quality, and provenance. |
| `features/intraday_windows.py` | Aggregation | Adapt | Deterministic windows with completeness checks. |
| `features/intraday_observables.py` | Calculation | Adapt selectively | Descriptive metrics only. |
| `features/reference_quotes.py` | Acquisition/normalization | Adapt | ES/NQ/VIX remain separately sourced context. |
| `features/baseline.py` | Calculation | Rewrite selectively | Descriptive distributions only; no structural label. |
| `features/depth_context.py` | Inferred observation | Rewrite | Neutral alignment language; no confirmation or causality. |
| `features/tape.py` | Calculation | Discard | Permissive midpoint classification conflicts with uncertainty policy. |
| absorption and liquidity-sweep modules | VR2/VR3 | Do not migrate | Only underlying factual primitives may be reconsidered. |
| impulse, continuation, exhaustion, effort/result, market context | VR2 | Exclude | Structural interpretation is outside VR1. |
| `setups/*`, `signals/*` | VR2/VR3 | Exclude | No setup, prediction, score, confidence, READY, or NO_TRADE. |
| `risk/*`, paper plans, trade plans | VR4 | Exclude | No entry, stop, target, sizing, risk, execution, or paper trading. |
| dashboard/UI modules | Screen | Exclude | Processing publishes contracts only. |

## 5. Observable taxonomy

- `RAW`: reported directly by the provider, subject to normalization.
- `DERIVED`: calculated deterministically from versioned observables.
- `INFERRED`: quantitative observational estimate with explicit uncertainty;
  it is not reported directly by IBKR.

An inferred observable must identify its method, inputs, valid conditions,
unknown/excluded population, classification coverage, and limitations. It must
not assert participant identity, intent, causality, or a provider-reported fact.

## 6. Family capability map

| Family | Equities capability | Processing interpretation |
|---|---|---|
| C1 — Prices | `DIRECT` | Trades, quotes, OHLCV, previous close, WAP, references. |
| C2 — CVD & Order Flow | `PARTIAL / DERIVED` | Trades are raw; aggressor and CVD are estimates with BUY/SELL/UNKNOWN. |
| C3 — Open Interest / Positioning | `PARTIAL` | Shortable shares are indicative availability, not OI, positioning, short interest, or borrow fee. |
| C4 — Flows | `UNSUPPORTED` | Volume/order flow/depth changes are not ETF, fund, or capital flows. |
| C5 — On-Chain / Market State | `NOT APPLICABLE` | Equities data does not supply the BTC on-chain/miner family. |
| C6 — Volatility | `PARTIAL / DERIVED` | Realized volatility, ranges, gaps, and separately sourced VIX; no structural regime. |
| C7 — Liquidations / Stress | `UNSUPPORTED` | No liquidation source; halts, VIX, or liquidity deterioration are not substitutes. |
| C8 — Liquidity Microstructure | `DIRECT + DERIVED + INFERRED` | Level I/II, depth, spread, imbalance, Time & Sales, large activity, persistence, qualified sweeps. |
| C9 — Blockchain Financial Networks | `NOT APPLICABLE` | SSL/IBKR is not a blockchain source and does not change C9. |

Consumer capability states are `COMPLETE`, `PARTIAL`, `UNSUPPORTED`, and
`NOT_APPLICABLE`. They describe data availability, not predictions or model
quality.

## 7. C2 guardrails

- Quote-test aggressor classification is `INFERRED`, not reported by IBKR.
- Missing, stale, crossed, misaligned, outside-market, or ambiguous cases remain
  `UNKNOWN`.
- Estimated CVD publishes buy, sell, unknown and total volume, classification
  coverage, method version, and reset/window policy.
- Unknown volume is never silently assigned to a side.
- A large trade does not identify an institution, whale, intent, or causality.

## 8. C8 guardrails

- Market Depth is the provider-visible subscribed book, not guaranteed total
  consolidated-market liquidity.
- Depth metrics identify levels/range, aggregation, session, venue, completeness,
  and timestamp.
- Insert/update/delete events describe displayed-liquidity changes. Disappearance
  is not automatically cancellation or execution.
- A large visible order is not automatically a whale or institution.
- Persistence does not prove that the same participant maintained an order.
- AMM liquidity is not order-book depth.
- Sweep behavior may describe sequential trades, crossed levels, and book changes,
  but never LONG/SHORT, breakout, rejection, continuation, or exhaustion.
- Cross-observable context does not establish causality.

## 9. IBKR capability and entitlement boundary

| Surface | Proposed use | Dependency / limitation |
|---|---|---|
| Level I | Bid, ask, last, sizes, volume | Entitlement; live/frozen/delayed types remain distinct. |
| Historical OHLC | Daily/intraday bars | Permissions, pacing, session and adjustment policy. |
| Real-time bars | OHLCV/WAP/trade count | Subscription capacity and pacing. |
| Tick-by-tick Last | Time & Sales | Live permissions and simultaneous line limits. |
| Market Depth | Visible Level II book | Depth entitlement, venue coverage, resets, book limits. |
| Shortable shares | Indicative availability | Not borrow fee, locate, short interest, or positioning. |
| ES/NQ/VIX | Reference context | Separate instrument permissions and timestamps. |
| Options OI/Greeks/IV | C3/C6 candidates | Not acquired by evaluated SSL code. |
| Borrow fee/rate | C3 candidate | Not acquired automatically by evaluated SSL code. |

Request code alone is not operational support. Live operation requires
credentials, TWS/Gateway connectivity, subscriptions, permissions, pacing
compliance, and returned-coverage validation.

## 10. Conceptual contract evolution

Existing BTC contracts remain unchanged. Equities should initially use a
parallel, versioned market-aware envelope, not new mandatory BTC fields.

| Field | Purpose |
|---|---|
| `contract_version` | Explicit schema version. |
| `market_class` | `CRYPTO` or `EQUITIES`. |
| `asset_id`, `symbol`, `asset_class` | Canonical and provider identity. |
| `venue`, `provider` | Source/routing context. |
| `family`, `observable_id` | Ownership and stable logical ID. |
| event/received/calculated timestamps | Source, acquisition, and derivation time. |
| `value`, `units` | Typed value and units. |
| `source_status` | Live/frozen/delayed/unavailable state. |
| `data_quality` | Staleness, coverage, completeness, validation facts. |
| `capabilities` | COMPLETE/PARTIAL/UNSUPPORTED/NOT_APPLICABLE. |
| `derivation_type`, `method_version` | RAW/DERIVED/INFERRED and method identity. |
| `provenance` | Source records and input-observable references. |

This is conceptual only, not an operational JSON contract. A later proposal must
define compatibility, required/optional fields, serialization, null semantics,
identifiers, and migration behavior before code or samples are created.

## 11. Compatibility risks and controls

| Risk | Required control |
|---|---|
| Crypto-specific units/labels | Keep Equities builders/envelopes separate until compatibility is proven. |
| Expansion of the frozen counter | Maintain an independent proposal registry. |
| Delayed data treated as live | Preserve IBKR market-data type end-to-end. |
| Session/calendar differences | Make timezone, holidays and extended-hours policy explicit. |
| Corporate actions | Version adjustment policy and retain provider metadata. |
| Quote/trade clock mismatch | Apply skew/staleness limits; unresolved trades are UNKNOWN. |
| Partial/reset depth books | Publish reset/completeness state and suppress invalid metrics. |
| SMART/direct ambiguity | Identify routing scope; never claim a consolidated book without proof. |
| Pacing and line limits | Centralize subscription budgeting and expose degraded capabilities. |
| VR2/VR3 leakage | Enforce this specification's exclusions during review. |

## 12. Implementation gate

No proposed observable becomes executable until a later approved change freezes
its definition and ownership, validates IBKR interfaces/entitlements, approves a
backward-compatible contract, adds replay fixtures and deterministic tests,
demonstrates no change to the 33 BTC endpoints, coordinates Emulator/Integration/
Screen separately, and keeps VR2–VR4 outputs outside VR1.

See [EQUITIES_OBSERVABLE_CATALOG.md](EQUITIES_OBSERVABLE_CATALOG.md).
