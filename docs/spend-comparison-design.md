# Controlled Spend Comparison — Design

**Status: M20-v1 is immutable incomplete evidence; M20-v2 is an unexecuted candidate protocol.** The generated [M20-v1 public disposition](results/controlled-spend-pilot.md) is canonical under manifest `a76c6cb0d2f34737ccd629398b0b2122a3c0a74c63a77055f8112cea602f7b44`. It contains 0 valid cells, 3 attempted protocol-invalid cells, 21 unrun cells, $0.2289047 pooled gateway spend, and null dispersion, correlations, and confirmatory task count. M20-v1 may not be resumed, tuned, reinterpreted, or pooled into M20-v2. M20-v2 remains `execution_authorized: false`; no provider request, confirmatory run, or savings claim is authorized.

## 1. Why the existing measurement cannot answer the question

`laconic research spend report` measures composition: of the money real sessions actually cost, how much went to uncached input, cache reads, cache writes, and output, and what the codec did in those same sessions.

It cannot measure savings, and no refinement of it can. Every session it reads ran with the codec enabled. There is no observation anywhere in that data of the same work done without the codec, so there is no quantity to subtract. This is not a precision problem that more sessions would fix; it is the absence of a comparison arm. The only bound on a general savings claim remains the committed K1 fixture's 8.53%, and that fixture is a deterministic gate-harness check, not representative evidence.

A comparison therefore requires generating new data under a design that produces all three controlled arms. That is the subject of this document.

## 2. The unit of work

**Decision: the unit is a task, not a session.**

A session is unusable as a unit. Sessions vary in length by three orders of magnitude, span different repositories and different kinds of work, and — as the composition report shows — a session can acquire a runtime ledger partway through and be "codec-on" for a small fraction of its turns. Two sessions are never the same work.

A task is a bounded unit of agent work with a written statement, a fixed repository at a fixed commit, and an observable completion condition. It is the smallest unit that can plausibly be repeated.

A task must satisfy all of:

- a written prompt that is reused verbatim across arms;
- a repository pinned to one commit, restored to that commit before each run;
- a completion condition checkable without a judge (a command exits zero, a named file exists, a named test passes);
- a bounded step budget, so a runaway arm terminates rather than dominating the cost distribution;
- no network dependency whose latency or availability differs between runs.

Tasks that cannot meet all five are excluded before any arm runs, not after results are visible.

## 3. Arms and pairing

**Decision: three arms, paired repetition of the same task, randomized within each task/repetition block.**

The pilot arms are:

1. **native OMP** — no Laconic extension and no Headroom transformation;
2. **Laconic** — the shipped OMP tool-result codec, including every induced `laconic_expand` call and extra turn;
3. **Headroom 0.37.0** — OMP routed through Headroom's shipped coding profile, with its anonymous beacon disabled.

The primary statistical contrast is Laconic versus native OMP. The Headroom arm answers the public architecture comparison's adjacent-product question, but this pilot does not power or claim Laconic-versus-Headroom superiority. Headroom completion and mechanism evidence, cost correlation, and pilot dispersion may be reported under the same effect-suppression rule as the primary contrast.

*Per-session alternation* is rejected because ordinary sessions do different work. *Between-task arms* are rejected because task variance would be confounded with treatment. Every arm instead starts from the same task tree and receives the same prompt, model, thinking level, tools, request limit, wall-clock limit, and completion oracle.

M20-v1 froze four deterministic tasks and two repetitions per task/arm: 24 maximum task runs. Its task list, source digests, prompts, completion oracles, arm orders, metric, estimator, action threshold, limits, privacy fields, and stopping rules remain immutable. M20-v2 keeps that population and analysis contract but uses distinct prompt paths, a new deterministic arm-order seed, a separate manifest and private root, and only the mechanical amendments in §10. Adding, dropping, restarting, tuning, or importing a v1 observation after any paid result invalidates v2.

This pilot freeze is distinct from a confirmatory pre-registration. The confirmatory sample cannot be fixed until the pilot supplies paired dispersion. A later confirmatory manifest must fix its sample and spend cap before its first arm and requires a new explicit owner authorization.

## 4. The metric

**Decision: total OMP-modelled cost per completed task run, in USD. Provider-reported token components remain private by arm; any public component totals are pooled across all arms and carry no arm label.**

Cost, not undifferentiated tokens, is the decision metric because uncached input, five-minute cache writes, one-hour cache writes, cache reads, and output have different prices. The codec may reduce one class while increasing another. Per-arm component totals stay in private analysis state because they sum to the suppressed arm cost and would reconstruct the pilot effect.

M19 originally selected `laconic.costs`. H-98 proved that choice is invalid for this pilot: its Claude Sonnet 5 entry is stale, and OMP transcripts do not expose the per-turn one-hour cache-write token split needed to reproduce the host's charge. The pilot therefore pins OMP 18.1.14 and its `anthropic/claude-sonnet-5` catalog entry, and treats each turn's OMP `usage.cost` object as the primary modelled-cost record. The catalog snapshot must agree with [Anthropic's published rates](https://platform.claude.com/docs/en/about-claude/pricing) at freeze time: $2/MTok base input, $2.50/MTok five-minute cache write, $4/MTok one-hour cache write, $0.20/MTok cache read, and $10/MTok output. A mismatch stops the pilot before a paid call.

Providers report token counters, not dollars; OMP applies its catalog. The report states that provenance and never labels OMP-modelled cost as an invoice. `laconic.costs` is not changed by this study because repricing it would rewrite historical K1 and M19 figures.

The shared provider gateway separately computes a conservative spend reservation before forwarding each request. That reservation enforces the hard cap; it is not substituted for OMP cost in the analysis. Secondary fields are wall-clock duration, assistant-turn count, provider-request count, completion, and mechanism-fired counters.

## 5. Induced expansion

**Decision: induced expansion is charged to the Laconic arm in full as ordinary spend; only content-free expansion counts may be reported separately.**

When the codec replaces a result, the agent may call `laconic_expand` to recover it. Every token of that call — the request, the recovered content entering the context, and every subsequent turn that re-reads it from the cached prefix — is spend the native OMP primary comparator does not incur. It is not overhead to be netted out or excused; it is the cost of the mechanism.

Charging it in full is automatic if the metric is the arm's total cost, which is why the metric is defined that way rather than as a sum over compressed observations. The separate report exists for diagnosis, not for adjustment: an arm that wins only after its expansions are excluded has not won.

The same rule covers induced *work*: extra turns the agent takes because it saw a reference instead of content. Those turns are in the arm's total by construction. A design that measured only the compressed observations would miss them entirely, which is the central reason the unit is a whole task run.

## 6. Cache-write amortization

**Decision: compare whole-task totals and make no per-compression cost attribution.**

The codec changes the prompt prefix. A cached prefix can be billed above base input to write and below base input to read, so a change may pay a new write and lose later cheap reads. The charge is displaced in time, order-dependent, and controlled by provider cache behavior that is visible only through aggregate counters.

The first study therefore compares the whole cost accumulated while completing a task. Cache component totals are descriptive diagnostics only. No report attributes a cache-write or cache-read delta to an individual Laconic encoding, expansion, or Headroom transformation. A system that wins only after one of its induced cost classes is excluded has not won.

## 7. Effect size, pilot output, and confirmatory sample

**Decision: 10% is the smallest total-cost reduction worth acting on; the pilot exposes dispersion, not effect.**

The owner fixed 10% before any pilot result. For the primary contrast, the confirmatory estimand is the paired per-task log-cost difference:

`d(t) = log(mean_laconic(t)) - log(mean_native(t))`

The log scale maps the 10% product threshold directly and prevents one expensive task from outweighing many cheaper tasks. Headroom uses a separately labelled paired log-cost difference and is not part of the primary power calculation.

The required sequence for M20-v2 is:

1. Commit a distinct, execution-disabled manifest before any credential probe or provider call.
2. Run the frozen four-task, two-repeat, three-arm candidate only after the owner explicitly authorizes the exact manifest hash and maximum reserved-spend calculation.
3. Require all 24 cells to be valid. Any unrun cell, task-completion failure, protocol failure, mechanism non-engagement, state drift, spend-cap refusal, or unparseable usage yields an incomplete disposition and no statistical output.
4. Keep arm means, absolute differences, paired effect estimates, and per-arm cost components in private analysis state. Publish only the sample standard deviation of paired task differences, cross-arm correlations, truthful completeness/failure counters, frozen parameters, and sample-feasibility output.
5. Compute a preliminary task count for the primary Laconic/native confirmatory study at two-sided alpha 0.05 and power 0.80, using the frozen 10% log threshold and `SD(d)`, only after all 24 cells are valid. The calculation assumes two repetitions per task, uses the normal approximation, rounds up, and applies a two-task minimum.
6. If the required sample or projected spend is operationally infeasible, stop. If feasible, commit a separate confirmatory manifest and obtain new explicit spend authorization before any confirmatory arm.

Neither variance pilot is a performance result. No v1 or v2 point estimate may be reported, quoted, used to select tasks, or used to change the 10% threshold.

## 8. Falsification and stopping

The later confirmatory hypothesis is: *for tasks from the pre-registered workload, Laconic reduces total OMP-modelled cost per completed task by at least the product-relevant threshold without reducing completion.*

Neither variance pilot tests that hypothesis. M20-v2 stops without statistics if a task tree or prompt drifts; the deterministic runner-side diagnosis does not observe the frozen failing baseline; an arm does not use the pinned model/configuration; provider usage is missing or malformed; the gateway cannot reserve spend before forwarding; the 16-request per-cell ceiling, 180-second wall limit, or $10 total cap is reached; completion fails; a Laconic or Headroom mechanism does not engage; live OMP or dogfood state changes; credential cleanup fails; or private data reaches a public artifact.

A future confirmatory result falsifies the product-relevant savings claim for this workload if the pre-registered interval favors native OMP or lies wholly inside the ±10% equivalence bounds, or if Laconic completes fewer tasks than the frozen tolerance permits. A null or negative result does not terminate the runtime product: its beta gate is safety, not savings.

## 9. M20-v1 historical disposition

M20-v1 ran once under H-99 and stopped under its frozen invalid-cell rule. All three attempted task oracles passed and all three mechanisms engaged. Each attempted model changed the literal first command, and one attempted a ninth provider request after eight successful requests. Those operational failures still invalidate all three cells under v1. The public v1 schema historically represents the 21 unrun cells as completion failures and the 3 attempted invalid cells as mechanism failures; that frozen wording is preserved for byte compatibility and must not be treated as the corrected taxonomy.

The canonical v1 manifest hash is `a76c6cb0d2f34737ccd629398b0b2122a3c0a74c63a77055f8112cea602f7b44`. Its public JSON and Markdown, fixture prompts, source digests, private state, and null statistics are immutable. M20-v1 observations cannot be reused in M20-v2 or any effect, dispersion, correlation, or confirmatory-sample calculation.

## 10. M20-v2 mechanical amendment

The owner approved these changes before the v2 manifest was committed:

- execute `python3 diagnose.py` in the runner before OMP starts and require the frozen failing baseline;
- exclude that deterministic diagnosis from provider usage and task-cost accounting;
- remove the literal first-command instruction from distinct v2 prompts and treat subsequent agent tool choice as measured behavior;
- increase only the per-cell provider-request ceiling from 8 to 16, retaining the 180-second wall limit and conservative cumulative reservation under the unchanged $10 campaign cap;
- publish distinct counters for attempted cells, valid cells, unrun cells, task-completion failures, protocol failures, and mechanism non-engagement;
- use a new schema version, campaign identifier, deterministic arm-order seed, exact public keys, prompt paths and digests, stopping rules, and private root;
- fail closed with `execution_authorized: false` until the owner separately authorizes provider execution against the exact committed v2 hash.

The unchanged contract is the four task seeds and completion oracles, OMP 18.1.14 package pin, `anthropic/claude-sonnet-5`, Headroom 0.37.0 coding profile, two repetitions, official price snapshot, 10% action threshold, alpha 0.05, power 0.80, privacy boundary, 180-second wall limit, and $10 total campaign cap.

V2 counters form a disjoint disposition. `attempted_cells + unrun_cells = 24`. Among attempted cells, protocol failure takes precedence when any diagnosis, provider/model/usage, request/time/cap, fixture, live-state, or cleanup invariant fails; task-completion failure applies only after protocol validity; mechanism non-engagement applies only after protocol and completion validity; otherwise the cell is valid. Therefore `valid_cells + task_completion_failures + protocol_failures + mechanism_non_engagement = attempted_cells`. Statistics remain null unless `attempted_cells = valid_cells = 24` and every failure counter is zero.

Semantic first-command validation was rejected because shell qualification, wrappers, quoting, and command chaining would leave validity model-controlled. A fixed per-cell dollar reservation was rejected because it duplicates the cumulative gateway, arbitrarily allocates `$10 / 24`, can strand unused budget, and can censor cells by request shape.

## 11. Authorization

This document authorizes the no-provider readiness stack: tracked protocol changes, explicit manifest routing, v2-only prompts and manifest, deterministic runner checks, fake-upstream gateway verification, synthetic analysis/privacy checks, mutation tests, reviews, commits, pushes, and open dependent PRs.

It does not authorize a model credential probe, provider request, campaign execution, merge, M20-v1 or private-root mutation, reuse of v1 observations, public arm costs or effects, a token/cost savings claim, a Laconic-versus-Headroom superiority claim, `laconic.costs` repricing, version bump, release preparation, tagging, PyPI upload, or GitHub Release. A paid M20-v2 run requires a new explicit owner instruction after review of the exact committed v2 manifest hash and maximum reserved-spend calculation.
