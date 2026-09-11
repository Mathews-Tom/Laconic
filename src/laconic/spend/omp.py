"""Read per-turn model usage out of OMP session transcripts.

An OMP session is one JSONL file under a profile's ``sessions/<project>/``
directory. Every record is a JSON object with a top-level ``type``. Two of
those types matter here and no others do:

``session``
    Carries the session's own ``id`` -- the same identifier the runtime
    ledger stores in ``runtime_decisions.session_id``, and therefore the
    only key the two sides can be joined on. It also carries ``cwd``, which
    this module never reads: a working directory is a filesystem path.

``message`` with ``message.role == "assistant"``
    Carries ``message.usage``, the host's per-turn token counters and its
    own modelled cost for that turn.

Everything else -- user prompts, tool results, tool arguments, titles, mode
changes -- is skipped without being parsed for content. The parser reaches
into exactly five leaf positions: ``type``, ``id``, ``message.role``,
``message.model``, ``message.provider``, and ``message.usage``.

Two properties of the ``usage`` object are checked rather than assumed,
because both are the kind of thing a host release can change underneath a
reader that then keeps reporting confidently wrong numbers:

* ``totalTokens`` must equal ``input + output + cacheRead + cacheWrite``.
  This is what detects a *new billable token class*. An unknown key in the
  usage object is not by itself an error -- ``cttl`` and ``reasoningTokens``
  are both already present and neither is billed separately -- but a new
  key that carries its own charge would break this identity, and that is
  the case worth failing on.
* ``cost.total`` must equal the sum of its four components.

The cost this module reads is the *host's* number, not the provider's.
Anthropic and OpenAI return token counters; they do not return dollars. OMP
applies its own price table to those counters. That is why the figure is
carried in a field named ``host_cost_usd`` and never simply "cost", and why
:mod:`laconic.spend.join` reports Laconic's own :mod:`laconic.costs` model
of the same counters beside it.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

#: Session-transcript files, as OMP names them: ``<timestamp>_<uuid>.jsonl``.
TRANSCRIPT_GLOB: Final = "*.jsonl"

#: ``usage`` keys that must be present and integral on every assistant turn.
#: A missing one is a host schema change this reader has not reviewed, so it
#: raises instead of contributing a silent zero.
_REQUIRED_TOKEN_KEYS: Final = ("input", "output", "cacheRead", "cacheWrite")

#: ``cost`` keys that must be present and numeric, mirroring the token keys.
_REQUIRED_COST_KEYS: Final = ("input", "output", "cacheRead", "cacheWrite")

#: Usage keys this reader accounts for. ``cttl`` is cache time-to-live
#: metadata and ``reasoningTokens`` is a breakdown of ``output``, already
#: billed inside it -- neither is a separate charge, which the
#: ``totalTokens`` identity independently confirms on every turn.
_MODELLED_USAGE_KEYS: Final = frozenset(
    {"input", "output", "cacheRead", "cacheWrite", "totalTokens", "cost", "cttl", "reasoningTokens"}
)

#: Modelled dollars compare within this much. Host costs are sums of
#: floating-point products, so exact equality is the wrong test; anything
#: larger than this is an accounting difference, not representation error.
COST_TOLERANCE_USD: Final = 1e-9


class MalformedSessionError(ValueError):
    """Raised when a transcript cannot be read as an OMP session.

    The message names the failing transcript's filename and line, because a
    person debugging their own corpus needs to find the file. OMP names a
    transcript ``<timestamp>_<session-id>.jsonl``, so that filename *is* the
    unhashed session id -- the identifier
    :func:`laconic.spend.report.session_hash` exists to keep out of
    artifacts. This message is therefore for a local terminal only. It must
    never be serialized, written to a shared destination, or embedded in a
    report. The containing directory is still excluded, so the message never
    reveals where the owner works.
    """


@dataclass(frozen=True, slots=True)
class TurnUsage:
    """One assistant turn's token counters and the host's modelled cost.

    Counters are per-turn deltas, not running totals: turn *n*'s
    ``cache_read`` is the prefix that turn re-read, and summing the four
    fields across a session is the session's real token volume.
    """

    model: str
    provider: str
    input_tokens: int
    cache_read: int
    cache_write: int
    output_tokens: int
    host_cost_usd: float

    @property
    def total_tokens(self) -> int:
        """Tokens this turn billed across all four components."""
        return self.input_tokens + self.cache_read + self.cache_write + self.output_tokens


@dataclass(frozen=True, slots=True)
class SessionUsage:
    """Every priced turn one OMP session recorded.

    Attributes:
        session_id: The host's own session identifier, which the runtime
            ledger also stores. The join key, and never serialized as-is.
        turns: One entry per assistant turn that carried a usage object.
        turns_without_usage: Assistant turns that carried none. A turn
            interrupted before the provider replied leaves such a record;
            counting them keeps "this session has fewer priced turns than
            replies" visible rather than looking like a parse failure.
        malformed_lines: Unparseable lines. A live session's last line is
            routinely a partial write; see :func:`load_session`.
        unknown_usage_keys: Usage keys this reader does not model, by name.
            Reported so host schema drift is visible even when the token
            identity still holds.
        nested: Whether this transcript lives inside another session's
            directory. OMP gives a subagent its own session id and its own
            transcript, filed under the session that spawned it, to any
            depth -- a subagent's subagent nests again. Its spend is its
            own: a parent's usage records cover only the parent's turns.
            Counting a root session and its subagents as peers would
            inflate the session count and make any per-session average
            meaningless, so the two are kept distinguishable.
    """

    session_id: str
    turns: tuple[TurnUsage, ...]
    turns_without_usage: int
    malformed_lines: int
    unknown_usage_keys: frozenset[str]
    nested: bool = False
    reports_host_cost: bool = True
    """Whether this session's host records a per-turn cost at all.

    A capability of the host, not a property of the number. An OMP session
    whose turns genuinely price to ``$0.00`` still reports cost; a Claude
    Code session records token counters and no cost whatever its spend was.
    Inferring the difference from ``host_cost_usd == 0.0`` would conflate
    the two.
    """


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, dict) else None


def _token_count(field: str, value: Any, *, origin: str) -> int:
    # bool is an int subclass, and `True` silently counting as one token is
    # exactly the kind of coercion this reader exists to refuse.
    if not isinstance(value, int) or isinstance(value, bool):
        raise MalformedSessionError(f"{origin}: usage.{field} is not an integer")
    if value < 0:
        raise MalformedSessionError(f"{origin}: usage.{field} is negative")
    return value


def _usd(field: str, value: Any, *, origin: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MalformedSessionError(f"{origin}: usage.cost.{field} is not a number")
    # json.loads accepts bare NaN and Infinity. NaN would then satisfy the
    # cost identity below for free -- every comparison with NaN is False --
    # and propagate into a total that json.dumps writes as a literal NaN,
    # which is not valid JSON for any downstream reader.
    if not math.isfinite(value):
        raise MalformedSessionError(f"{origin}: usage.cost.{field} is not a finite number")
    return float(value)


def _parse_usage(raw: Mapping[str, Any], *, origin: str) -> tuple[dict[str, int], float]:
    """Return ``(token counters, host cost)`` for one validated usage object."""
    counters = {key: _token_count(key, raw.get(key), origin=origin) for key in _REQUIRED_TOKEN_KEYS}

    declared = raw.get("totalTokens")
    total = _token_count("totalTokens", declared, origin=origin)
    if total != sum(counters.values()):
        raise MalformedSessionError(
            f"{origin}: usage.totalTokens is {total}, but the four billed components sum to "
            f"{sum(counters.values())}. The host is billing a token class this reader does "
            f"not count; adding it up as-is would understate the turn."
        )

    cost = _as_mapping(raw.get("cost"))
    if cost is None:
        raise MalformedSessionError(f"{origin}: usage.cost is missing or not an object")
    components = {key: _usd(key, cost.get(key), origin=origin) for key in _REQUIRED_COST_KEYS}
    declared_total = _usd("total", cost.get("total"), origin=origin)
    if abs(declared_total - sum(components.values())) > COST_TOLERANCE_USD:
        raise MalformedSessionError(
            f"{origin}: usage.cost.total is {declared_total}, but its four components sum to "
            f"{sum(components.values())}"
        )
    return counters, declared_total


def _iter_records(path: Path) -> Iterator[tuple[int, bool, Any]]:
    """Yield ``(line number, is last line, parsed record or None)``.

    Streams with a one-record lookahead rather than buffering the file: the
    real corpus already holds transcripts in the tens of megabytes, and the
    only reason to know where the end is is to tolerate a live session's
    partial final write. Blank trailing lines are not records, so a partial
    record followed by a newline is still recognised as the last one.
    """
    pending: tuple[int, Any] | None = None
    with path.open(encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed: Any = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if pending is not None:
                yield pending[0], False, pending[1]
            pending = (number, parsed)
    if pending is not None:
        yield pending[0], True, pending[1]


def load_session(path: Path, *, nested: bool = False) -> SessionUsage:
    """Read one OMP transcript into a :class:`SessionUsage`.

    A partial final line is tolerated and counted: a session being written
    right now ends mid-record, and refusing to read the owner's live session
    would make this tool unusable during exactly the work it measures. A
    partial line anywhere *earlier* is a damaged file and raises, because
    every record after it may have been lost too.

    Raises:
        MalformedSessionError: if the file carries no ``session`` record, if
            a record before the last is unparseable, or if any assistant
            turn's usage object fails validation.
    """
    session_id: str | None = None
    turns: list[TurnUsage] = []
    without_usage = 0
    malformed = 0
    unknown: set[str] = set()

    for number, is_last, record in _iter_records(path):
        origin = f"{path.name}:{number}"
        if record is None:
            if not is_last:
                raise MalformedSessionError(
                    f"{origin}: unparseable record before the end of the file"
                )
            malformed += 1
            continue
        entry = _as_mapping(record)
        if entry is None:
            raise MalformedSessionError(f"{origin}: record is not a JSON object")

        kind = entry.get("type")
        if kind == "session":
            found = entry.get("id")
            if not isinstance(found, str) or not found:
                raise MalformedSessionError(f"{origin}: session record has no string id")
            if session_id is not None and session_id != found:
                raise MalformedSessionError(
                    f"{origin}: transcript declares two different session ids"
                )
            session_id = found
            continue
        if kind != "message":
            continue

        message = _as_mapping(entry.get("message"))
        if message is None or message.get("role") != "assistant":
            continue
        raw_usage = message.get("usage")
        if raw_usage is None:
            without_usage += 1
            continue
        usage = _as_mapping(raw_usage)
        if usage is None:
            raise MalformedSessionError(f"{origin}: usage is not an object")

        counters, host_cost = _parse_usage(usage, origin=origin)
        unknown |= set(usage) - _MODELLED_USAGE_KEYS
        model = message.get("model")
        provider = message.get("provider")
        if not isinstance(model, str) or not model:
            raise MalformedSessionError(f"{origin}: assistant turn has no model identifier")
        if not isinstance(provider, str) or not provider:
            raise MalformedSessionError(f"{origin}: assistant turn has no provider identifier")
        turns.append(
            TurnUsage(
                model=model,
                provider=provider,
                input_tokens=counters["input"],
                cache_read=counters["cacheRead"],
                cache_write=counters["cacheWrite"],
                output_tokens=counters["output"],
                host_cost_usd=host_cost,
            )
        )

    if session_id is None:
        raise MalformedSessionError(f"{path.name}: no session record; not an OMP transcript")
    return SessionUsage(
        session_id=session_id,
        turns=tuple(turns),
        turns_without_usage=without_usage,
        malformed_lines=malformed,
        unknown_usage_keys=frozenset(unknown),
        nested=nested,
    )


def find_transcripts(roots: Sequence[Path]) -> list[tuple[Path, bool]]:
    """Return ``(transcript, nested)`` for every session under ``roots``.

    OMP files one directory per project and one transcript per session
    directly inside it. A subagent gets its own session, whose transcript is
    filed inside its spawning session's own directory -- so anything deeper
    than ``<root>/<project>/<session>.jsonl`` is a nested session. Depth is
    the only reliable signal: nesting goes arbitrarily deep and the
    intermediate directory is sometimes named after the agent rather than
    after a session id, so a parent id cannot be recovered from the path.

    A path naming a file directly is taken as one non-nested transcript.
    """
    found: dict[Path, bool] = {}
    for root in roots:
        if root.is_file():
            found[root] = False
        elif root.is_dir():
            for path in root.rglob(TRANSCRIPT_GLOB):
                if path.is_file():
                    found[path] = path.parent.parent != root
    return sorted(found.items())


def load_sessions(roots: Sequence[Path]) -> list[SessionUsage]:
    """Read every transcript under ``roots``.

    Raises:
        MalformedSessionError: propagated from :func:`load_session`. A
            corpus scan does not swallow a damaged transcript: a reader that
            skips what it cannot understand reports a smaller number with
            the same confidence as a complete one.
    """
    return [load_session(path, nested=nested) for path, nested in find_transcripts(roots)]
