"""Join what a session cost to what the codec did in that same session.

**Unit of analysis.** One joined record is one *host session id*. That is the
only key the two sides share: OMP writes it into the session transcript, and
the runtime engine writes it into every `runtime_decisions` row. Nothing
finer is available honestly. Token usage is per turn and each decision
carries a timestamp, so a turn-level alignment is *mechanically* possible --
and it is deliberately not done here, because charging a particular
cache-write to a particular compression decision is a counterfactual, and
this package has no counterfactual to draw on.

**What a joined record supports.** Describing how one real session's spend
divided across the four components a provider bills, and how much codec
activity happened in that same session.

**What it does not support.** Comparing sessions as if they were
interchangeable, ranking them, extrapolating, or attributing any difference
in spend to the codec. Sessions differ in length, repository, task, model,
and in how much of their spend was incurred before the codec ever saw an
eligible observation. Every session on disk ran with the codec enabled, so
the corpus is single-arm: there is no codec-off session anywhere in it, and
therefore no savings figure can be computed from it. No function in this
module computes a difference against a hypothetical.

**Two cost models, deliberately.** ``host_cost_usd`` is what OMP itself
recorded. Providers return token counters, not dollars, so that figure is
the host's own price table applied to real counters -- an external input,
reported with its provenance. ``modelled_cost`` is :mod:`laconic.costs`
applied to those same counters, which is the single pricing convention this
repository owns. They differ when a model is absent from every layer of the
price registry :mod:`laconic.pricing.registry` resolves, and
:attr:`Composition.unpriced_models` names every model for which that
happened. They also differ wherever the host's own table and the registry
disagree, which on a real corpus they do: a host may charge a rate no list
price publishes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from laconic.costs import CostBreakdown, ModelUsage, session_cost, unpriced_models
from laconic.spend.ledger import SessionDecisions
from laconic.spend.omp import SessionUsage


@dataclass(frozen=True, slots=True)
class SessionComposition:
    """One host session's spend, and the codec activity recorded beside it.

    Attributes:
        session_id: The host session id both sides agreed on.
        nested: Whether this is a subagent's own session (see
            :class:`laconic.spend.omp.SessionUsage`).
        usage: Token counters accumulated per model.
        host_cost_usd: What the host itself recorded this session cost.
        reports_host_cost: Whether this session's host records a cost at
            all. See :class:`laconic.spend.omp.SessionUsage`.
        decisions: The codec's record for this session, or ``None`` when the
            session ran without a runtime ledger -- which is the normal state
            for any session predating the extension or running in a profile
            it was never installed into.
    """

    session_id: str
    nested: bool
    usage: Mapping[str, ModelUsage]
    host_cost_usd: float
    reports_host_cost: bool
    decisions: SessionDecisions | None

    @property
    def modelled_cost(self) -> CostBreakdown:
        """This session's spend under :mod:`laconic.costs`."""
        return session_cost(self.usage)

    @property
    def turns(self) -> int:
        """Priced assistant turns in this session."""
        return sum(model.turns for model in self.usage.values())


@dataclass(frozen=True, slots=True)
class Composition:
    """Every scanned session, plus every ledger that matched none of them.

    Attributes:
        sessions: One entry per scanned transcript, whether or not it
            recorded a billable turn. A session with no priced turns is kept
            because it can still carry a ledger full of codec decisions.
        unmatched_ledger_sessions: Session ids the runtime store holds a
            ledger for, but which no scanned transcript claims. A session run
            under a different OMP profile, or whose transcript has been
            deleted, lands here. Reported rather than dropped, because a
            silently discarded ledger would make codec activity look smaller
            than it was.
        damaged_ledgers: Ledgers whose schema a killed initialize destroyed.
        unknown_usage_keys: Usage keys the reader does not model, by name,
            unioned across the corpus. Non-empty means host schema drift.
    """

    sessions: tuple[SessionComposition, ...]
    unmatched_ledger_sessions: tuple[str, ...]
    damaged_ledgers: int
    unknown_usage_keys: frozenset[str] = field(default_factory=frozenset)

    @property
    def priced(self) -> tuple[SessionComposition, ...]:
        """Sessions that recorded at least one billable turn."""
        return tuple(session for session in self.sessions if session.turns)

    @property
    def sessions_without_priced_turns(self) -> int:
        """Scanned transcripts that parsed cleanly but recorded no billable turn."""
        return len(self.sessions) - len(self.priced)

    @property
    def host_reporting(self) -> tuple[SessionComposition, ...]:
        """Priced sessions whose host records a per-turn cost of its own.

        The set ``host_cost_usd`` can actually cover. A Claude Code session
        records token counters and no cost, so it contributes modelled
        dollars and nothing to the host total; summing every session's
        modelled cost and comparing it to the host total therefore compares
        two different corpora. This property is what makes the two sides of
        that comparison name the same sessions.
        """
        return tuple(session for session in self.priced if session.reports_host_cost)

    @property
    def matched(self) -> tuple[SessionComposition, ...]:
        """Sessions that carry a runtime ledger, priced or not."""
        return tuple(session for session in self.sessions if session.decisions is not None)

    @property
    def unmatched_spend_sessions(self) -> tuple[SessionComposition, ...]:
        """Sessions with spend but no runtime ledger."""
        return tuple(session for session in self.priced if session.decisions is None)

    def usage(self, sessions: Iterable[SessionComposition] | None = None) -> dict[str, ModelUsage]:
        """Accumulate per-model token counters across ``sessions``."""
        totals: dict[str, ModelUsage] = {}
        for session in self.sessions if sessions is None else sessions:
            for model, counters in session.usage.items():
                current = totals.get(model, ModelUsage())
                totals[model] = ModelUsage(
                    turns=current.turns + counters.turns,
                    input_tokens=current.input_tokens + counters.input_tokens,
                    cache_read=current.cache_read + counters.cache_read,
                    cache_write=current.cache_write + counters.cache_write,
                    output_tokens=current.output_tokens + counters.output_tokens,
                )
        return totals

    def modelled_cost(self, sessions: Iterable[SessionComposition] | None = None) -> CostBreakdown:
        """Spend under :mod:`laconic.costs` across ``sessions``."""
        return session_cost(self.usage(sessions))

    def host_cost_usd(self, sessions: Iterable[SessionComposition] | None = None) -> float:
        """Spend as the host itself reported it across ``sessions``."""
        return sum(
            session.host_cost_usd for session in (self.sessions if sessions is None else sessions)
        )

    @property
    def unpriced_models(self) -> list[str]:
        """Models billed at the fallback price because no list price is known."""
        return unpriced_models(self.usage())

    def fallback_priced_cost_share(
        self, sessions: Iterable[SessionComposition] | None = None
    ) -> float:
        """Fraction of modelled cost attributed to models with no list price.

        Naming the unpriced models is not enough on its own: a reader has
        no way to tell whether they are a rounding error or most of the
        bill. On the development corpus they are 44% of it, with one model
        charged at Sonnet rates while really costing well over them.

        The share matters more than the names because the estimate divides
        its per-token rates out of this same cost, so a large fallback
        share means the rates -- and therefore the dollar band -- are built
        on prices nobody published.
        """
        usage = self.usage(sessions)
        total = session_cost(usage).total
        if total <= 0:
            return 0.0
        unpriced = set(unpriced_models(usage))
        if not unpriced:
            return 0.0
        fallback = session_cost({m: u for m, u in usage.items() if m in unpriced}).total
        return fallback / total


@dataclass(frozen=True, slots=True)
class CodecActivity:
    """Codec counters summed over the sessions that carry a ledger."""

    sessions: int
    eligible: int
    emitted: int
    raw_chars: int
    visible_chars: int
    full_expansions: int
    span_expansions: int

    @property
    def pass_through(self) -> int:
        """Eligible observations the codec left untouched."""
        return self.eligible - self.emitted

    @property
    def chars_avoided(self) -> int:
        """Characters the model did not see. Not tokens, and not a saving."""
        return self.raw_chars - self.visible_chars


def codec_activity(composition: Composition) -> CodecActivity:
    """Sum the codec's own counters over every matched session."""
    matched = composition.matched
    decisions = [session.decisions for session in matched if session.decisions is not None]
    return CodecActivity(
        sessions=len(matched),
        eligible=sum(row.eligible for row in decisions),
        emitted=sum(row.emitted for row in decisions),
        raw_chars=sum(row.raw_chars for row in decisions),
        visible_chars=sum(row.visible_chars for row in decisions),
        full_expansions=sum(row.full_expansions for row in decisions),
        span_expansions=sum(row.span_expansions for row in decisions),
    )


class DuplicateSessionError(ValueError):
    """Raised when two transcripts claim the same host session id.

    One session id must name one transcript. If two did, both would be
    joined against the same ledger and every codec counter, token total, and
    dollar figure derived from them would be counted twice with no caveat.
    Failing is right: the corpus is not what the join assumes.
    """


def join(
    usage: Iterable[SessionUsage],
    decisions: Iterable[SessionDecisions],
    *,
    damaged_ledgers: int = 0,
) -> Composition:
    """Join host spend to codec decisions on the host session id.

    Every scanned transcript becomes one entry, including one that recorded
    no billable turn: such a session can still carry a ledger full of codec
    decisions, and dropping it would both mislabel its ledger as having no
    transcript and silently shrink every codec total.

    Neither side is authoritative over the other. A session with spend and no
    ledger keeps its spend. A ledger with no scanned transcript is named in
    :attr:`Composition.unmatched_ledger_sessions`. Nothing is discarded for
    failing to match, because both directions are ordinary and both change
    how the totals should be read.

    Raises:
        DuplicateSessionError: if two transcripts claim one session id.
    """
    by_session = {row.session_id: row for row in decisions}
    sessions: list[SessionComposition] = []
    unknown: set[str] = set()
    seen: set[str] = set()

    for record in usage:
        unknown |= record.unknown_usage_keys
        if record.session_id in seen:
            raise DuplicateSessionError(
                "two transcripts claim one session id; joining both against the same "
                "ledger would double every counter derived from it"
            )
        seen.add(record.session_id)
        counters: dict[str, ModelUsage] = {}
        for turn in record.turns:
            counters[turn.model] = counters.get(turn.model, ModelUsage()).add_turn(
                input_tokens=turn.input_tokens,
                cache_read=turn.cache_read,
                cache_write=turn.cache_write,
                output_tokens=turn.output_tokens,
            )
        sessions.append(
            SessionComposition(
                session_id=record.session_id,
                nested=record.nested,
                usage=counters,
                host_cost_usd=sum(turn.host_cost_usd for turn in record.turns),
                reports_host_cost=record.reports_host_cost,
                decisions=by_session.get(record.session_id),
            )
        )

    return Composition(
        sessions=tuple(sorted(sessions, key=lambda entry: entry.session_id)),
        unmatched_ledger_sessions=tuple(sorted(set(by_session) - seen)),
        damaged_ledgers=damaged_ledgers,
        unknown_usage_keys=frozenset(unknown),
    )
