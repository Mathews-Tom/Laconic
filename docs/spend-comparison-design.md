# Controlled Spend Comparison — Design

**Status: designed, not implemented, not authorized.** This document specifies what a controlled codec-on/codec-off comparison would require. Nothing in this repository implements it, and nothing in this repository is permitted to run it. Implementing it requires a fresh explicit instruction from the owner.

## 1. Why the existing measurement cannot answer the question

`laconic research spend report` measures composition: of the money real sessions actually cost, how much went to uncached input, cache reads, cache writes, and output, and what the codec did in those same sessions.

It cannot measure savings, and no refinement of it can. Every session it reads ran with the codec enabled. There is no observation anywhere in that data of the same work done without the codec, so there is no quantity to subtract. This is not a precision problem that more sessions would fix; it is the absence of a comparison arm. The only bound on a general savings claim remains the committed K1 fixture's 8.53%, and that fixture is a deterministic gate-harness check, not representative evidence.

A comparison therefore requires generating new data under a design that produces both arms. That is the subject of this document.

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

## 3. Arming

**Decision: paired repetition of the same task, with randomized within-pair order, and repeated measurement per cell.**

Three candidates were considered.

*Per-session alternation* — flip the codec on and off between the owner's ordinary sessions. **Rejected.** The work differs between sessions, so the treatment is confounded with the task. This is the design that produces a number quickly and cannot defend it.

*Paired repetition* — run the same task, from the same pinned commit, once with the codec on and once with it off. **Selected.** The task is held fixed, so the difference is attributable to the arm and to run-to-run variance, and to nothing else about what was being done.

*Between-subjects across many tasks* — different tasks in each arm, relying on randomization and volume. **Rejected for the first study.** The per-task cost variance an agent workload exhibits puts the sample required to detect a plausible effect far beyond what a single owner can generate. It remains the correct design for a later multi-participant study.

Within a pair, arm order is randomized: agents are nondeterministic, and always running codec-off first would confound the arm with any ordering effect (warmer file caches, a differently populated model-side cache on the provider). Each (task, arm) cell is run *k* times, because a single run of a nondeterministic agent is a draw, not a measurement. Analysis is paired by task, on the per-task difference of the cell means — see §7 for the exact estimand.

**Freeze before running:** task list, *k*, the metric, the estimand and estimator, the effect-size threshold, and the stopping rule are all written down and committed before the first arm executes. Adding a task, dropping a task, or changing *k* after seeing results invalidates the study.

## 4. The metric

**Decision: total modelled cost per task run, in USD, under one pinned price table, with token components reported alongside.**

Cost, not tokens, because the four token classes are billed at four different rates and the codec plausibly moves volume between them: it can shorten a result (fewer tokens written into the cached prefix) while changing the prefix (invalidating a cache the next turn would otherwise have read cheaply). A token count that adds those four classes together would hide exactly the effect that matters.

Cost is modelled by `laconic.costs` from provider-reported token counters, at a price table pinned for the whole study. Providers return counters, not prices. The pinned table must include every model used, so that no arm is billed at a fallback rate. `laconic.costs.PRICING` today carries published prices for a handful of Anthropic models only, and `laconic.costs.unpriced_models` names every model a corpus used that is missing from it; a comparison may not begin until that list is empty for the models it uses.

Secondary, reported but not the decision metric: wall-clock duration, turn count, and completion (did the arm satisfy the task's condition at all). §7 fixes the estimand these figures are analysed on.

## 5. Induced expansion

**Decision: induced expansion is charged to the codec arm in full, as ordinary spend, and additionally reported separately.**

When the codec replaces a result, the agent may call `laconic_expand` to recover it. Every token of that call — the request, the recovered content entering the context, and every subsequent turn that re-reads it from the cached prefix — is spend the codec-off arm never incurs. It is not overhead to be netted out or excused; it is the cost of the mechanism.

Charging it in full is automatic if the metric is the arm's total cost, which is why the metric is defined that way rather than as a sum over compressed observations. The separate report exists for diagnosis, not for adjustment: an arm that wins only after its expansions are excluded has not won.

The same rule covers induced *work*: extra turns the agent takes because it saw a reference instead of content. Those turns are in the arm's total by construction. A design that measured only the compressed observations would miss them entirely, which is the central reason the unit is a whole task run.

## 6. Cache-write amortization

**Unresolved, and this is the hardest part of the design.**

The codec changes the prompt prefix. A cached prefix is billed at 1.25× input to write and 0.10× input to read, so a change that invalidates a prefix pays the write again and loses the cheap reads that would have followed. Over a long session, a single mid-prefix change can cost more than every character it removed.

Three properties make this hard to attribute:

- The charge is **displaced in time**. The write happens on the turn the prefix changes; the loss shows up as reads that never occur on later turns.
- The charge is **order-dependent**. The same set of observations, compressed in a different order, produces different cache behavior.
- The provider's cache policy is **not observable**. Time-to-live, eviction, and prefix-matching granularity are inferred from `cacheRead`/`cacheWrite` counters, not documented as a contract.

Two candidate treatments:

1. **No attribution.** Compare arm totals and say nothing about which component caused the difference. The comparison stays valid; it just does not explain itself. Component-level totals are still reported descriptively.
2. **Component decomposition with a stated assumption.** Report the per-component difference and state explicitly that the cache-write component is not attributable to individual compressions.

Option 1 is the safe default and is what a first study should do. Option 2 requires an assumption about provider cache behavior that this project cannot currently verify, and adopting it would smuggle an unverified provider model into a headline number. **This is recorded as unresolved rather than decided**: it must be settled, in writing, before a study runs, and a study that reports per-component attribution without settling it is invalid.

## 7. Effect size and sample

**Unresolved as a specific number; the procedure for fixing it is decided.**

The threshold cannot be chosen from the composition report, because that report has no variance estimate for a repeated task — it has one observation per session and no session was ever repeated. Choosing an effect size from it would be choosing it from data.

**Fix the estimand first.** §4 makes the metric total modelled cost per task run in USD, but a threshold stated as "an X% reduction" is a different quantity, and powering one while testing the other is incoherent. The estimand is the **paired per-task log-cost difference**, `d(t) = log(mean_on(t)) - log(mean_off(t))`, so that a threshold expressed as a percentage maps onto it directly and a task costing ten dollars does not outweigh ten tasks costing one. Absolute USD differences are reported alongside, descriptively.

The required sequence, in order:

1. **Pilot both arms.** Run *n* tasks, *k* times each, **in both arms** — this is the correction that matters. A paired test's power is governed by the standard deviation of the within-pair difference `d(t)`, which cancels between-task cost variance entirely and depends on the cross-arm correlation. A codec-off-only pilot measures precisely the dispersion that pairing removes, so feeding its coefficient of variation into a paired power calculation either wildly overstates the sample or silently assumes a correlation nobody measured. Report `SD(d)` and the cross-arm correlation. Publish nothing else from the pilot: it is a variance estimate, not a result, and its point estimate of the effect is not reported, quoted, or used.
2. **State the smallest effect worth acting on.** This is a product decision, not a statistical one: below what percentage reduction in total cost, on this workload, would nobody install the thing? Write it down before step 3, and — because the pilot has by then produced an effect estimate whether or not it is looked at — have someone who has not seen the pilot's arms write it down.
3. **Compute the sample** from `SD(d)` and that threshold, for a paired design at conventional power, and write the resulting *n* and *k* down. Note that *k* enters twice: it shrinks the within-cell noise in each `mean_on(t)`/`mean_off(t)` before pairing, so the trade between more tasks and more repeats is explicit rather than incidental.
4. **If the required sample exceeds what one owner can generate**, say so and stop. That is a legitimate and likely outcome, and it is a better result than an underpowered study reporting a number.

Recording the honest expectation: a local run of `laconic research spend report` over the author's own sessions at the time of writing put cache reads near 70% of spend both across the corpus and within codec-active sessions, with a few tens of thousands of characters avoided across a couple of hundred eligible observations. Those figures are not reproducible from this repository — they describe one machine's private sessions at one moment, and the report they come from is written to a git-ignored directory. They are cited only as the order of magnitude that shaped this design. Nothing in that picture suggests a large effect, and the variance in per-task agent cost is likely to be substantial. Step 4 firing is a realistic outcome of this design, not a failure of it.

## 8. Falsification

The hypothesis under test is: *for tasks of this kind, running with the codec enabled reduces total modelled cost per task run.*

It is falsified if any of these holds on the pre-registered analysis:

- the paired estimate is zero or favors codec-off, with the confidence interval excluding the pre-registered threshold;
- the confidence interval lies wholly inside the equivalence bounds set by that threshold — an equivalence result, which is a real answer and must be reported as one rather than as "no significant difference";
- the codec arm completes fewer tasks than the codec-off arm by more than a pre-registered tolerance, in which case cost is not comparable at all because the arms did different amounts of work;
- any arm's model is unpriced, any task's repository state drifted between arms, or any run exceeded its step budget in one arm only.

A null or negative result terminates the savings claim for this workload. It does not terminate the product: the runtime beta's gate is safety, not savings, and `docs/grounding.md` already separates the two.

## 9. Authorization

A comparison may not run until all of the following are true, and each is a separate condition:

1. The owner issues a fresh explicit instruction to implement and run it. Nothing in M19, and nothing in this document, constitutes that instruction.
2. The pre-registration — task list, *k*, metric, price table, effect-size threshold, sample, stopping rule, and the §6 cache-attribution decision — is written and committed **before** the first arm executes.
3. A spend cap is set and enforced in the harness, not by intention. Real provider calls cost real money, and a runaway agent arm is the expected failure.
4. The comparison runs in disposable repositories at pinned commits, never against the owner's live work, and never by disabling the codec in the owner's ordinary sessions.
5. The dogfood collection running today is not interrupted, reconfigured, or used as an arm.

## 10. What this document does not do

It does not implement anything. There is no harness, no task list, no pre-registration, and no pilot. It states what would have to be true for a savings claim to be defensible, so that the decision to pursue one — or to decline to — is made deliberately rather than by drifting into it.
