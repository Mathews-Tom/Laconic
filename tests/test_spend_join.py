"""Composition arithmetic holds, and nothing that fails to join disappears."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from laconic.costs import SHARE_TOLERANCE_PCT, ZeroCostError
from laconic.runtime.operator import open_query_only
from laconic.runtime.storage import RuntimeStorage
from laconic.spend.join import (
    Composition,
    DuplicateSessionError,
    codec_activity,
    join,
)
from laconic.spend.ledger import SessionDecisions, scan_store
from laconic.spend.omp import SessionUsage, TurnUsage

MATCHED = "01a078c0-15c2-7000-9389-19db9037f833"
SPEND_ONLY = "01a06d0f-6060-7000-94fc-3301f336bf8b"
LEDGER_ONLY = "01a069ed-abc1-7000-bb87-726652030a22"


def _turn(
    *,
    model: str = "claude-opus-4-8",
    input_tokens: int = 4,
    cache_read: int = 0,
    cache_write: int = 87173,
    output: int = 218,
    host_cost: float = 0.55030125,
) -> TurnUsage:
    return TurnUsage(
        model=model,
        provider="anthropic",
        input_tokens=input_tokens,
        cache_read=cache_read,
        cache_write=cache_write,
        output_tokens=output,
        host_cost_usd=host_cost,
    )


def _usage(session_id: str, turns: tuple[TurnUsage, ...], *, nested: bool = False) -> SessionUsage:
    return SessionUsage(
        session_id=session_id,
        turns=turns,
        turns_without_usage=0,
        malformed_lines=0,
        unknown_usage_keys=frozenset(),
        nested=nested,
    )


def _decisions(
    session_id: str,
    *,
    eligible: int = 21,
    emitted: int = 2,
    raw_chars: int = 115_587,
    visible_chars: int = 105_061,
    full_expansions: int = 1,
    span_expansions: int = 0,
) -> SessionDecisions:
    return SessionDecisions(
        session_id=session_id,
        eligible=eligible,
        emitted=emitted,
        raw_chars=raw_chars,
        visible_chars=visible_chars,
        full_expansions=full_expansions,
        span_expansions=span_expansions,
    )


def _abandon_writer_mid_transaction(ledger: Path) -> None:
    """Leave a genuinely hot rollback journal behind, as a killed engine does.

    A tiny page cache forces the uncommitted rows out to the journal before
    the writer disappears without unwinding its transaction, so the next
    reader must roll that journal back before it can read anything.
    """
    rows = "[(900 + i, 'x' * 400 + str(i)) for i in range(4000)]"
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, sqlite3\n"
            f"db = sqlite3.connect({str(ledger)!r})\n"
            "db.execute('PRAGMA cache_size = 2')\n"
            "db.execute('BEGIN IMMEDIATE')\n"
            "db.executemany('INSERT INTO runtime_decisions (session_id, sequence, "
            "request_id, tool_name, outcome, reason, candidate_reference, raw_chars, "
            "visible_chars, latency_ms, created_at) VALUES (\\'crashed\\', ?, ?, \\'Read\\', "
            "\\'pass_through\\', \\'not_smaller\\', NULL, 5, 5, 1.0, 101.0)', "
            f"{rows})\n"
            "os._exit(0)\n",
        ],
        check=True,
    )
    assert Path(f"{ledger}-journal").exists()


def _seed_ledger(data_dir: Path, session_id: str, *, emitted: int, pass_through: int) -> None:
    storage = RuntimeStorage(data_dir)
    ledger = storage.open_ledger(session_id)
    sequence = 0
    for index in range(emitted):
        ledger.record_runtime_decision(
            sequence=sequence,
            request_id=f"e{index}",
            tool_name="Read",
            outcome="emitted",
            reason="smaller_envelope",
            candidate_reference=f"{session_id}/h{index}",
            raw_chars=400,
            visible_chars=120,
            latency_ms=1.5,
            created_at=100.0 + sequence,
        )
        sequence += 1
    for index in range(pass_through):
        ledger.record_runtime_decision(
            sequence=sequence,
            request_id=f"p{index}",
            tool_name="Bash",
            outcome="pass_through",
            reason="not_smaller",
            candidate_reference=None,
            raw_chars=60,
            visible_chars=60,
            latency_ms=0.9,
            created_at=200.0 + sequence,
        )
        sequence += 1
    ledger.record_runtime_expansion(request_id="x0", reference=f"{session_id}/h0", span=False)
    ledger.close()


def test_a_session_present_on_both_sides_carries_both_halves() -> None:
    composition = join([_usage(MATCHED, (_turn(),))], [_decisions(MATCHED)])

    assert len(composition.sessions) == 1
    session = composition.sessions[0]
    assert session.decisions is not None
    assert session.decisions.chars_avoided == 10_526
    assert session.turns == 1
    assert composition.unmatched_ledger_sessions == ()


def test_spend_without_a_ledger_is_kept_and_marked_unmatched() -> None:
    composition = join([_usage(SPEND_ONLY, (_turn(),))], [])

    assert composition.matched == ()
    assert [entry.session_id for entry in composition.unmatched_spend_sessions] == [SPEND_ONLY]
    # The spend still counts toward the corpus total; only the join failed.
    assert composition.host_cost_usd() == pytest.approx(0.55030125)


def test_a_ledger_without_a_transcript_is_named_not_dropped() -> None:
    composition = join([_usage(MATCHED, (_turn(),))], [_decisions(LEDGER_ONLY)])

    assert composition.unmatched_ledger_sessions == (LEDGER_ONLY,)
    # A dropped ledger would make codec activity look smaller than it was;
    # an unmatched one is excluded from the matched totals but still visible.
    assert codec_activity(composition).sessions == 0


def test_component_shares_sum_to_one_hundred() -> None:
    composition = join(
        [
            _usage(MATCHED, (_turn(), _turn(input_tokens=2, cache_read=87173, cache_write=967))),
            _usage(SPEND_ONLY, (_turn(model="claude-sonnet-5"),)),
        ],
        [_decisions(MATCHED)],
    )

    shares = composition.modelled_cost().shares()

    assert abs(shares.total - 100.0) <= SHARE_TOLERANCE_PCT
    assert shares.cache_write > shares.uncached_input


def test_a_corpus_with_no_spend_refuses_to_apportion_shares() -> None:
    empty = Composition(
        sessions=(),
        unmatched_ledger_sessions=(),
        damaged_ledgers=0,
    )

    with pytest.raises(ZeroCostError):
        empty.modelled_cost().shares()


def test_the_hosts_cost_and_laconics_model_are_both_reported() -> None:
    # claude-opus-4-8 is priced identically to what OMP modelled here, so the
    # two agree; a model absent from PRICING is where they diverge, and that
    # is what unpriced_models exists to name.
    composition = join([_usage(MATCHED, (_turn(),))], [])

    assert composition.host_cost_usd() == pytest.approx(composition.modelled_cost().total)
    assert composition.unpriced_models == []


def test_an_unpriced_model_is_named_rather_than_silently_guessed() -> None:
    composition = join([_usage(MATCHED, (_turn(model="gpt-5.6-terra"),))], [])

    assert composition.unpriced_models == ["gpt-5.6-terra"]
    # The fallback price is Sonnet's, so the host's own figure and Laconic's
    # disagree. Reporting only one of them would hide that.
    assert composition.host_cost_usd() != pytest.approx(composition.modelled_cost().total)


def test_per_model_counters_accumulate_across_sessions() -> None:
    composition = join(
        [
            _usage(MATCHED, (_turn(), _turn(model="claude-sonnet-5"))),
            _usage(SPEND_ONLY, (_turn(),)),
        ],
        [],
    )

    totals = composition.usage()

    assert totals["claude-opus-4-8"].turns == 2
    assert totals["claude-sonnet-5"].turns == 1
    assert totals["claude-opus-4-8"].cache_write == 87173 * 2


def test_a_subagent_session_stays_distinguishable_from_a_root_session() -> None:
    composition = join(
        [_usage(MATCHED, (_turn(),)), _usage(SPEND_ONLY, (_turn(),), nested=True)],
        [],
    )

    nested = {entry.session_id: entry.nested for entry in composition.sessions}
    assert nested == {MATCHED: False, SPEND_ONLY: True}


def test_a_transcript_with_no_priced_turns_is_counted_and_still_joined() -> None:
    composition = join([_usage(SPEND_ONLY, ())], [])

    assert composition.priced == ()
    assert composition.sessions_without_priced_turns == 1
    assert composition.unmatched_spend_sessions == ()


def test_a_ledger_whose_transcript_had_no_priced_turn_is_not_called_unmatched() -> None:
    # The transcript exists; it just had no billable turn. Calling its ledger
    # unmatched would be false, and dropping the session would silently
    # shrink every codec total.
    composition = join([_usage(MATCHED, ())], [_decisions(MATCHED, eligible=40, emitted=9)])

    assert composition.unmatched_ledger_sessions == ()
    activity = codec_activity(composition)
    assert activity.sessions == 1
    assert activity.eligible == 40
    assert activity.emitted == 9
    assert activity.chars_avoided == 10_526


def test_two_transcripts_claiming_one_session_id_are_refused() -> None:
    # Joining both against the same ledger would double every counter.
    with pytest.raises(DuplicateSessionError, match="double every counter"):
        join(
            [_usage(MATCHED, (_turn(),)), _usage(MATCHED, (_turn(),))],
            [_decisions(MATCHED)],
        )


def test_the_store_reader_groups_real_ledgers_by_session(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed_ledger(data_dir, MATCHED, emitted=2, pass_through=3)
    _seed_ledger(data_dir, LEDGER_ONLY, emitted=1, pass_through=0)

    scan = scan_store(data_dir)

    by_id = {row.session_id: row for row in scan.sessions}
    assert set(by_id) == {MATCHED, LEDGER_ONLY}
    assert by_id[MATCHED].eligible == 5
    assert by_id[MATCHED].emitted == 2
    assert by_id[MATCHED].pass_through == 3
    assert by_id[MATCHED].raw_chars == 2 * 400 + 3 * 60
    assert by_id[MATCHED].visible_chars == 2 * 120 + 3 * 60
    assert by_id[MATCHED].chars_avoided == 560
    assert by_id[MATCHED].full_expansions == 1
    assert scan.damaged_ledgers == 0


def test_the_store_reader_never_writes_to_the_store(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed_ledger(data_dir, MATCHED, emitted=1, pass_through=1)
    sessions = data_dir / "sessions"
    before = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns) for path in sessions.iterdir()
    }

    scan_store(data_dir)

    after = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns) for path in sessions.iterdir()
    }
    assert after == before


def test_a_ledger_with_no_schema_is_counted_as_damaged_not_fatal(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed_ledger(data_dir, MATCHED, emitted=1, pass_through=1)
    damaged = data_dir / "sessions" / ("f" * 64 + ".sqlite3")
    damaged.write_bytes(b"")

    scan = scan_store(data_dir)

    assert scan.damaged_ledgers == 1
    assert [row.session_id for row in scan.sessions] == [MATCHED]


def test_an_empty_store_reads_as_empty(tmp_path: Path) -> None:
    scan = scan_store(tmp_path / "data")

    assert scan.sessions == ()
    assert scan.damaged_ledgers == 0


def test_a_session_with_an_expansion_but_no_decision_is_still_reported(tmp_path: Path) -> None:
    # The engine cannot produce this, but the schema permits it. Dropping the
    # row would lose a recorded expansion with no trace.
    data_dir = tmp_path / "data"
    _seed_ledger(data_dir, MATCHED, emitted=1, pass_through=1)
    ledger = RuntimeStorage(data_dir).open_ledger(LEDGER_ONLY)
    ledger.record_runtime_expansion(request_id="lone", reference=f"{LEDGER_ONLY}/F1", span=False)
    ledger.close()

    by_id = {row.session_id: row for row in scan_store(data_dir).sessions}

    assert by_id[LEDGER_ONLY].eligible == 0
    assert by_id[LEDGER_ONLY].emitted == 0
    assert by_id[LEDGER_ONLY].full_expansions == 1


def test_a_ledger_deleted_mid_scan_is_never_recreated_by_the_reader(tmp_path: Path) -> None:
    # owned_ledger_files enumerates, then each ledger is opened. A bare
    # sqlite3.connect would CREATE the file in that window, leaving a stray
    # empty database in the owner's store that every later scan counts as
    # damaged forever.
    data_dir = tmp_path / "data"
    _seed_ledger(data_dir, MATCHED, emitted=1, pass_through=1)
    vanished = data_dir / "sessions" / ("a" * 64 + ".sqlite3")

    with pytest.raises(sqlite3.OperationalError):
        with closing(open_query_only(vanished)):
            pass

    assert not vanished.exists()


def test_the_reader_recovers_a_crashed_writers_journal_and_reads_the_ledger(
    tmp_path: Path,
) -> None:
    # The one case where reading the store touches it: SQLite must roll a
    # killed writer's own aborted transaction back before anything can be
    # read. The package documents that rather than claiming it never happens.
    data_dir = tmp_path / "data"
    _seed_ledger(data_dir, MATCHED, emitted=1, pass_through=1)
    ledger = RuntimeStorage(data_dir).ledger_path(MATCHED)
    _abandon_writer_mid_transaction(ledger)

    scan = scan_store(data_dir)

    assert scan.damaged_ledgers == 0
    # The killed writer's rows are gone, because its transaction was rolled
    # back rather than completed, and the committed session still reads.
    assert {row.session_id for row in scan.sessions} == {MATCHED}
    assert not Path(f"{ledger}-journal").exists()
