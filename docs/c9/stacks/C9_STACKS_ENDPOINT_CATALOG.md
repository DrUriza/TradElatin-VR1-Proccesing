# C9/Stacks Proposed Source-Surface Catalog

**Status for every entry:** PROPOSED / NOT IMPLEMENTED  
**Scope:** pre-implementation catalog; no current runtime endpoints or contracts

## Catalog rules

The identifiers below are logical source IDs, not promises of stable external
URLs and not additions to the current Processing endpoint allowlists. During
Milestone 1, each source must be verified against the actual API or contract
interface, mapped to one of the three frozen C9 observables, and assigned explicit
provenance, units, timestamps, finality, and failure behavior.

`C9 ownership` identifies the canonical C9 domain. `Possible consuming C1–C8
families` permits contextual consumption only; it does not transfer ownership or
authorize a consumer to change the source semantics.

## A. C9 CORE / NATIVE

| Logical source ID | Intended source/API/contract class | What it measures | C9 ownership | Possible consuming C1–C8 families | Status | Notes / semantic restrictions |
|---|---|---|---|---|---|---|
| `sbtc_token_supply` | `sbtc-token`; Stacks read-only contract interface; Hiro / Stacks APIs | Observed sBTC token supply plus validated issuance or mint-burn context | C9.1 — sBTC Supply & Peg State | C1, C5 | PROPOSED / NOT IMPLEMENTED | Supply, minted amount, burned amount, and circulating interpretation are distinct. Pin contract/network, decimals, block, and provenance. Peg price is separate context, not supply. |
| `sbtc_bridge_deposits` | Emily public API; `sbtc-registry`; protocol contracts/events | Deposit operations, amounts, counts, states, and timing into sBTC | C9.2 — sBTC Bridge Flow; C9.3 operational state where applicable | C4, C5, C6 | PROPOSED / NOT IMPLEMENTED | Requested/pending/accepted/confirmed/completed deposits are not equivalent. Do not count an unfinalized request as completed bridge inflow. |
| `sbtc_bridge_withdrawals` | Emily public API; `sbtc-registry`; protocol contracts/events | Withdrawal operations, amounts, counts, states, and timing out of sBTC | C9.2 — sBTC Bridge Flow; C9.3 operational state where applicable | C4, C5, C6 | PROPOSED / NOT IMPLEMENTED | Preserve requested, pending, confirmed, completed, rejected, and failed states. Net flow requires an explicit sign convention and common time window. |
| `sbtc_bridge_limits` | Emily public API; `sbtc-registry`; Stacks read-only contract interfaces | Active protocol or service limits affecting bridge operations | C9.3 — sBTC Bridge Operational State | C4, C6 | PROPOSED / NOT IMPLEMENTED | Label protocol limits separately from API, wallet, UI, provider, or policy limits; preserve scope, units, and effective time. |
| `sbtc_bridge_chainstate` | Emily public API; Bitcoin/Stacks chain references; Hiro / Stacks APIs | Chain, block, confirmation, finality, and bridge synchronization context | C9.3 — sBTC Bridge Operational State | C4, C5, C6 | PROPOSED / NOT IMPLEMENTED | Do not infer health from a single height. Record both chains where relevant, source time, lag basis, and reorganization/finality policy. |
| `sbtc_signer_state` | `sbtc-registry`; Emily public API; Stacks read-only interfaces; protocol events | Observable signer/registry state relevant to bridge operation | C9.3 — sBTC Bridge Operational State | C5, C6 | PROPOSED / NOT IMPLEMENTED | Report only publicly observable protocol state. Do not infer signer availability, consensus, security, or custody health without a validated protocol-defined measure. |

## B. C9 TRANSVERSAL

| Logical source ID | Intended source/API/contract class | What it measures | C9 ownership | Possible consuming C1–C8 families | Status | Notes / semantic restrictions |
|---|---|---|---|---|---|---|
| `sbtc_ft_transfers` | Hiro / Stacks APIs; SIP-010/token contract events; protocol-specific contracts/events | sBTC fungible-token transfers with sender, recipient, amount, transaction, block, and event context | C9 transversal transfer surface; contextual input to C9.1/C9.2 only when semantics prove relevance | C2, C5 | PROPOSED / NOT IMPLEMENTED | A transfer is not automatically a trade, bridge flow, mint, burn, or whale action. Classify only with validated contract/event semantics. |
| `sbtc_holder_distribution` | Hiro / Stacks APIs; token balances/read-only interfaces; indexed ledger state | Distribution of observed sBTC balances across addresses at a defined snapshot | C9 transversal ownership-distribution surface; contextual input to C9.1 | C5 | PROPOSED / NOT IMPLEMENTED | Addresses are not persons or entities. Exclude or label contracts, custodial aggregation, dust, inactive balances, and incomplete indexing where identifiable. |
| `sbtc_dex_trades` | Protocol-specific DEX contracts/events; Hiro / Stacks APIs | Validated swaps/trades involving sBTC, including venue, pair, amounts, and execution context | C9 transversal market-activity surface | C1, C2, C8 | PROPOSED / NOT IMPLEMENTED | Only validated swap/trade events qualify. Preserve venue/pool/pair and token direction; do not claim aggressor side or CVD semantics unless derivable and validated. |
| `sbtc_amm_pool_state` | AMM read-only contract interfaces; pool contracts/events; Hiro / Stacks APIs | Pool reserves, liquidity, price state, fees, and invariant-related context for sBTC pools | C9 transversal liquidity surface | C1, C6, C8 | PROPOSED / NOT IMPLEMENTED | AMM reserves and curve liquidity are not order-book bid/ask depth. Derived price impact must identify pool model, fee tier, path, size, block, and assumptions. |
| `sbtc_lending_market_state` | Protocol-specific lending contracts/read-only interfaces/events | sBTC lending supply, borrow, utilization, collateral, and rate state where exposed | C9 transversal DeFi-credit surface | C3, C5, C6 | PROPOSED / NOT IMPLEMENTED | Do not label lending supply/borrow as derivatives Open Interest, and do not label protocol rates as perpetual Funding Rate. Preserve protocol-specific definitions. |
| `sbtc_protocol_liquidations` | Protocol-specific lending/liquidation contracts and validated events | Confirmed protocol liquidation events involving sBTC | C9 transversal liquidation surface | C7, C5, C6 | PROPOSED / NOT IMPLEMENTED | Count only events whose protocol semantics are verified. Do not mix lending liquidations with centralized/perpetual liquidation maps or pending risk positions. |
| `stacks_fee_state` | Hiro / Stacks APIs; Stacks node/mempool/transaction data | Observed transaction fee levels and fee context affecting Stacks/sBTC operations | C9 transversal network-operational surface; contextual input to C9.3 | C6 | PROPOSED / NOT IMPLEMENTED | Distinguish paid fees, estimates, percentiles, and protocol-specific fees. Units, sampling window, transaction class, and freshness are mandatory. |
| `stacks_mempool_activity` | Hiro / Stacks APIs; Stacks node mempool interfaces | Pending transaction volume/activity and congestion context relevant to operational timing | C9 transversal network-operational surface; contextual input to C9.3 | C6 | PROPOSED / NOT IMPLEMENTED | Mempool data is ephemeral and provider-dependent. Pending does not mean confirmed; deduplicate transactions and expose snapshot coverage and staleness. |

## Frozen observable mapping

| Frozen observable | Primary native source surfaces | Optional transversal context |
|---|---|---|
| C9.1 — sBTC Supply & Peg State | `sbtc_token_supply` | `sbtc_ft_transfers`, `sbtc_holder_distribution`, validated `sbtc_dex_trades` / `sbtc_amm_pool_state` for separately sourced peg context |
| C9.2 — sBTC Bridge Flow | `sbtc_bridge_deposits`, `sbtc_bridge_withdrawals` | `sbtc_ft_transfers` only when contract/event semantics prove bridge relevance |
| C9.3 — sBTC Bridge Operational State | `sbtc_bridge_limits`, `sbtc_bridge_chainstate`, `sbtc_signer_state`, operational fields from deposit/withdrawal sources | `stacks_fee_state`, `stacks_mempool_activity` |

## Cross-family relationship summary

- **C1 ←** price/peg/DEX context.
- **C2 ←** transfers/swaps where order-flow semantics are valid.
- **C3 ←** lending/rates context, without calling it OI/Funding.
- **C4 ←** bridge deposits/withdrawals.
- **C5 ←** supply/transfers/holders/registry/signer state.
- **C6 ←** bridge operational stress/fees/network context.
- **C7 ←** protocol liquidations when validated.
- **C8 ←** DEX/AMM liquidity, without confusing AMM liquidity with order-book depth.

## Implementation gate

Before any logical source ID can become executable, Milestone 1 must validate the
real source interface, access conditions, field semantics, unit conversions,
timestamps, pagination, finality, quality/error states, and the destination C9
contract version. Until then, every catalog entry remains **PROPOSED / NOT
IMPLEMENTED** and is excluded from the current Processing runtime inventory.
