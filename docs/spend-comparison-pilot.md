# Controlled Spend Variance Pilot

## Scope

This runbook operates the one owner-authorized M20 variance pilot. The committed manifest fixes four tasks, two repetitions, three arms, Claude Sonnet 5, OMP 18.1.14, Headroom 0.37.0, a maximum of eight provider requests per run, and a $10 total provider-spend cap.

The pilot measures dispersion and cross-arm cost correlation for confirmatory-sample planning. It is not a performance result. Never publish arm means, a paired effect estimate, task-level costs, per-arm token components, a savings claim, or a product-superiority claim.

A confirmatory run is not authorized. It requires a new committed manifest, a newly frozen sample and cap, and explicit owner authorization.

## Evidence boundaries

Keep the artifact root outside the repository, the live `~/.omp/agent` tree, and the live Laconic runtime-data tree. The runner creates it with mode `0700`. Raw OMP transcripts, Headroom metadata logs, Laconic ledgers, task worktrees, and private arm-cost analysis remain there. Each isolated OMP credential database and every SQLite sidecar are deleted after its run. The campaign credential snapshot is deleted on every exit path.

Only the generated public JSON and Markdown reports may enter Git. Their exact-key privacy gate permits the frozen manifest hash, disposition, completeness and mechanism-failure counts, paired log-cost standard deviation, two cross-arm correlations, confirmatory task-count feasibility, frozen statistical parameters, and total gateway spend. It rejects every additional key.

## Preflight

Run from the repository root after the manifest, runner, gateway, analysis, and privacy changes are merged:

The harness executes the exact `@oh-my-pi/pi-coding-agent@18.1.14` package through `bunx`; it does not trust the workstation's current `omp` executable. The Headroom arm receives a private `omp` shim that delegates to the same exact package.

```bash
uv sync --locked
uv run python -m tools.controlled_spend manifest check --verify-oracles
uv run python -m tools.controlled_spend pilot preflight
laconic-dogfood-check
```

Record content digests for the live OMP agent tree and Laconic runtime-data tree. The runner records independent before/after tree digests in the private campaign state and fails the campaign if either changes. Do not use OMP concurrently while the pilot runs.

## Execute once

Choose a fresh private path outside Git and outside every live-state root. Never reuse a partial path and never restart or tune a failed cell.

```bash
PRIVATE_ROOT="$HOME/.local/share/laconic-controlled-spend/m20-pilot"
uv run python -m tools.controlled_spend pilot run --artifact-root "$PRIVATE_ROOT"
```

The shared loopback gateway reserves the frozen worst-case request cost before forwarding. It stops before a request that could cross $10, charges the reservation if provider usage is missing or malformed, and stops after eight requests in one cell. Native OMP, Laconic, and Headroom use the same gateway, model, prompt, task tree, tools, thinking level, completion oracle, request limit, and wall-clock limit.

Any incomplete cell terminates the campaign. Preserve the private root for diagnosis. Do not resume, replace, add, drop, or rerun a cell after any paid result.

## Generate and verify the public disposition

```bash
uv run python -m tools.controlled_spend pilot report \
  --artifact-root "$PRIVATE_ROOT" \
  --output-json docs/results/controlled-spend-pilot.json \
  --output-markdown docs/results/controlled-spend-pilot.md

uv run python -m tools.controlled_spend pilot check \
  --artifact-root "$PRIVATE_ROOT" \
  --report-json docs/results/controlled-spend-pilot.json \
  --report-markdown docs/results/controlled-spend-pilot.md

laconic-dogfood-check
```

A complete disposition requires all 24 frozen run IDs in exact order, a valid completion result for every cell, one strict OMP usage transcript per cell, provider-request counts matching priced assistant turns, the frozen provider and model, `python3 diagnose.py` as the first tool action, unchanged fixture guards, and mechanism evidence for every arm. Native must have no Laconic or Headroom artifact. Laconic must record at least one eligible and one emitted runtime decision. Headroom must log every request without message content and show either a transformation or explicit numeric pass-through.

An incomplete disposition publishes only counts, spend, and the frozen parameters. Dispersion, correlations, and confirmatory task count remain null.

## Interpretation

For a complete pilot, the private analysis computes each task/arm mean over the two repetitions and the paired task log-cost difference `log(mean_laconic) - log(mean_native)`. The public report exposes only the sample standard deviation of those four paired differences. It also reports native/Laconic and native/Headroom cost correlations across the four task means.

The preliminary confirmatory task count uses a two-sided normal approximation with alpha 0.05, power 0.80, the precommitted 10% worthwhile reduction, and two repetitions per task. It rounds up and applies a two-task minimum because paired-difference dispersion is not estimable from one task. Changing the repetition count invalidates that calculation and requires a new variance model.

Do not infer direction, magnitude, savings, equivalence, or superiority from the variance pilot. Judge only whether the resulting confirmatory sample is operationally feasible. Stop if it is infeasible. If it is feasible, freeze a new confirmatory protocol and obtain explicit spend authorization before any new provider call.
