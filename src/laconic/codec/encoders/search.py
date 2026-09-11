"""Search observation encoder: path interning and tabular output.

A search-shaped tool result (``Grep``, ``Glob``) is a list of matches, most
of them naming one of a small set of paths repeatedly — a file with five
matches repeats its own path five times in the raw text. This encoder
interns each distinct path once into a short local reference (``p0``,
``p1``, ...) and renders every match as a compact ``pN[:line]  text`` row
against that legend, instead of repeating the full path on every line.

Like :class:`~laconic.codec.encoders.command.CommandEncoder`, this encoder
elides the middle of a long match list: the head and tail rows are kept
verbatim and the span between them is replaced by one count line. Path
interning alone does not reliably shrink a match list: it recovers the
repeated path bytes but pays for a legend, so it only wins when a few
paths each repeat many times. A typical result is dominated by paths
that appear once or twice, where the legend costs more than the
repetition saves — measured across the local dogfood ledgers, un-elided
encodings averaged slightly *larger* than their input and the runtime's
strictly-smaller rule passed almost all of them through. Elision is what
makes a long match list compressible in the common case.

The head and tail widths are the codec's shared ``keep_head``/``keep_tail``
rather than search-specific values: a match list is long enough that the
same widths the command encoder uses already leave most results untouched
and still elide the long tail. The legend lists every matched path
regardless of how many rows are elided, so eliding never hides *which*
files matched, and the header's hit count still reports how many matches
exist. The three elision rules in ``docs/system-design.md`` §2.2 are
satisfied the same way the command encoder satisfies them: head and tail
survive verbatim, error-shaped lines in the elided middle are surfaced
explicitly, and the full text remains exactly recoverable through the
ledger handle.

A candidate path is only interned when it *looks like* a path: no
whitespace, and either a ``/``/``\\`` separator or a dotted extension. A
bare pre-colon word with neither (``rg: ...`` diagnostics, a prose header
ending in ``: something``) is left verbatim rather than misread as a path
— without this check every colon-bearing line becomes a spurious "hit"
against a garbage single-word "path", inflating both the hit count and the
legend. An optional Windows drive-letter prefix (``C:``) is folded into
the path itself, not treated as the field delimiter, so
``C:\\src\\app.py:12:text`` interns the whole path rather than just ``C``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from laconic.codec.encoders._elision import (
    DEFAULT_KEEP_HEAD,
    DEFAULT_KEEP_TAIL,
    DEFAULT_MAX_ERRORS,
    elide_middle,
)
from laconic.ledger import Ledger, ObservationKind, Record

#: See ``laconic.codec.encoders.file._LONE_SURROGATE``: a lone UTF-16
#: surrogate is a legal ``str`` code point but not valid UTF-8, and the
#: ledger's ``subject``/``encoded`` columns must be storable UTF-8 text.
_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")


def _storable(text: str) -> str:
    return _LONE_SURROGATE.sub("\ufffd", text)


#: An optional Windows drive-letter prefix, folded into the path rather
#: than read as the ``path:line:text`` delimiter.
_DRIVE = r"(?:[A-Za-z]:)?"

#: A path-shaped run of non-whitespace, non-``:`` characters: either it
#: contains a ``/``/``\\`` separator, or it ends in a dotted extension.
#: Neither alternative matches a bare word like ``rg`` or ``Search``, so a
#: diagnostic or prose line ahead of a colon is never mistaken for a path.
_PATH_LIKE = rf"{_DRIVE}[^\s:]*[/\\][^\s:]*|{_DRIVE}[^\s:]*\.[A-Za-z0-9]{{1,6}}"

#: ripgrep/grep ``-n`` shape: ``path:line:text``.
_PATH_LINE_TEXT = re.compile(rf"^(?P<path>{_PATH_LIKE}):(?P<line>\d+):(?P<text>.*)$")

#: A path-scoped message with no line number, e.g. ``path/to/file.py: ok``.
_PATH_TEXT = re.compile(rf"^(?P<path>{_PATH_LIKE}):(?P<text>.*)$")

#: A bare path with no trailing message at all — ``Glob``'s output shape,
#: one matched path per line, with no ``:`` in sight.
_BARE_PATH = re.compile(rf"^(?P<path>{_PATH_LIKE})$")


@dataclass(frozen=True, slots=True)
class _Entry:
    """One line of raw search output, parsed if its shape allows it."""

    line: str
    path: str | None
    line_no: int | None
    text: str | None


def _parse_entry(line: str) -> _Entry:
    if match := _PATH_LINE_TEXT.match(line):
        return _Entry(line, match["path"], int(match["line"]), match["text"].lstrip(" "))
    if match := _PATH_TEXT.match(line):
        return _Entry(line, match["path"], None, match["text"].lstrip(" "))
    if match := _BARE_PATH.match(line):
        return _Entry(line, match["path"], None, "")
    return _Entry(line, None, None, None)


def _render(
    subject: str,
    entries: list[_Entry],
    paths: dict[str, int],
    *,
    keep_head: int,
    keep_tail: int,
    max_errors: int,
) -> str:
    hit_count = sum(1 for entry in entries if entry.path is not None)
    parts = [f"{subject}  {hit_count} hits, {len(paths)} files"]
    if paths:
        legend = " ".join(f"p{index}={path}" for path, index in paths.items())
        parts.append(f"  paths: {legend}")
    rows = []
    for entry in entries:
        if entry.path is None:
            rows.append(entry.line)
            continue
        index = paths[entry.path]
        ref = f"p{index}:{entry.line_no}" if entry.line_no is not None else f"p{index}"
        rows.append(f"  {ref}  {entry.text}" if entry.text else f"  {ref}")
    elided = elide_middle(rows, keep_head=keep_head, keep_tail=keep_tail, max_errors=max_errors)
    parts.append(elided.text)
    return "\n".join(parts)


class SearchEncoder:
    """Path-interned, tabular encoding of search-shaped output.

    Every call registers the encoding with ``ledger`` under
    :attr:`~laconic.ledger.ObservationKind.SEARCH` and returns the
    resulting :class:`~laconic.ledger.Record`; ``encode`` never raises
    regardless of ``raw``'s content.
    """

    def __init__(
        self,
        ledger: Ledger,
        *,
        keep_head: int = DEFAULT_KEEP_HEAD,
        keep_tail: int = DEFAULT_KEEP_TAIL,
        max_errors: int = DEFAULT_MAX_ERRORS,
    ) -> None:
        self._ledger = ledger
        self._keep_head = keep_head
        self._keep_tail = keep_tail
        self._max_errors = max_errors

    def encode(
        self,
        subject: str,
        raw: str,
        request: Mapping[str, object],
        *,
        turn: int,
    ) -> Record:
        del request  # no request-carried hints are defined for search results
        entries = [_parse_entry(line) for line in raw.split("\n")]
        paths: dict[str, int] = {}
        for entry in entries:
            if entry.path is not None and entry.path not in paths:
                paths[entry.path] = len(paths)
        encoded = _render(
            subject,
            entries,
            paths,
            keep_head=self._keep_head,
            keep_tail=self._keep_tail,
            max_errors=self._max_errors,
        )
        return self._ledger.register(
            ObservationKind.SEARCH, _storable(subject), raw, _storable(encoded), turn
        )
