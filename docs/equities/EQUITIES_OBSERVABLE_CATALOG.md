# Proposed Equities Observable Catalog

**Status for every entry:** PROPOSED EQUITIES OBSERVABLE — NOT IMPLEMENTED  
**Scope:** TradELATIN VR1 Processing  
**Evaluated source:** SSL Market / IBKR

## 1. Catalog rules

These are proposed logical observable IDs, not active endpoints, runtime
registrations, contract fields, allowlist entries, or additions to the frozen
**33 BTC/CRYPTO logical endpoints across C1–C8**.

The catalog does not freeze request parameters, venues, subscriptions, production
symbols, URLs, pacing, payload schemas, or provider assignments. Availability
depends on connection, entitlement, data type, instrument, venue, session, pacing,
and completeness. Cross-family use does not transfer ownership or establish
causality.

## 2. C1 — Prices

| Logical observable ID | Type | SSL/IBKR source | Measurement | Availability | Restrictions |
|---|---|---|---|---|---|
| `eq_c1_trade_price` | RAW | Tick-by-tick Last; Level I last | Execution/last price | DIRECT | Preserve source granularity. |
| `eq_c1_best_quote` | RAW | Level I bid/ask | Reported best bid and ask | DIRECT | Preserve venue/routing, data type, staleness, crossed state. |
| `eq_c1_ohlcv_bar` | RAW | Historical; real-time bars | Provider OHLCV by interval/session | DIRECT | Declare bar size, session, adjustment policy, volume units. |
| `eq_c1_previous_close` | RAW | Historical/Level I | Provider previous close | DIRECT | Declare session and adjustment basis. |
| `eq_c1_wap` | RAW | Real-time bars | Provider weighted average price for bar | DIRECT | Do not relabel as session VWAP without matching semantics. |
| `eq_c1_reference_quote` | RAW | ES/NQ/VIX requests | Separate reference-instrument quote | PARTIAL | Preserve instrument, asset class, timestamp, source; context is not causality. |
| `eq_c1_midpoint` | DERIVED | Valid best quote | Quote midpoint | DERIVED | Suppress for missing, stale, invalid, or crossed quotes. |
| `eq_c1_return` | DERIVED | Versioned price series | Return over explicit window | DERIVED | Publish price basis, window, session boundary, missing-data policy. |

## 3. C2 — CVD & Order Flow

| Logical observable ID | Type | SSL/IBKR source | Measurement | Availability | Restrictions |
|---|---|---|---|---|---|
| `eq_c2_trade_size` | RAW | Tick-by-tick Last | Reported executed size | PARTIAL | Coverage, units, and exposed conditions must be retained. |
| `eq_c2_trade_count` | RAW | Real-time bars | Provider trade count per bar | PARTIAL | A bar count cannot reconstruct trade sequence. |
| `eq_c2_volume_rate` | DERIVED | Trades/bars | Executed volume per time | DERIVED | Declare granularity, window, session, missing-event policy. |
| `eq_c2_trade_rate` | DERIVED | Trade events/counts | Trades per time | DERIVED | Do not mix bar and tick counts without a method. |
| `eq_c2_estimated_aggressor_volume` | INFERRED | Time & Sales + quotes | Estimated BUY/SELL/UNKNOWN volume | PARTIAL | Quote test only; preserve unknown volume and skew rules. |
| `eq_c2_estimated_cvd` | INFERRED | Estimated aggressor volume | Classified buy minus sell cumulative volume | PARTIAL | Label estimated; publish coverage, unknown volume, method/window/reset. |
| `eq_c2_classification_coverage` | DERIVED | Classification results | Fraction classified BUY/SELL | DERIVED | Quality fact, not predictive confidence. |
| `eq_c2_large_trade_activity` | DERIVED | Trade-size distribution | Trades above versioned threshold | PARTIAL | No institution, whale, or intent claim. |

## 4. C3 — Open Interest / Positioning

| Logical observable ID | Type | SSL/IBKR source | Measurement | Availability | Restrictions |
|---|---|---|---|---|---|
| `eq_c3_shortable_shares` | RAW | Generic tick 236 | Indicative shares available to short | PARTIAL | Not OI, short interest, positioning, borrow fee, confirmed locate, or guaranteed availability. |

The evaluated SSL code does not acquire options OI, put/call, Greeks, IV
surfaces, real borrow fees, or consolidated positioning.

## 5. Unsupported or non-applicable families

| Family | Capability | Reason |
|---|---|---|
| C4 — Flows | UNSUPPORTED | Volume, signed volume, and depth changes are not ETF/fund/capital flows. |
| C5 — On-Chain / Market State | NOT_APPLICABLE | Equities data is not the BTC on-chain/miner domain. |
| C7 — Liquidations / Stress | UNSUPPORTED | No liquidation feed; halts, VIX, spread, and depth withdrawal are not liquidations. |
| C9 — Blockchain Financial Networks | NOT_APPLICABLE | SSL/IBKR is not a blockchain source and does not change C9. |

No observable IDs are assigned to these families from the evaluated SSL surfaces.

## 6. C6 — Volatility

| Logical observable ID | Type | SSL/IBKR source | Measurement | Availability | Restrictions |
|---|---|---|---|---|---|
| `eq_c6_realized_volatility` | DERIVED | Trade/OHLC returns | Realized variability by estimator/window | DERIVED | Publish sampling, estimator, annualization, session, gap policy; no regime. |
| `eq_c6_intraday_range` | DERIVED | Intraday OHLC | High-low range in price/bps | DERIVED | Declare interval, session, missing bars, price basis. |
| `eq_c6_gap` | DERIVED | Previous close + open/current | Session gap in price/bps | DERIVED | Adjustment policy and session boundary required. |
| `eq_c6_vix_reference` | RAW | VIX market data | Observed VIX reference | PARTIAL | Separate instrument/context, not selected-equity realized volatility. |

Volatility regime, impulse, continuation, exhaustion, breakout, rejection, and
market-state interpretation are excluded.

## 7. C8 — Liquidity Microstructure

| Logical observable ID | Type | SSL/IBKR source | Measurement | Availability | Restrictions |
|---|---|---|---|---|---|
| `eq_c8_top_of_book_size` | RAW | Level I sizes | Displayed best bid/ask size | DIRECT | Venue/routing and units required. |
| `eq_c8_depth_level` | RAW | Depth callbacks | Price, size, side, position/venue where exposed | PARTIAL | Visible book only; handle operations, resets, sequence integrity. |
| `eq_c8_spread` | DERIVED | Valid best quote | Absolute/bps bid-ask spread | DERIVED | Suppress stale, crossed, missing, or mismatched scope. |
| `eq_c8_top_imbalance` | DERIVED | Top sizes | Top-of-book imbalance | DERIVED | Define zero/missing behavior and formula version. |
| `eq_c8_microprice` | DERIVED | Best prices/sizes | Size-weighted top price estimate | DERIVED | Descriptive only, not forecast/fair value. |
| `eq_c8_visible_bid_depth` | DERIVED | Rebuilt bid book | Displayed bid depth by scope | PARTIAL | Publish levels/range, completeness, venue, timestamp. |
| `eq_c8_visible_ask_depth` | DERIVED | Rebuilt ask book | Displayed ask depth by scope | PARTIAL | Same restrictions as bid depth. |
| `eq_c8_depth_imbalance` | DERIVED | Bid/ask depth | Relative visible-depth imbalance | PARTIAL | Matched scopes and complete-enough snapshot required. |
| `eq_c8_depth_concentration` | DERIVED | Depth levels | Concentration across prices/levels | PARTIAL | Formula, scope, venue, reset state required. |
| `eq_c8_displayed_liquidity_change` | DERIVED | Depth history | Added/removed displayed size | PARTIAL | Removal does not identify cancellation, execution, replacement, or correction. |
| `eq_c8_large_visible_order` | DERIVED | Depth-size distribution | Visible size above threshold | PARTIAL | No participant identity, intent, or guaranteed executable liquidity. |
| `eq_c8_visible_liquidity_persistence` | DERIVED | Depth history | Duration/recurrence of large visible liquidity | PARTIAL | Does not prove one participant; identity/replacement rules required. |
| `eq_c8_time_sales` | RAW | Tick-by-tick Last | Normalized observed execution sequence | PARTIAL | Preserve feed coverage, conditions where exposed, event/receipt time. |
| `eq_c8_large_liquidity_activity` | INFERRED | Large trades + depth changes | Qualified concurrence of large executed/displayed activity | PARTIAL | No institution, whale, intent, causality, absorption, or setup claim. |
| `eq_c8_observable_sweep_behavior` | INFERRED | Trades, quotes, depth | Rapid executions across levels with book changes | PARTIAL | No LONG/SHORT, breakout, rejection, continuation, exhaustion, or advice. |
| `eq_c8_depth_tape_alignment` | INFERRED | Depth + classified tape | Quantitative alignment/conflict | PARTIAL | Tape side remains estimated; alignment is not confirmation/causality. |

## 8. Existing inputs versus additional acquisition

Calculable from evaluated SSL surfaces: midpoint, returns, spread, microprice,
depth totals/imbalance/concentration, displayed changes/persistence, large visible
orders/trades, estimated aggression/CVD/coverage, realized volatility/ranges/gaps,
and qualified large-liquidity/sweep observations.

Additional acquisition and approval are required for options chains/OI/Greeks/IV,
put/call metrics, borrow fee/rebate, confirmed locates, consolidated short
interest, ETF creations/redemptions, capital flows, full consolidated depth where
not supplied, participant identity, and C7 liquidation data.

## 9. Approval gate

Before promotion, each ID requires definition/ownership approval, source and
entitlement validation, units/windows/session behavior, method version, replay
fixtures and deterministic tests, versioned contract mapping, BTC compatibility
evidence, and coordinated capability mapping for Emulator, Integration, and
Screen.

Until then every ID remains **PROPOSED EQUITIES OBSERVABLE — NOT IMPLEMENTED**
and excluded from the current runtime.
