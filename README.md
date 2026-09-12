# Laconic

**Your coding agent re-reads the same file on every single turn. You pay for it every single turn.**

Laconic is a local codec that sits at your agent's tool boundary. It replaces big tool results with a structural outline and the span that was actually asked for — and keeps the exact original bytes on disk, addressable, until you explicitly purge them.

[![PyPI](https://img.shields.io/pypi/v/laconic)](https://pypi.org/project/laconic/)
[![Python](https://img.shields.io/pypi/pyversions/laconic)](https://pypi.org/project/laconic/)
[![CI](https://github.com/Mathews-Tom/laconic/actions/workflows/ci.yml/badge.svg)](https://github.com/Mathews-Tom/laconic/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

```bash
uv tool install laconic
laconic setup
```

That is the whole installation. It touches no provider configuration, proxies nothing, and needs no account.

## Everyone is compressing the wrong thing

We measured 179 real agent sessions — 19,818 assistant turns, $2,134.27 of modelled spend — before writing any product code. The result killed our own first version:

| Where the money goes | Share of spend |
| --- | ---: |
| **Cache reads** — re-ingesting the resident context, turn after turn | **60.3%** |
| Cache writes | 26.7% |
| **Everything the model says** — prose, patches, commands | 11.3% |
| Uncached input | 1.7% |

Human-facing prose is **2.30%** of the bill. 80.6% of turns emit no prose at all. Compressing *that prose* by 44% — our own v1's best measured result — moves a real session bill by **1.01%**. Deleting every word of prose saves 2.30%. That is the ceiling, and it is not a product.

Meanwhile tool results sitting resident in the prefix are **≈38.1% of total spend**, seventeen times the entire prose channel, and the mean resident prefix is **205,842 tokens per turn**.

The bill is not what your agent *says*. It is what your agent *carries*.

That corpus is the author's own and is not shipped. What you can do is run the identical measurement over *your* sessions, or over the fixture committed to this repo:

```bash
uv run python scripts/measure_session_composition.py   # your own transcripts
uv run laconic research measure tests/corpus           # the committed fixture
```

## What it actually does

Your agent reads a 745-line file. Here is what it receives instead — the real output of `laconic.ledger` encoding its own source:

```text
[laconic 01a0f3c2-.../X1 | full: laconic_expand({"reference":"01a0f3c2-.../X1"})]
"""Content-addressed store of every observation the codec has elided.

The ledger upholds the design's central invariant: compression is lossy in
presentation, lossless in reach. Anything an encoder removes from what the
model sees stays addressable here, so every elision is reversible.
...
  [4 error lines from the elided region]
  [... 665 lines elided — expand with the handle]
...
        self.close()
```

**29,186 characters in, 3,097 out** — 89% fewer characters at the tool boundary. The recovery header is counted too, so the complete envelope the model actually receives is 3,227 characters, and Laconic emits it only because *that* total is still smaller than the original. (The reference is abbreviated above for width; a real one carries the full session id.)

Nothing is gone. The first line carries the handle, so the model can pull the full file or any line span back itself, mid-task, without asking you.

Note the second-to-last line. Lines that look like errors get lifted *out* of the elided region and kept, because a traceback buried in the middle of a file is exactly the thing you cannot afford to elide silently.

That is the trade other compressors cannot make. Headroom's default coding profile deliberately *protects* file reads from compression, because an agent needs exact bytes to patch a file. Laconic stores the exact bytes first, which is what makes reducing the read safe at all.

## Why you can leave it on

**Nothing is lost.** Raw content commits to a local ledger *before* a replacement is allowed to exist. Every emitted reference expands exactly — byte for byte, including code points a strict encoder would reject. Across the qualification campaign, exact-expansion failures: **0**.

**It declines more often than it fires.** A replacement is emitted only when the complete recovery-bearing envelope — handle, header and all — is strictly smaller than the original. In the qualification campaign it passed 96 of 137 eligible observations straight through untouched. A codec that refuses 70% of its opportunities is a codec that isn't guessing.

**It fails open, in every direction.** Engine missing, spawn failure, crash, malformed response, deadline breach, storage error — you get the original tool result. There is a 250 ms steady-state deadline and a three-consecutive-failure circuit breaker. A crash costs compression, never correctness.

**It's fast enough to forget about.** p50 **1.45 ms**, p95 **18.65 ms**.

**It's yours.** No telemetry, no hosted service, no beacon. Exactly one command in this entire tool reaches the network — `laconic pricing update` — and only when you type it. Raw observations never leave your machine. Reports are content-free by construction and then re-checked by an independent privacy gate that refuses to serialize a single key it cannot certify; a new field with no shape check fails loudly rather than shipping uncertified.

**You can always get out.** `laconic status` to inspect, `/laconic pause` mid-session, `laconic uninstall` to restore native behaviour, and `laconic purge` as a separate, deliberate act. Uninstalling never deletes your recovery ledgers; purging is something you have to mean.

## Two hosts, one engine

```bash
laconic setup
```

`setup` detects what you actually have, installs what each host supports, and then tells you whether the codec has recorded a real decision yet — because installing a file is not evidence that anything ran.

| Host | Codec | Diagnostics | What it hooks |
| --- | --- | --- | --- |
| OMP | yes | yes | Native extension over `read`, `bash`, `grep`, `glob` |
| Claude Code | yes | yes | Transforming `PostToolUse` hook over `Bash` and `Read` |
| Codex | no | no | No adapter ships, and Laconic says so rather than pretending |

Both adapters are thin. They drive the same Python engine, so the strictly-smaller rule, the ledger, reference minting and exact recovery are shared rather than forked per host.

```bash
laconic setup --verify-only    # did it actually run?
```

## What it costs you — and what we refuse to claim

```bash
laconic savings
```

Verbatim, from this machine:

```text
Modelled cost avoided
  $110.80 to $255.82  (4.58% to 10.58%)
  against a modelled $2,418.27 for the sessions this estimate covers
  A model, not a measurement: no session ran without the codec, so
  this is what the removed characters would have cost, not a saving
  anyone observed. Every assumption is listed in the written report.
```

The command carries its own caveat because the caveat is load-bearing.

**It is a model, not a measurement, and it says so in code.** Every session Laconic has ever recorded ran with the codec *on*. There is no counterfactual anywhere in that data, so there is nothing to subtract. The figure carries `basis: modelled_not_measured` in the JSON, and the privacy gate refuses to serialize the block under any other value.

It is a *band* because more than one input is assumed — characters per token, and how much of your corpus's cache re-read rate the removed tokens would really have seen. That second assumption carries most of the price, so it is banded rather than stated as fact.

**And it withholds itself when it shouldn't be trusted.** Prices resolve through a registry that ships 3,134 models offline. When more than 25% of the cost the estimate is built from comes from models with no published list price, `status`, `savings` and the written report stop printing dollars and lead with the percentage instead — because the same pricing error sits in the numerator and the denominator and largely cancels in a share, but not in a dollar figure.

We found that out the hard way. A hand-written price table was shadowing that registry and had gone stale on two models; it inflated our own modelled corpus by $2,331.82 — more than the entire discrepancy we were chasing. It is gone, and the fix is a structural test that fails if any second price source is ever put in front of the registry again.

**What we will not say:** that Laconic saves you tokens, money, cache or latency in general. Character reduction at the tool boundary is a character count. The conversion to tokens and then to money is lossy and workload-dependent, and we do not have the paired evidence that would license the claim. Our own research gate measured **8.41%** net cost reduction on a committed fixture against a pre-registered 15% threshold — that is a **kill**, published rather than buried, and it still bounds any general savings claim this project could make.

If that honesty is a dealbreaker, this is the wrong tool. If it is the reason you'd trust the rest, welcome.

## Verify the claims above

Nothing here rests on our summary of ourselves.

```bash
laconic status                                  # your own decisions, counts, recovery ledger
laconic expand '<session>/X1'                   # pull any elided observation back, exactly
laconic expand '<session>/X1:40-90'             # or just a line span
uv run laconic research gates --corpus tests/corpus --format json
```

Upgrading from 0.8.0 or earlier? Offline research commands moved under an
explicit namespace: `laconic measure` and `laconic gates` are now
`laconic research measure` and `laconic research gates`.

That last command exits **non-zero**, deliberately: our own K1 gate is a kill on
the committed fixture, and the harness reports it as one.

The qualification campaign's generated report is committed verbatim, including the counters that would have failed it: [`docs/runtime-beta-report.md`](docs/runtime-beta-report.md). Ten sessions, three repositories, 137 eligible observations, all 26 required failure and lifecycle scenarios exercised with none missing, every safety counter zero, and 35.84% character reduction on that read-heavy workload — a figure that describes *that* workload and nothing else.

## Documentation

| Document | What's in it |
| --- | --- |
| [`docs/grounding.md`](docs/grounding.md) | **Start here.** What Laconic is, what it deliberately is not, and how to detect strategy drift |
| [`docs/omp-runtime.md`](docs/omp-runtime.md) | OMP install, interception boundary, recovery, controls, uninstall, purge |
| [`docs/claude-code-codec.md`](docs/claude-code-codec.md) | The Claude Code hook, its shape-fidelity constraint, and its measured limits |
| [`docs/overview.md`](docs/overview.md) | The full measurement that defines the problem, and the positioning it forces |
| [`docs/system-design.md`](docs/system-design.md) | Architecture: engine, ledger, codec, price registry, protocol boundaries |
| [`docs/headroom-comparison.md`](docs/headroom-comparison.md) | Version-pinned comparison with Headroom, including where Headroom is the better choice |
| [`docs/research-disposition.md`](docs/research-disposition.md) | Prior evidence, terminal research outcomes, and claims that remain unproven |
| [`docs/pitch.md`](docs/pitch.md) | The short version |
| [`docs/spend-comparison-design.md`](docs/spend-comparison-design.md) | Why a single-arm corpus cannot produce a savings figure, and the pilot that tried |
| [`docs/observe-cli.md`](docs/observe-cli.md) | `laconic diagnostics observe`: content-free local diagnostics |
| [`docs/observe-design.md`](docs/observe-design.md) | Observe as a released, automatic, content-free diagnostic surface |
| [`docs/runtime-beta-runbook.md`](docs/runtime-beta-runbook.md) | How the qualification campaign is frozen, run and reported |

## Working on it

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict src
uv run python -m pytest -q
```

Python 3.12+, `uv`, `mypy --strict`, 1,596 tests. `docs/grounding.md` is the charter — changes are expected to advance the runtime, recovery, fail-open behaviour, operator control, packaging or bounded proof, and not to quietly widen a claim.

## Roadmap

1. ~~Transport-neutral session engine with namespaced exact recovery and strict-smaller decisions.~~
2. ~~Ownership-safe OMP extension with a 250 ms deadline, fail-open behaviour, expansion and operator controls.~~
3. ~~Qualify the built package through real OMP sessions across multiple repositories.~~
4. ~~Publish the opt-in beta.~~ `v0.9.0`, with `v0.9.1` fixing a cold-start defect only a clean first install could reach.
5. ~~Measure where model spend actually goes, without overclaiming.~~ Single-arm, no savings figure; M20-v1 ended incomplete, M20-v2 published variance and feasibility only.
6. ~~Extend the codec to a second host.~~ Claude Code, `v0.11.0`.
7. Earn a savings claim, or keep declining to make one. Needs a real comparison arm; MCP, action rewriting and history compaction stay deferred until runtime evidence justifies them.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
