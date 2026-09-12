# Research Disposition

This document preserves the evidence that shaped Laconic without allowing an incomplete research programme to stand in for the product roadmap.

## Decision boundary

Laconic now separates two questions:

- **Product:** Can a private, opt-in runtime compress eligible tool observations with exact recovery, fail-open behavior, bounded latency, clear operator controls, and no remote telemetry?
- **Research:** Does that mechanism produce general net token or cost savings without changing agent behavior across a representative population?

The first question governs the OMP beta. The second governs general savings and behavior claims. Neither answer substitutes for the other.

## Disposition ledger

| Workstream | Evidence | Disposition |
| --- | --- | --- |
| V1 prose compression | The mechanism reduced output tokens by 44.6% in its benchmark, but human-facing prose represented 2.30% of measured real-session spend. | Retired as a product direction. Preserve the measurement as evidence that channel selection matters. |
| V2 observation codec | Deterministic encoders and exact ledger recovery are implemented. Tool results represented 63.24% of measured context volume; their repeated residency in cached context was estimated at approximately 38.1% of modeled spend. | Active product core. Integrate it first as an observation-only runtime; do not attribute the residency estimate to first-emission compression or infer realized token or cost savings from channel share. |
| Committed K1 fixture | The fixture reports 8.41% net savings against a pre-registered 15% kill threshold. K2 reports 100% action equivalence, K4 26.8 tokens of overhead, K5 0.0pp difference, and K3 remains manual. | Keep as a deterministic fixture and gate-harness check. It is not a representative real-world benchmark and no longer blocks an opt-in runtime beta. It does not support general savings claims. |
| Laconic Observe | Released content-free receipts, local audit, compatibility reporting, and explicit install/remove/status/report commands for Claude Code and OMP. It never transforms agent-visible results. | Retain as supporting diagnostics and adapter evidence. It is not the primary product and does not satisfy the runtime beta gate. |
| K1 Stage A | A body-free metadata screen admitted 1,063 sessions across 61 project lineages and three providers. | Completed feasibility evidence. Its `proceed_to_stage_b_request` result was not product or spend authorization. |
| K1 Stage B | Produced the frozen manifest and eligibility machinery used by Stage C. | Completed research infrastructure. Preserve it; do not treat it as active product work. |
| K1 Stage C | The final source-mapped replacement cohort selected 24 sessions across 4 self-owned lineages after excluding 10 Retailogists sessions and 3 missing or ambiguous model mappings. All 11 Codex and 13 OMP baselines produced zero replay-engine turns, actions, and observations because the parser accepts Claude-shaped assistant/user tool-use records. Execution stopped before replay client construction. No provider prompt, replay artifact, external annotation, or modeled spend resulted. | Terminally incomplete historical replay path. Do not retry, weaken the gate, infer normalized records, or spend provider budget under its archived plan. Provider-specific normalization requires a separate future research design and explicit authorization. |
| K1 hybrid remeasurement | Available evidence could not support its intended paired claim. | Closed as a product prerequisite. Reopen only under a new research question and evidence contract. |
| External archive manifest | Proposed external-data collection but did not establish an available, authorized source. | Closed. External annotated data is not required for runtime delivery. |
| Prospective snapshot capture | Defined supporting capture infrastructure but could not unblock a runtime that did not exist. | Frozen. A shipped runtime may later generate prospective receipts under its own privacy and qualification contract. |
| Residency management | Decision accounting exists, but no live host applies history rewriting. | Deferred. Admit only after runtime evidence and a host surface justify the cache and correctness risk. |
| Action compression | The core action codec exists, but live edit rewriting has a larger correctness boundary than observation transformation. | Deferred beyond the observation-only beta. |

## Current authorization

The approved product tranche is:

1. align public product authority;
2. build a canonical session runtime and recovery protocol;
3. integrate that runtime into OMP;
4. ~~qualify it through at least 10 completed real OMP sessions across 3 repositories and at least 100 eligible observations~~ — completed, see `docs/runtime-beta-report.md`;
5. ~~prepare the bounded `v0.9.0` beta only when every safety criterion passes~~ — completed; `v0.9.0` is published, with `v0.9.1` correcting a cold-start defect;
6. ~~measure locally, read-only, where model spend goes and what the codec did in those same sessions~~ — completed as a single-arm composition report containing no savings figure.

Item 6 is composition measurement, not savings measurement: every readable session ran with the codec enabled, so the corpus has no counterfactual. M20-v1 separately ran one frozen controlled comparison and ended incomplete with 0 valid cells and null dispersion, correlations, effect, and confirmatory sample size. M20-v2 corrected the operational validity mechanics and then executed once, completing with 24 of 24 valid cells and $0.9975374 of pooled gateway spend. It publishes dispersion, cross-arm cost correlations, mechanism and completion counters, and a confirmatory requirement of 15 tasks at two repetitions. Its arm means and paired effect remain private, so it establishes feasibility and variance, not savings, equivalence, direction, or superiority. Running it, any confirmatory cohort, or any other provider-backed comparison requires fresh explicit owner authorization. The current authorization does not include provider replay spend, external data collection, Claude Code integration, MCP, action rewriting, history compaction, hosted services, or universal savings claims.

## Claims that remain valid

- The measured corpus was dominated by tool-result and tool-argument traffic.
- The existing codec can deterministically reduce selected observation representations while keeping omitted content in a recoverable ledger.
- The committed fixture exercises the replay and gate machinery and reports its own bounded results.
- Observe records content-free local diagnostics without changing what an agent sees.

## Claims that remain unproven

- General token or monetary savings in live sessions.
- Prompt-cache savings from shorter visible results.
- Behavioral equivalence under real runtime use.
- Net benefit after induced expansions or additional agent work.
- Generalization across users, repositories, clients, models, and workloads.

The runtime beta may report observed raw and visible character counts for its own sessions. It must not rename those counts as token, cost, cache, or behavior improvement.
