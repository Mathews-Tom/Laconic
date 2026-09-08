# Controlled Spend Comparison — Design

**Status: owner-authorized variance pilot; implementation and paid execution pending.** H-99 records the M19 human review sign-off, the fresh instruction to implement and run one bounded pilot, and the owner's fixed choices: a $10 total provider-spend cap, Claude Sonnet 5, and a 10% smallest effect worth acting on. This authorization does not extend to a confirmatory run or a savings claim.

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

The variance pilot freezes four deterministic tasks and two repetitions per task/arm: 24 maximum task runs. All three arm orders within each `(task, repetition)` block come from one committed random seed. The task list, source digests, prompts, completion oracles, arm orders, metric, estimator, action threshold, limits, privacy fields, and stopping rules are committed before the credential probe or first paid arm. Adding, dropping, restarting, or tuning a cell after any paid result invalidates the pilot.

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

The required sequence is:

1. Run the frozen four-task, two-repeat, three-arm pilot.
2. Require all 24 cells to complete and pass their oracles. Any missing cell, differential completion, mechanism failure, state drift, spend-cap refusal, or unparseable usage yields an incomplete disposition and no variance output.
3. Keep arm means, absolute differences, paired effect estimates, and per-arm cost components in private analysis state. Publish only the standard deviation of the paired task differences, the cross-arm cost correlation, completeness/mechanism counters, frozen parameters, and the sample-feasibility output.
4. Compute a preliminary task count for the primary Laconic/native confirmatory study at two-sided alpha 0.05 and power 0.80, using the frozen 10% log threshold and the pilot `SD(d)`. The calculation assumes the confirmatory study keeps the pilot's two repetitions per task; changing *k* requires a new variance model rather than reusing this task count. State the approximation and round up.
5. If the required sample or projected spend is operationally infeasible, stop. If feasible, write and commit a new confirmatory manifest and obtain a new explicit spend authorization before any confirmatory arm.

The pilot is not a performance result. Its point estimate is not reported, quoted, used to select tasks, or used to change the 10% threshold.

## 8. Falsification and stopping

The later confirmatory hypothesis is: *for tasks from the pre-registered workload, Laconic reduces total OMP-modelled cost per completed task by at least the product-relevant threshold without reducing completion.*

The variance pilot does not test that hypothesis. It stops without an effect estimate if any frozen validity condition fails: a task tree or prompt drifts; an arm does not use the pinned model/configuration; completion differs; a Laconic or Headroom mechanism cannot be verified; usage is missing; the gateway cannot reserve spend before forwarding; a request would exceed $10; live OMP or dogfood state changes; or private data reaches a public artifact.

A future confirmatory result falsifies the product-relevant savings claim for this workload if the pre-registered interval favors native OMP or lies wholly inside the ±10% equivalence bounds, or if Laconic completes fewer tasks than the frozen tolerance permits. A null or negative result does not terminate the runtime product: its beta gate is safety, not savings.

## 9. Authorization

The pilot may run only when every condition below is satisfied:

1. **Satisfied — informed sign-off and fresh instruction.** H-99 records the owner's review of the M19 composition/limitations/privacy/design packet and the explicit instruction to implement and run the recommended comparison.
2. **Pending until committed — pilot pre-registration.** The manifest must pin all tasks, repeats, 24 arm orders, model/catalog, price authority, metric, estimator, 10% threshold, limits, privacy schema, $10 cap, and stopping rules before the credential probe or first paid arm.
3. **Pending until verified — enforced cap.** The gateway must reserve a conservative request maximum before forwarding and refuse a request that could exceed $10. Intention, wall-clock timeout, or post-run accounting is not enforcement.
4. **Required — disposable isolation.** Every arm runs in an owner-only isolated OMP directory and disposable Git repository. It never changes the owner's live OMP profile, worktree, runtime ledger store, or ordinary sessions.
5. **Required — uninterrupted dogfood.** The collection already running is neither disabled nor used as an arm. Before/after state digests and `laconic-dogfood-check` must remain clean.

The confirmatory study is not authorized by satisfying these pilot conditions.

## 10. What this document authorizes

This design authorizes implementation of the bounded harness and one frozen variance pilot under H-99. It does not authorize a confirmatory run, post-result tuning or restarts, public arm costs or effect estimates, a token/cost savings claim, a Laconic-versus-Headroom superiority claim, `laconic.costs` repricing, release preparation, tagging, or publication.
