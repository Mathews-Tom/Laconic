# Laconic – Compress what a coding agent carries, not what it says.

> **Status: the opt-in OMP runtime beta is published.** Version 0.10.0 adds read-only spend-composition reporting to the installable package. The repository also records the immutable incomplete M20-v1 result and the completed M20-v2 variance pilot, which reports dispersion, cross-arm cost correlations, and a confirmatory sample size but no savings figure. The predeclared real-OMP qualification campaign passed with every safety counter at zero — see [`docs/runtime-beta-report.md`](docs/runtime-beta-report.md). It is a beta: installation is explicit and opt-in, and it makes no token, cost, cache, or behavior savings claim.

Laconic is a private, local runtime codec for existing coding agents. It reduces eligible model-visible `read`, `bash`, `grep`, and `glob` observations while preserving exact, on-demand access to omitted content. It integrates with OMP rather than replacing it.

> **[`docs/grounding.md`](docs/grounding.md) is the authoritative statement of what Laconic is, what it deliberately is not, and how to detect strategy drift.** Read it before proposing or reviewing changes.

## How Laconic differs from Headroom

[Headroom](https://github.com/headroomlabs-ai/headroom) is a broad context-compression platform: libraries, a local provider proxy, MCP tools, agent wrappers, multiple compressors, memory, cache-aware request handling, and optional output shaping. Laconic is intentionally narrower. It intercepts OMP tool results inside the host, leaves provider configuration unchanged, and focuses on coding-agent observations backed by exact local recovery.

Laconic's strongest distinction is file reads. Headroom's default coding profile protects reads because agents need exact source bytes. Laconic stores those exact bytes first, then can replace a large read with a structural outline and requested span while preserving exact full or line-span expansion until explicit purge. The OMP beta adds strict-smaller replacement, bounded fail-open behavior, owner-namespaced recovery, explicit operator controls, and no Laconic telemetry or hosted service.

Headroom is the stronger choice for broad client, provider, content-type, and framework coverage. Laconic is the stronger fit when the requirement is an OMP-native, provider-neutral, auditable observation boundary with durable exact recovery. No controlled head-to-head study establishes a token, cost, cache, latency, or behavior advantage for either system on the same coding-agent tasks. See [`docs/headroom-comparison.md`](docs/headroom-comparison.md) for the version-pinned comparison, evidence limits, and source links.

## Install

```bash
uv tool install laconic
laconic setup --dry-run
laconic setup
```

`laconic setup` detects which coding-agent hosts are present, states what each one can actually do, installs Laconic's owned adapter for every supported host, and then reports whether the runtime has recorded a real decision. It is idempotent and never contacts a provider.

Capability differs by host, and the difference is the whole point of the table it prints:

| Host | Codec | Observe |
| --- | --- | --- |
| OMP | yes | yes |
| Claude Code | yes | yes |
| Codex | no | no |

`setup` installs the codec on OMP, and on Claude Code installs both a transforming `PostToolUse` hook and content-free receipt hooks as separate owners of one settings file. Codex has no adapter of either kind.

The two hosts compress by different mechanisms and over different tool sets. OMP intercepts every eligible tool result; Claude Code replaces `Bash` and `Read` results through a hook, and does not expose `Grep` or `Glob` as tools at all. See [`docs/claude-code-codec.md`](docs/claude-code-codec.md) for the shape-fidelity constraint that design is built around and what it removed on real sessions.

Installing writes a file; it does not prove the codec ran. Start a session on a host whose codec column is `yes`, invoke a `read`, `bash`, `grep`, or `glob` tool, then:

```bash
laconic setup --verify-only
```

A confirmed line means the runtime decided a real observation. A codec that decided to pass everything through is still working — most tool results are too small to shrink, and declining them is the correct outcome.

To install a single adapter by hand instead, use `laconic install omp` or `laconic diagnostics observe install --client claude-code`. Use `/laconic status|pause|resume` in an active OMP session, `laconic status` for content-free aggregate health, and `laconic expand '<session>/F1[:first-last]'` for exact operator recovery. See [`docs/omp-runtime.md`](docs/omp-runtime.md) before installing or purging data.

## Modelled cost avoided

```bash
laconic research spend report
```

Writes a local, content-free composition of where your model spend went, alongside a **modelled** estimate of what the codec's removed characters would otherwise have cost. Both hosts are scanned.

The estimate is a band, not a figure, and it is labelled `modelled_not_measured` everywhere it appears. Every session Laconic has ever recorded ran with the codec **on**, so no counterfactual exists and **nothing here is a measured saving**.

What the model does is divide the per-token cache prices out of your own corpus rather than a price table. What it assumes — and therefore what the band spans — is how many characters a token carries, and how much of your corpus's cache re-read rate the removed tokens would really have seen. That second assumption is most of the price, so it is banded rather than stated as fact.

The re-read term is why tool-boundary removal is worth more than its character count suggests. Text removed there is written to the prompt cache once and re-read never, so removal compounds across a session instead of saving once. The corpus average overstates it for tool output, which arrives later in a session than the system prompt it is averaged with, so the low end of the band discounts it.

## Documentation

| Document | What's in it |
| --- | --- |
| [`docs/grounding.md`](docs/grounding.md) | Product boundary, invariants, runtime gate, and drift checks |
| [`docs/omp-runtime.md`](docs/omp-runtime.md) | Runtime installation, interception boundary, recovery, controls, uninstall, and purge |
| [`docs/claude-code-codec.md`](docs/claude-code-codec.md) | Claude Code transforming `PostToolUse` hook: setup, shape fidelity, measured limits |
| [`docs/headroom-comparison.md`](docs/headroom-comparison.md) | Version-pinned comparison with Headroom: product boundaries, strengths, recovery, privacy, and evidence limits |
| [`docs/research-disposition.md`](docs/research-disposition.md) | Prior evidence, terminal research outcomes, and claims that remain unproven |
| [`docs/pitch.md`](docs/pitch.md) | The short version: problem, measured channel opportunity, product boundary, and limitations |
| [`docs/overview.md`](docs/overview.md) | Full what/why/how, measurements, positioning, and separated product/research gates |
| [`docs/system-design.md`](docs/system-design.md) | OMP-first runtime architecture, recovery, protocol boundaries, and supporting components |
| [`docs/observe-design.md`](docs/observe-design.md) | Observe as a released, automatic, content-free diagnostic surface |
| [`docs/observe-cli.md`](docs/observe-cli.md) | `laconic diagnostics observe` guide: install/remove/status/report |
| [`docs/k1-stage-a-cli.md`](docs/k1-stage-a-cli.md) | `laconic research k1 stage-a scan` metadata feasibility guide |
| [`docs/k1-stage-b-manifest-cli.md`](docs/k1-stage-b-manifest-cli.md) | `laconic research k1 stage-b build-manifest` guide |
| [`docs/runtime-beta-report.md`](docs/runtime-beta-report.md) | The qualification campaign's generated aggregate report, committed verbatim |
| [`docs/runtime-beta-runbook.md`](docs/runtime-beta-runbook.md) | How that campaign is frozen, run, and reported with `python -m laconic.beta` |
| [`docs/spend-comparison-design.md`](docs/spend-comparison-design.md) | Controlled-spend design, immutable incomplete M20-v1 result, and the completed M20-v2 variance pilot |

## Source checkout

Releases from 0.9.0 onward contain the runtime, codec, evaluation, rendering, and Observe surfaces. Version 0.10.0 adds read-only spend-composition reporting to the installable package and versions repository-only controlled-spend research tooling, whose M20-v2 pilot has now run once and completed. Versions up to 0.8.0 had no live runtime integration and exposed research commands at the top level, such as `laconic measure` and `laconic gates`. Since 0.9.0 those commands live under the explicit `research` namespace:

```bash
uv run laconic research measure tests/corpus --expect tests/corpus/expected.json
uv run laconic research gates --corpus tests/corpus --format json
laconic --help
```

The committed fixture reports K1 net savings of 8.53% against its pre-registered 15% research threshold. It validates the gate machinery but is not representative deployment evidence. That result does not block the bounded OMP beta, and the beta will not claim general token, cost, cache, or behavior savings from character reduction. The qualification campaign separately measured 35.84% character reduction across ten agent-driven read-heavy investigation sessions; that figure describes that workload only, and no savings threshold gates the beta. See the "Beta qualification result" section of [`docs/omp-runtime.md`](docs/omp-runtime.md) for how the campaign was produced and what it does not establish.

## Product roadmap

1. ~~Build a transport-neutral session engine with namespaced exact recovery and strict-smaller decisions.~~
2. ~~Package an ownership-safe OMP extension with a 250 ms deadline, fail-open behavior, expansion, and operator controls.~~
3. ~~Qualify the built package through at least 10 completed real OMP sessions across 3 repositories and at least 100 eligible observations.~~
4. ~~Publish the opt-in OMP beta.~~ Published as `v0.9.0`, with `v0.9.1` correcting a cold-start defect that only a clean first install could reach.
5. ~~Measure where model spend goes and build a controlled-comparison protocol without overclaiming.~~ The single-arm composition report contains no savings figure; M20-v1 ended incomplete; M20-v2 ran once and completed, publishing variance and feasibility only, with arm means and the paired effect held private.
6. Design the Claude Code adapter separately after the protocol survives OMP dogfood. MCP, action rewriting, and history compaction remain deferred.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
