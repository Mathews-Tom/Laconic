# Controlled Spend Variance Pilot

## Scope

This runbook preserves the completed M20-v1 pilot and specifies the unexecuted M20-v2 candidate. M20-v1 remains bound to `tools/controlled_spend/pilot-manifest.json`, its original prompts, a maximum of eight provider requests per run, and manifest hash `a76c6cb0d2f34737ccd629398b0b2122a3c0a74c63a77055f8112cea602f7b44`.

Both protocols concern dispersion and cross-arm cost correlation for confirmatory-sample planning. Neither is a performance result. Never publish arm means, a paired effect estimate, task-level costs, per-arm token components, a savings claim, or a product-superiority claim.

M20-v2 is a candidate only. Its separate manifest and private root must remain execution-disabled until the owner authorizes the exact committed hash. A confirmatory run also requires a new committed manifest, newly frozen sample and cap, and explicit owner authorization.

## Evidence boundaries

Keep every campaign artifact root outside the repository, the live `~/.omp/agent` tree, and the live Laconic runtime-data tree. M20-v1 remains read-only at its preserved private root. M20-v2 requires a different fresh root created with mode `0700`. Raw OMP transcripts, Headroom metadata logs, Laconic ledgers, task worktrees, and private arm-cost analysis remain private. Each isolated OMP credential database and every SQLite sidecar must be deleted after its run, and the campaign credential snapshot must be deleted on every exit path.

Only generated public JSON and Markdown reports may enter Git. V1 retains its frozen exact-key schema byte-for-byte. V2 uses a separate exact-key schema for its manifest hash, disposition, attempted/valid/unrun and disjoint failure counters, paired log-cost standard deviation, two cross-arm correlations, confirmatory task-count feasibility, frozen statistical parameters, and total gateway spend. Every additional key is rejected.

## Verify M20-v1 without mutation or provider access

Never run M20-v1 again. To verify its public disposition, remove provider credentials from the command environment, copy the private root to a temporary owner-only location outside Git, and run report/check against the copy. `generate_report` writes private analysis state, so pointing it at the preserved root would violate the evidence boundary.

```bash
V1_MANIFEST="tools/controlled_spend/pilot-manifest.json"
VERIFY_PARENT="$(mktemp -d)"
chmod 700 "$VERIFY_PARENT"
cp -R "$HOME/.local/share/laconic-controlled-spend/m20-pilot" "$VERIFY_PARENT/m20-pilot"

uv run python -m tools.controlled_spend pilot report \
  --manifest "$V1_MANIFEST" \
  --artifact-root "$VERIFY_PARENT/m20-pilot" \
  --output-json "$VERIFY_PARENT/controlled-spend-pilot.json" \
  --output-markdown "$VERIFY_PARENT/controlled-spend-pilot.md"

uv run python -m tools.controlled_spend pilot check \
  --manifest "$V1_MANIFEST" \
  --artifact-root "$VERIFY_PARENT/m20-pilot" \
  --report-json "$VERIFY_PARENT/controlled-spend-pilot.json" \
  --report-markdown "$VERIFY_PARENT/controlled-spend-pilot.md"

cmp "$VERIFY_PARENT/controlled-spend-pilot.json" docs/results/controlled-spend-pilot.json
cmp "$VERIFY_PARENT/controlled-spend-pilot.md" docs/results/controlled-spend-pilot.md
```

The manifest must resolve to `a76c6cb0d2f34737ccd629398b0b2122a3c0a74c63a77055f8112cea602f7b44`; every statistical field must remain null; stored before/after live-state digests must match; and no `credential-snapshot.db*` artifact may exist.

## M20-v1 is closed

The preserved v1 root is `$HOME/.local/share/laconic-controlled-spend/m20-pilot`. Never resume, replace, add, drop, rewrite, or delete a cell or private artifact there. Never use its three invalid attempted cells in M20-v2. The generated [public disposition](results/controlled-spend-pilot.md) is the canonical v1 result.

All three attempted task oracles passed and all three mechanisms engaged. The three cells were still protocol-invalid because every model changed the literal first command; one also attempted a ninth request after eight successful requests. V1's frozen public schema reports 21 unrun cells as completion failures and 3 attempted invalid cells as mechanism failures. Preserve those bytes as history; use the corrected v2 taxonomy only for v2 evidence.

## M20-v2 candidate preflight

After the full readiness stack is available, every command names the v2 manifest explicitly:

```bash
V2_MANIFEST="tools/controlled_spend/pilot-manifest-v2.json"
V2_PRIVATE_ROOT="$HOME/.local/share/laconic-controlled-spend/m20-v2-pilot"

uv sync --locked
uv run python -m tools.controlled_spend manifest check \
  --manifest "$V2_MANIFEST" \
  --verify-oracles
uv run python -m tools.controlled_spend pilot preflight \
  --manifest "$V2_MANIFEST"
laconic-dogfood-check
```

The runner executes `python3 diagnose.py` against each materialized task before OMP starts and requires the frozen failing baseline. This deterministic check is outside provider usage and task-cost accounting. V2 prompts do not require an exact first tool command; later tool choice is measured behavior.

The shared loopback gateway reserves each request's conservative maximum before forwarding. It accepts at most 16 requests per cell, refuses the 17th, retains the 180-second wall limit, charges the reservation if usage is missing or malformed, and never permits cumulative spent plus outstanding reservations to exceed $10.

The committed candidate manifest hash is `526c5de204d39c4c2bb8d9d96bb54163f5caff52e55940467fd036f4f4acf45f`; its `execution_authorized` field remains `false`. The conservative request-size reservation is at most `(16,777,216 input bytes × $4.00/M) + (4,096 output tokens × $10.00/M) = $67.149824` before the campaign cap. Across 24 cells × 16 requests, that uncapped envelope is `$25,785.532416`. The shared gateway instead enforces `spent + outstanding reservations ≤ $10.00`, so the proposed paid campaign cap and maximum campaign commitment are both `$10.00`; any request whose reservation would cross that bound is refused before forwarding.

Preflight, report, and check are no-provider operations and need no authorization receipt. `pilot run` fails closed for M20-v2 unless the operator names a valid external execution-authorization receipt, and fails closed for M20-v1 unconditionally.

## M20-v1 recorded disposition

The authorized campaign ran once and stopped under the frozen invalid-cell rule. The generated [public disposition](results/controlled-spend-pilot.md) is the canonical record of its validity counts and gateway spend. Dispersion, correlations, and confirmatory task count are null. The live OMP and Laconic runtime tree digests matched before and after, the credential snapshot was deleted, and the post-run dogfood check passed.

Do not restart, tune, or salvage this pilot. It supplies no sample-feasibility result and does not authorize a confirmatory run.

## M20-v2 external execution authorization

The manifest's `execution_authorized` field stays `false` forever. It is not a switch. Setting it to `true` would change the manifest bytes and therefore the committed hash `526c5de204d39c4c2bb8d9d96bb54163f5caff52e55940467fd036f4f4acf45f` that the owner reviewed, and `manifest check`, `pilot preflight`, `pilot report`, and `pilot check` all reject a v2 manifest whose value is not exactly `false`. The field records that the study contract does not authorize itself.

Authorization is therefore external to the study. It is an operator capability carried by a private, single-use receipt file that lives outside Git and outside both live-state roots. A receipt cannot override any manifest field, cannot raise the cap, and cannot select a different population, model, task, profile, or limit.

A receipt is a canonically serialized JSON object with exactly these keys:

| Key | Required value |
| --- | --- |
| `schema_version` | `1` |
| `authorization_id` | A 64-character lowercase hexadecimal opaque identifier |
| `study_id` | `m20-variance-pilot-v2` |
| `manifest_sha256` | The exact committed v2 manifest digest |
| `artifact_root` | The absolute canonical private root this receipt authorizes |
| `total_spend_usd` | A decimal string equal to the manifest cap, `10.00` |
| `authorized_at` | An RFC 3339 timestamp |
| `execution_authorized` | `true` |
| `single_use` | `true` |

The receipt must be a regular non-symlink file owned by the invoking user with mode `0600`, inside a directory owned by that user with mode `0700`. It must sit outside the repository working tree, outside the Laconic runtime data directory, outside the OMP agent directory, and outside the artifact root it authorizes.

`pilot run` validates the receipt before it touches credentials, runs the OMP or Headroom preflight, creates the artifact root, or forwards a provider request. It refuses a missing file, a non-canonical or non-object payload, an unknown or missing key, a wrong schema version, a wrong study ID, a wrong manifest digest, a wrong artifact root, a cap that differs from the manifest, a malformed timestamp, `execution_authorized` that is not exactly `true`, `single_use` that is not exactly `true`, a symlink, a wrong owner or mode, a v1 manifest, and an artifact root that already exists.

On acceptance the runner records the receipt's SHA-256 digest and opaque authorization ID in private campaign state before the first provider request, then consumes the receipt by removing it. A consumed receipt cannot authorize a second campaign, and the bound artifact root cannot be reused because an existing root is refused.

M20-v1 execution is permanently closed. `pilot run` refuses every v1 manifest with or without a receipt, while v1 `pilot report` and `pilot check` remain fully operational for historical verification.

## M20-v2 execution boundary

Provider execution requires a fresh owner instruction naming or unambiguously accepting the exact committed v2 manifest hash and maximum reserved-spend calculation. Use only a fresh private root distinct from M20-v1. Never reuse a partial v2 root after any paid result. `V2_AUTHORIZATION` below is the operator's private receipt path; it has no default and no environment fallback.

```bash
uv run python -m tools.controlled_spend pilot run \
  --manifest "$V2_MANIFEST" \
  --authorization "$V2_AUTHORIZATION" \
  --artifact-root "$V2_PRIVATE_ROOT"

uv run python -m tools.controlled_spend pilot report \
  --manifest "$V2_MANIFEST" \
  --artifact-root "$V2_PRIVATE_ROOT" \
  --output-json docs/results/controlled-spend-pilot-v2.json \
  --output-markdown docs/results/controlled-spend-pilot-v2.md

uv run python -m tools.controlled_spend pilot check \
  --manifest "$V2_MANIFEST" \
  --artifact-root "$V2_PRIVATE_ROOT" \
  --report-json docs/results/controlled-spend-pilot-v2.json \
  --report-markdown docs/results/controlled-spend-pilot-v2.md
```

The maximum campaign commitment is `$10.00`. The gateway refuses any request whose reservation would make cumulative spend plus outstanding reservations cross that bound, so no receipt and no invocation can commit more.

There is no retry. Once `pilot run` starts or creates its bound root, it is not run again for that authorization, even if zero provider requests were forwarded. Record the terminal state, preserve the private root for diagnosis, and publish only the exact allowlisted disposition the frozen logic produces. Never soften, repair, resume, or salvage an incomplete campaign.

### Live-state quiescence is a pre-spend gate

The campaign hashes both live-state roots before it starts and again when it finishes, and the frozen `live_state_changed` stopping rule invalidates the whole campaign when the two digests differ. Those roots are the Laconic runtime data directory and the OMP agent directory, and both are shared with whatever agents are running on the machine at the time.

Ordinary local activity mutates them within seconds: the Observe audit log and per-session runtime ledgers under the Laconic data directory, and the agent database write-ahead log, per-session transcripts, and composer status caches under the OMP agent directory. A supervising agent session is itself such a writer, so a campaign supervised from a live coding-agent session cannot satisfy the rule.

Verify quiescence before committing spend by sampling both roots with the runner's own `tree_state_digest` across a window at least as long as the campaign's expected duration, under the same monitoring you intend to use. If the digests move, the campaign will end `incomplete` with `live_state_changed`, every attempted cell will be classified a protocol failure, and the spend will buy no valid cell. Do not start the campaign, and do not narrow or relax the rule to make it pass: the roots it covers are part of the frozen contract.

## Interpretation

For a complete M20-v2 pilot, private analysis computes each task/arm mean over the two repetitions and the paired task log-cost difference `log(mean_laconic) - log(mean_native)`. The public report exposes only the sample standard deviation of those four paired differences and the native/Laconic and native/Headroom cost correlations across four task means.

The preliminary confirmatory task count uses a two-sided normal approximation with alpha 0.05, power 0.80, the precommitted 10% worthwhile reduction, and two repetitions per task. It remains null unless all 24 cells are valid. Changing the repetition count requires a new variance model.

Do not infer direction, magnitude, savings, equivalence, or superiority from either variance pilot. If a complete v2 result makes a confirmatory sample operationally feasible, freeze a new confirmatory protocol and obtain explicit spend authorization before any new provider call.
