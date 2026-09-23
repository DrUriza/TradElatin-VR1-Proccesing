# C9/Stacks Processing Technical Specification

**Status:** PRE-IMPLEMENTATION — PROPOSED / NOT IMPLEMENTED  
**Scope:** TradELATIN VR1 Processing  
**Target work:** Stacks Getting Started Grant, Milestone 1

## 1. Purpose and boundary

This document freezes the intended Processing architecture and semantic boundary
for C9/Stacks before implementation. It is a design specification, not evidence
of a prototype or live Stacks support.

No Stacks adapter, Stacks acquisition path, C9 normalizer, C9 processor, C9
contract builder, runtime registration, endpoint allowlist entry, or sample C9
contract exists in the current repository. There is no operational Stacks support.
Those items belong to proposed grant Milestone 1. The implemented C1–C8 runtime
and its inventory remain unchanged.

## 2. Future Processing architecture

```text
Stacks/sBTC
    → Acquisition Adapter
    → Raw Validation
    → Normalization
    → C9 Processing
    → Versioned C9 Contracts
    → Financial Observables
    → Screen/HMI
```

The intended responsibilities are:

1. **Stacks/sBTC:** authoritative protocol, contract, event, ledger, and API
   surfaces from which facts may be observed.
2. **Acquisition Adapter:** retrieves source-specific payloads without assigning
   financial meaning beyond source identity and retrieval metadata.
3. **Raw Validation:** checks schema shape, identifiers, units, timestamps,
   pagination/completeness markers, block/transaction references, and source
   errors before any computation.
4. **Normalization:** converts validated payloads into canonical types, units,
   timestamps, directions, statuses, and provenance while retaining source
   references and finality context.
5. **C9 Processing:** derives only the three frozen C9 observables using explicit,
   versioned definitions and quality flags.
6. **Versioned C9 Contracts:** publishes stable machine-readable outputs with
   schema version, observation time, source time, provenance, completeness,
   confidence/quality state, and calculation metadata.
7. **Financial Observables:** exposes descriptive state for VR1 — OBSERVE; it does
   not predict prices, issue trading signals, or execute actions.
8. **Screen/HMI:** consumes contracts and renders their state without silently
   recomputing or changing C9 semantics.

## 3. Ownership rule

C9 owns the Stacks/sBTC data, while C1–C8 can consume C9 observables for cross-family contextual analysis.

Ownership means that C9 defines source provenance, normalization, semantics,
quality, and the canonical contract for Stacks/sBTC observations. A consuming
family may contextualize its own view but must not duplicate, rename, or alter
the meaning of the C9 source surface.

## 4. Frozen C9 observables

Only the following three observables are frozen for the first implementation.

### C9.1 — sBTC Supply & Peg State

**Definition boundary:** supply / issuance / mint-burn context.

This observable is intended to describe sBTC supply state and its protocol-backed
issuance context. Supply values must identify units, decimals, observation block,
contract identity, and provenance. Mint/burn or issuance context may only be
reported when the underlying event or contract semantics are verified. Price or
peg context may be attached as contextual evidence, but it must not redefine
token supply and must retain its own source and timestamp.

### C9.2 — sBTC Bridge Flow

**Definition boundary:** deposits / withdrawals / net bridge flow.

This observable is intended to report validated bridge deposits and withdrawals
over an explicit window, with `net bridge flow` defined using a documented sign
convention. Requested, pending, accepted, confirmed, completed, rejected, and
failed operations must not be aggregated as if they were equivalent. Counts and
amounts must remain distinguishable; revisions caused by finality or state
transitions must be traceable.

### C9.3 — sBTC Bridge Operational State

**Definition boundary:** operation status / pending state / timing / fees / limits /
operational context.

This observable is intended to describe bridge availability and operating
conditions. It may include validated operation states, pending queues, elapsed
or estimated timing, fee state, protocol limits, signer/registry context, and
chain state. It must not convert missing data into a healthy state, and it must
distinguish protocol limits from UI/provider limits and observed timing from an
estimate or service-level claim.

## 5. Proposed source classes

The implementation may evaluate these proposed sources, subject to interface,
license/access, stability, and semantic validation:

- `sbtc-token`
- `sbtc-registry`
- Emily public API
- Stacks read-only contract interfaces
- Hiro / Stacks APIs
- protocol-specific contracts/events for transversal extensions

No source listed here is currently wired into Processing. Source precedence,
fallback behavior, pagination, historical availability, rate limits, and finality
rules must be established during implementation and recorded in the resulting
contract metadata.

## 6. Cross-family consumption map

| Consumer | Permitted C9 context | Semantic restriction |
|---|---|---|
| C1 | price/peg/DEX context | C9 supply and bridge facts remain C9-owned; market price must retain venue/source identity. |
| C2 | transfers/swaps where order-flow semantics are valid | A transfer is not automatically a trade; a swap is not automatically signed order flow. |
| C3 | lending/rates context | Do not call protocol lending/rate state Open Interest or Funding. |
| C4 | bridge deposits/withdrawals | Preserve operation status and direction; do not equate pending requests with completed flows. |
| C5 | supply/transfers/holders/registry/signer state | Preserve contract, registry, signer, block, and provenance context. |
| C6 | bridge operational stress/fees/network context | Descriptive operational context is not a volatility forecast. |
| C7 | protocol liquidations when validated | Only protocol-defined, validated liquidation events qualify. |
| C8 | DEX/AMM liquidity | Do not confuse AMM reserves/liquidity with order-book depth. |

Cross-family enrichment must reference the originating C9 contract version and
observation timestamp. Consumers must tolerate C9 being absent because C9 is not
part of the current runtime. Cross-family context does not establish causality.

## 7. Validation and provenance requirements

A future C9 pipeline must, at minimum:

- pin contract identifiers and network/environment;
- preserve transaction, event, block, and API provenance when available;
- record source time separately from acquisition and processing time;
- normalize token decimals and amount units explicitly;
- distinguish observed, derived, estimated, and unavailable fields;
- represent pending/final/failed/reverted states without collapsing them;
- detect incomplete pagination, stale data, duplicates, and conflicting sources;
- define reorganization/finality handling before claiming finalized flows;
- version schemas and formulas; and
- fail closed to `unavailable` or a qualified state rather than fabricate values.

## 8. Explicit non-goals for this change

This documentation change does not:

- create C9 Python code;
- add Stacks to `acquisition.py`;
- modify endpoint allowlists or runtime family registration;
- create sample contracts or fixtures;
- claim a Stacks prototype;
- claim live Stacks support; or
- perform Milestone 1 in advance.

This pre-implementation specification freezes logical source-surface identifiers
and semantic ownership only. It does not freeze URLs, HTTP methods, parameters,
payload schemas, pagination, exact contract addresses, or production provider
assignments; those details must be validated during Milestone 1.

The detailed proposed source-surface inventory is frozen separately in
[C9_STACKS_ENDPOINT_CATALOG.md](C9_STACKS_ENDPOINT_CATALOG.md).
