"""Render local spend composition, with its limits attached to it.

Everything this module serializes is a count, a dollar figure, a percentage,
a model identifier, or a hash. No session id, path, repository name, prompt,
tool argument, tool result, or file content reaches an artifact, and
:mod:`laconic.spend.privacy` re-checks that independently before anything is
written.

The rendering is deterministic: the same corpus renders byte-identically
twice. There is no generation timestamp, deliberately -- a timestamp would
make the artifact impossible to diff and would be the only thing in it that
is not a measurement.

**This report contains no savings figure and cannot be turned into one.**
Every session it reads ran with the codec enabled. A savings number requires
a comparison against the same work done without it, and no such observation
exists anywhere in this data.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from laconic import __version__
from laconic.costs import CostBreakdown, CostShares, ZeroCostError
from laconic.spend.join import Composition, SessionComposition, codec_activity

#: Bumped whenever a report field is added, removed, or reinterpreted.
REPORT_SCHEMA_VERSION: Final = 1

#: The default output directory, relative to the working directory. Git-ignored:
#: a spend report is local evidence about the owner's own work, not a repository
#: artifact, even though it carries no content.
DEFAULT_OUTPUT_DIR: Final = Path(".laconic/spend")

#: The closed vocabulary of limitations. Every report carries all of them, in
#: this order. They are a fixed set rather than free text so that
#: `laconic.spend.privacy` can verify the report still says what it must:
#: a report that dropped its single-arm caveat would otherwise validate
#: cleanly and read like a savings result.
LIMITATIONS: Final = (
    "single_arm_corpus_every_session_ran_with_the_codec_enabled",
    "no_counterfactual_exists_so_no_savings_figure_can_be_derived",
    "character_reduction_is_not_token_reduction",
    "cost_is_modelled_from_token_counters_never_billed_by_a_provider",
    "sessions_are_not_controlled_units_and_are_not_comparable",
    "a_ledger_only_proves_the_codec_ran_not_that_it_covered_the_session",
    "committed_k1_fixture_8_53_pct_still_bounds_general_savings_claims",
)

#: Exactly the keys a serialized report may carry.
ALLOWED_REPORT_KEYS: Final = frozenset(
    {
        "schema_version",
        "laconic_version",
        "sessions_with_spend",
        "root_sessions",
        "nested_sessions",
        "sessions_without_priced_turns",
        "priced_turns",
        "matched_sessions",
        "unmatched_spend_sessions",
        "unmatched_ledger_sessions",
        "damaged_ledgers",
        "corpus_tokens",
        "corpus_cost",
        "corpus_shares",
        "corpus_host_cost_usd",
        "matched_tokens",
        "matched_cost",
        "matched_shares",
        "matched_host_cost_usd",
        "codec",
        "codec_active_sessions_without_priced_turns",
        "unpriced_models",
        "unknown_usage_keys",
        "sessions",
        "limitations",
    }
)

#: Exactly the keys each per-session entry may carry.
ALLOWED_SESSION_KEYS: Final = frozenset(
    {
        "session_hash",
        "nested",
        "turns",
        "tokens",
        "modelled_cost_usd",
        "host_cost_usd",
        "eligible",
        "emitted",
        "raw_chars",
        "visible_chars",
        "chars_avoided",
        "full_expansions",
        "span_expansions",
    }
)

#: Exactly the keys a token block may carry.
ALLOWED_TOKEN_KEYS: Final = frozenset({"uncached_input", "cache_read", "cache_write", "output"})

#: Exactly the keys a cost or share block may carry.
ALLOWED_COST_KEYS: Final = frozenset(
    {"uncached_input", "cache_read", "cache_write", "output", "total"}
)

#: Exactly the keys the codec block may carry.
ALLOWED_CODEC_KEYS: Final = frozenset(
    {
        "sessions",
        "eligible",
        "emitted",
        "pass_through",
        "raw_chars",
        "visible_chars",
        "chars_avoided",
        "full_expansions",
        "span_expansions",
    }
)


def session_hash(session_id: str) -> str:
    """Return the stable, content-free identifier a report uses for a session.

    A real session id names a live file in the owner's home directory and is
    never serialized. The digest keeps rows distinguishable and stable across
    runs without being a locator.
    """
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _tokens(sessions: tuple[SessionComposition, ...], composition: Composition) -> dict[str, int]:
    counters = composition.usage(sessions)
    return {
        "uncached_input": sum(model.input_tokens for model in counters.values()),
        "cache_read": sum(model.cache_read for model in counters.values()),
        "cache_write": sum(model.cache_write for model in counters.values()),
        "output": sum(model.output_tokens for model in counters.values()),
    }


def _cost(breakdown: CostBreakdown) -> dict[str, float]:
    return {
        "uncached_input": breakdown.uncached_input,
        "cache_read": breakdown.cache_read,
        "cache_write": breakdown.cache_write,
        "output": breakdown.output,
        "total": breakdown.total,
    }


def _shares(breakdown: CostBreakdown) -> dict[str, float] | None:
    """Return the percentage split, or ``None`` when there is nothing to split.

    :meth:`laconic.costs.CostBreakdown.shares` raises rather than reporting
    0.00% four times over an empty corpus, which would hide the emptiness.
    A report over no spend says so by carrying no shares at all.
    """
    try:
        split: CostShares = breakdown.shares()
    except ZeroCostError:
        return None
    return {
        "uncached_input": split.uncached_input,
        "cache_read": split.cache_read,
        "cache_write": split.cache_write,
        "output": split.output,
        "total": split.total,
    }


def _session_entry(session: SessionComposition) -> dict[str, Any]:
    decisions = session.decisions
    assert decisions is not None  # only matched sessions are serialized
    return {
        "session_hash": session_hash(session.session_id),
        "nested": session.nested,
        "turns": session.turns,
        "tokens": {
            "uncached_input": sum(model.input_tokens for model in session.usage.values()),
            "cache_read": sum(model.cache_read for model in session.usage.values()),
            "cache_write": sum(model.cache_write for model in session.usage.values()),
            "output": sum(model.output_tokens for model in session.usage.values()),
        },
        "modelled_cost_usd": session.modelled_cost.total,
        "host_cost_usd": session.host_cost_usd,
        "eligible": decisions.eligible,
        "emitted": decisions.emitted,
        "raw_chars": decisions.raw_chars,
        "visible_chars": decisions.visible_chars,
        "chars_avoided": decisions.chars_avoided,
        "full_expansions": decisions.full_expansions,
        "span_expansions": decisions.span_expansions,
    }


@dataclass(frozen=True, slots=True)
class SpendReport:
    """A rendered composition report and the payload it was rendered from."""

    payload: dict[str, Any]

    def to_json(self) -> str:
        """Serialize deterministically: sorted keys, stable separators.

        ``allow_nan=False`` because Python's default emits a bare ``NaN`` or
        ``Infinity`` token, which is not JSON and which no strict downstream
        parser accepts. A non-finite value should never reach here -- the
        loader and the privacy gate both refuse one -- so this raises rather
        than writing an unparseable artifact.
        """
        return json.dumps(self.payload, indent=2, sort_keys=True, allow_nan=False) + "\n"


def build_report(composition: Composition) -> SpendReport:
    """Turn a join into the content-free payload a report serializes."""
    matched = composition.matched
    priced = composition.priced
    activity = codec_activity(composition)
    payload: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "laconic_version": __version__,
        "sessions_with_spend": len(priced),
        "root_sessions": sum(1 for entry in priced if not entry.nested),
        "nested_sessions": sum(1 for entry in priced if entry.nested),
        "sessions_without_priced_turns": composition.sessions_without_priced_turns,
        "codec_active_sessions_without_priced_turns": sum(
            1 for entry in matched if not entry.turns
        ),
        "priced_turns": sum(entry.turns for entry in priced),
        "matched_sessions": len(matched),
        "unmatched_spend_sessions": len(composition.unmatched_spend_sessions),
        "unmatched_ledger_sessions": len(composition.unmatched_ledger_sessions),
        "damaged_ledgers": composition.damaged_ledgers,
        "corpus_tokens": _tokens(composition.sessions, composition),
        "corpus_cost": _cost(composition.modelled_cost()),
        "corpus_shares": _shares(composition.modelled_cost()),
        "corpus_host_cost_usd": composition.host_cost_usd(),
        "matched_tokens": _tokens(matched, composition),
        "matched_cost": _cost(composition.modelled_cost(matched)),
        "matched_shares": _shares(composition.modelled_cost(matched)),
        "matched_host_cost_usd": composition.host_cost_usd(matched),
        "codec": {
            "sessions": activity.sessions,
            "eligible": activity.eligible,
            "emitted": activity.emitted,
            "pass_through": activity.pass_through,
            "raw_chars": activity.raw_chars,
            "visible_chars": activity.visible_chars,
            "chars_avoided": activity.chars_avoided,
            "full_expansions": activity.full_expansions,
            "span_expansions": activity.span_expansions,
        },
        "unpriced_models": composition.unpriced_models,
        "unknown_usage_keys": sorted(composition.unknown_usage_keys),
        "sessions": [
            _session_entry(session)
            for session in sorted(matched, key=lambda entry: session_hash(entry.session_id))
        ],
        "limitations": list(LIMITATIONS),
    }
    return SpendReport(payload=payload)


_LIMITATION_PROSE: Final = {
    "single_arm_corpus_every_session_ran_with_the_codec_enabled": (
        "Single-arm corpus. Every session measured here ran with the codec enabled."
    ),
    "no_counterfactual_exists_so_no_savings_figure_can_be_derived": (
        "No counterfactual exists in this data. Nothing here is a savings figure, "
        "and no arithmetic over these numbers can produce one."
    ),
    "character_reduction_is_not_token_reduction": (
        "Characters avoided is a character count. It is not tokens, and the "
        "relationship between the two is not measured here."
    ),
    "cost_is_modelled_from_token_counters_never_billed_by_a_provider": (
        "Both dollar figures are modelled from token counters. Providers return "
        "counters, not prices: the host figure is OMP's own price table, the "
        "Laconic figure is laconic.costs. Neither is a bill."
    ),
    "sessions_are_not_controlled_units_and_are_not_comparable": (
        "A session is not a controlled unit. Sessions differ in length, "
        "repository, task, and model, so per-session figures do not compare."
    ),
    "a_ledger_only_proves_the_codec_ran_not_that_it_covered_the_session": (
        "A matched session is one that has a ledger, not one the codec covered "
        "end to end. A long session that the extension only joined partway "
        "through contributes all of its spend and almost none of its codec "
        "activity; compare each row's turns against its eligible count."
    ),
    "committed_k1_fixture_8_53_pct_still_bounds_general_savings_claims": (
        "The committed K1 fixture's 8.53% remains the only bound on a general "
        "savings claim. This report does not move it."
    ),
}


def _usd(value: float) -> str:
    return f"${value:,.2f}"


def _pct(value: float) -> str:
    return f"{value:.2f}%"


def _split_lines(title: str, cost: dict[str, float], shares: dict[str, float] | None) -> list[str]:
    lines = [f"### {title}", "", "| Component | USD | Share |", "| --- | ---: | ---: |"]
    for key, label in (
        ("uncached_input", "Uncached input"),
        ("cache_read", "Cache read"),
        ("cache_write", "Cache write"),
        ("output", "Output"),
    ):
        share = "n/a" if shares is None else _pct(shares[key])
        lines.append(f"| {label} | {_usd(cost[key])} | {share} |")
    lines.append(f"| **Total** | **{_usd(cost['total'])}** | |")
    lines.append("")
    return lines


def render_markdown(report: SpendReport) -> str:
    """Render the report deterministically as Markdown."""
    payload = report.payload
    codec = payload["codec"]
    lines: list[str] = [
        "# Local spend composition",
        "",
        "Composition of the owner's own model spend, joined to what the runtime",
        "codec did in those same sessions. **This report makes no savings claim",
        "and contains no savings figure.**",
        "",
        f"Generated by laconic {payload['laconic_version']}, report schema "
        f"v{payload['schema_version']}.",
        "",
        "## Limitations",
        "",
    ]
    lines += [f"- {_LIMITATION_PROSE[name]}" for name in payload["limitations"]]
    lines += [
        "",
        "## Corpus",
        "",
        f"- Sessions with priced turns: {payload['sessions_with_spend']} "
        f"({payload['root_sessions']} root, {payload['nested_sessions']} subagent)",
        f"- Priced assistant turns: {payload['priced_turns']}",
        f"- Transcripts with no priced turn: {payload['sessions_without_priced_turns']}"
        f" (of which {payload['codec_active_sessions_without_priced_turns']} still recorded "
        f"codec activity)",
        "",
        "## Join",
        "",
        f"- Sessions with both spend and a runtime ledger: {payload['matched_sessions']}",
        f"- Sessions with spend but no ledger: {payload['unmatched_spend_sessions']}",
        f"- Ledgers with no matching transcript: {payload['unmatched_ledger_sessions']}",
        f"- Ledgers with an unreadable schema: {payload['damaged_ledgers']}",
        "",
        "## Where the money went",
        "",
    ]
    lines += _split_lines(
        "Whole corpus (laconic.costs)", payload["corpus_cost"], payload["corpus_shares"]
    )
    lines += [
        f"Host-reported total for the same turns: {_usd(payload['corpus_host_cost_usd'])}.",
        "",
    ]
    lines += _split_lines(
        "Sessions the codec was active in (laconic.costs)",
        payload["matched_cost"],
        payload["matched_shares"],
    )
    lines += [
        f"Host-reported total for the same turns: {_usd(payload['matched_host_cost_usd'])}.",
        "",
        "## What the codec did in those sessions",
        "",
        f"- Sessions: {codec['sessions']}",
        f"- Eligible observations: {codec['eligible']}",
        f"- Replaced: {codec['emitted']}; passed through: {codec['pass_through']}",
        f"- Characters raw: {codec['raw_chars']:,}; visible: {codec['visible_chars']:,}; "
        f"avoided: {codec['chars_avoided']:,}",
        f"- Expansions: {codec['full_expansions']} full, {codec['span_expansions']} span",
        "",
        "Counted over every session with a runtime ledger, including any that "
        "recorded no billable turn.",
    ]
    if payload["unpriced_models"]:
        lines += [
            "## Models with no published list price",
            "",
            "Billed at the laconic.costs fallback rate, so the Laconic figure and "
            "the host's own figure necessarily differ for them.",
            "",
        ]
        lines += [f"- `{model}`" for model in payload["unpriced_models"]]
        lines.append("")
    if payload["unknown_usage_keys"]:
        lines += [
            "## Host usage keys this reader does not model",
            "",
        ]
        lines += [f"- `{key}`" for key in payload["unknown_usage_keys"]]
        lines.append("")
    if payload["sessions"]:
        lines += [
            "## Joined sessions",
            "",
            "| Session | Turns | Modelled USD | Host USD | Eligible | Replaced | Chars avoided |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for entry in payload["sessions"]:
            lines.append(
                f"| `{entry['session_hash'][:12]}` | {entry['turns']} | "
                f"{_usd(entry['modelled_cost_usd'])} | {_usd(entry['host_cost_usd'])} | "
                f"{entry['eligible']} | {entry['emitted']} | {entry['chars_avoided']:,} |"
            )
        lines.append("")
    return "\n".join(lines)
