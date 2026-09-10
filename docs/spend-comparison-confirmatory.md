# Controlled Spend Confirmatory Study — Draft Pre-Registration

**Status: draft for owner review. Not authorized, not funded, not executable.** No confirmatory manifest exists, no manifest digest has been minted, no fixture beyond the four pilot tasks has been authored, and no provider request may be made against this document. Scoping it surfaced three blockers that change the cost and the claims boundary materially; each needs an owner decision before a manifest is committed.

## 1. What the pilot licensed

M20-v2 completed under manifest `0a7cacb9e3970ed578a78eb1363d7e6fa70dec8fd7fbdd45aec1fa15d5efac28` with 24 of 24 valid cells and `$0.9975374` of pooled gateway spend. Its whole purpose was to size a confirmatory study, and it returned a paired log-cost standard deviation of `0.141205388084` and a confirmatory requirement of **15 tasks at two repetitions**.

That number is a faithful output of the frozen calculation. It is not, on its own, a safe basis for funding a study.

## 2. Blocker one — the sample size rests on four observations

The pilot's dispersion estimate comes from **four** paired task differences, one per frozen task. A standard deviation estimated on three degrees of freedom is very imprecise, and the required sample scales with its square.

Using the frozen parameters — two-sided alpha `0.05`, power `0.80`, smallest worthwhile reduction `10%` on the log scale so `Δ = |ln 0.9| = 0.105361`, normal approximation — the required task count is `n = (z_{α/2} + z_β)² σ² / Δ²`:

| Basis for σ | Sidedness | σ | Required tasks | Cells | Estimated spend |
| --- | --- | ---: | ---: | ---: | ---: |
| Pilot point estimate | — | 0.1412 | **15** | 90 | $3.74 |
| 80% upper confidence limit | one-sided | 0.2439 | 43 | 258 | $10.72 |
| 90% upper confidence limit | one-sided | 0.3199 | 73 | 438 | $18.21 |
| 95% upper confidence limit | one-sided | 0.4123 | 121 | 726 | $30.18 |
| Two-sided 95% CI, lower endpoint | two-sided | 0.0800 | 5 | 30 | $1.25 |
| Two-sided 95% CI, upper endpoint | two-sided | 0.5265 | 196 | 1176 | $48.88 |

The sidedness column matters and is easy to misread. The first three upper limits are conventional **one-sided** upper confidence limits on σ, which is the right form for sizing decisions because only under-estimating σ is harmful. The last two rows are the two endpoints of the **two-sided** 95% interval, so the last row's `0.5265` is a one-sided 97.5% bound, not the one-sided 95% bound of `0.4123` above it. Read as a single ladder they would over-provision.

Taking the two-sided endpoints together, the 95% confidence interval for the required task count is roughly **5 to 196**. That is a statement about the interval induced by the σ interval, not a probability statement about a single unknown n; the practically useful figures are the one-sided upper limits, because a study is harmed by under-sizing and merely made more expensive by over-sizing. A fixed 15-task design is powered only if the true σ is at or below the pilot's point estimate; if σ is at the 80% upper confidence limit the study is powered at well under half its nominal 80%, and it would spend real money to produce an inconclusive result — the most expensive possible outcome, because an underpowered null cannot be distinguished from a true absence of effect.

Estimated spend assumes the pilot's observed `$0.041564` per cell holds. That is itself an extrapolation from 24 cells and should be treated as a planning figure, not a cap.

### Recommended response

Two defensible designs, in preference order.

1. **Internal-pilot two-stage design.** Freeze a first stage of 15 tasks, re-estimate σ from the completed stage, and recompute the total requirement before deciding whether to fund a second stage. This spends `$3.74` to buy a far better σ than four pairs gave, then makes the larger funding decision on evidence.

   Pre-specification alone does not control type I error; the *method* does. The re-estimation must be **blinded — variance only**. Stage one's paired differences may be used to re-estimate σ and nothing else; the interim effect estimate must not be inspected, and must not influence whether stage two runs, how large it is, or when to stop. Blinded variance re-estimation has negligible type I inflation. Any use of the interim effect requires a combination test or a conditional-error rule instead, and the pre-registration must say which it uses before data exists.

   Stage one's observations **are pooled** into the final effect estimate, so the two stages form one study with one analysis, not a pilot followed by a replication. The final report states the total task count actually run and the fact that it was re-estimated.
2. **Size to a one-sided upper confidence limit.** Fund 43 tasks against the one-sided 80% upper limit, roughly `$10.72`, or 121 tasks against the one-sided 95% upper limit, roughly `$30.18`. Simpler and single-shot, but it pays for power that may not be needed and still is not guaranteed.

A fixed 15-task single-stage study is **not recommended**. It is the option most likely to spend money and answer nothing.

## 3. Blocker two — eleven task fixtures do not exist

The repository contains four task fixtures, `t01` through `t04`, each a seed tree of roughly 60–100 lines of Python with a failing baseline, a prompt, a solution patch, and a completion oracle. A 15-task study needs **11 new fixtures**; a 43-task study needs **39**.

Each new fixture must satisfy the same contract the existing four do: a deterministic seed repository, a `diagnose.py` that fails before the fix and passes after, a prompt that does not prescribe a tool trajectory, a reference solution patch that applies cleanly, and a `unittest` oracle that fails before and passes after. Every one of those properties is verified by `manifest check --verify-oracles`.

The validity risk is larger than the authoring effort. The pilot's σ describes dispersion across four tasks that were chosen together and resemble each other. If the eleven new tasks are minor variations of the same shape, the confirmatory study inherits a σ that does not generalize and the result will not transfer to real work. If they are genuinely diverse, the true σ is likely **higher** than the pilot's estimate, which pushes the required n up again — the first blocker and this one are coupled, not independent.

This is the dominant cost of the study, and it is human authoring time, not provider spend.

## 4. Blocker three — the claims boundary reverses

Every controlled-spend artifact published so far deliberately withholds the paired effect. The pilot publishes dispersion, correlations, counters, and pooled spend precisely so that no reader can recover which arm was cheaper; a security review confirmed direction is unrecoverable from the published figures.

A confirmatory study exists to publish exactly that: **a paired effect estimate, its confidence interval, and a direction.** Running it is a deliberate decision to start making a comparative cost claim about Laconic, native OMP, and Headroom, including the possibility that the result is unfavourable or equivocal and must be published anyway.

The pre-registration must therefore fix, before any data exists:

- the exact estimand and its published form;
- the equivalence bounds, so a null is reported as equivalence rather than as absence of evidence;
- a commitment to publish the interval whatever its sign;
- what remains private even in a confirmatory report.

## 5. Structural work in the tooling

`TASK_COUNT` is a frozen module constant of `4` in `tools/controlled_spend/manifest.py`, enforced for every manifest, and `RUN_COUNT` derives from it. A confirmatory manifest with a different task count requires making the task count schema-scoped so the M20-v1 and M20-v2 manifests continue to validate byte-identically under their existing digests. That is a contained change with an obvious regression test, but it must land and be reviewed before a confirmatory manifest can be committed.

The gateway cap, receipt binding, quiescence gate, and privacy allowlist all carry over unchanged. A confirmatory campaign will run longer than nine minutes in proportion to its cell count, so the quiescence window must be re-sized to the new expected duration.

## 6. Open decisions

None of these can be resolved from the pilot data.

1. Two-stage internal pilot with blinded variance-only re-estimation, or single-stage sized to a one-sided upper limit at 43 or 121 tasks, or something else.
2. Who authors the 11 or 39 new fixtures, and against what diversity criterion.
3. Whether the project is prepared to publish a comparative cost claim in either direction, including an unfavourable one.
4. The spend cap for the confirmatory campaign, which is not the pilot's `$10.00`.

## 7. Not authorized by this document

No confirmatory manifest, no manifest digest, no fixture authoring, no credential access, no provider request, no spend, no change to any published artifact, and no savings, equivalence, direction, or superiority claim. M20-v1 and M20-v2 remain closed and may not be rerun or pooled into this study.
