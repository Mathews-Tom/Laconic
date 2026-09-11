"""The cached band `laconic status` shows: present, dated, or plainly absent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from laconic.spend.cached import read_cached_estimate

_ESTIMATE: dict[str, Any] = {
    "avoided_cost_usd_low": 54.24,
    "avoided_cost_usd_high": 124.56,
    "avoided_share_pct_low": 3.56,
    "avoided_share_pct_high": 8.18,
    "denominator_usd": 1522.64,
}


def _report(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_written_band_is_read_back_with_its_age(tmp_path: Path) -> None:
    """Age is the whole point of showing a cached figure.

    `status` answers immediately because it reads only ledgers; producing
    an estimate joins every transcript and takes tens of seconds. Showing
    the last one is only honest if it says how old it is.
    """
    report = _report(tmp_path / "report.json", {"estimate": _ESTIMATE})
    modified = report.stat().st_mtime

    cached = read_cached_estimate(report, now=modified + 7200)

    assert cached is not None
    assert cached.low_usd == 54.24
    assert cached.high_usd == 124.56
    assert cached.age_text == "2h ago"


def test_a_corpus_that_could_not_be_estimated_reads_as_absent(tmp_path: Path) -> None:
    """`None`, so the caller says "not estimated" rather than printing zero."""
    report = _report(tmp_path / "report.json", {"estimate": None})

    assert read_cached_estimate(report) is None


def test_a_missing_or_unreadable_report_reads_as_absent(tmp_path: Path) -> None:
    """Never raise into `status`: a damaged cache costs a line, not the command."""
    assert read_cached_estimate(tmp_path / "absent.json") is None

    damaged = tmp_path / "damaged.json"
    damaged.write_text("{not json", encoding="utf-8")
    assert read_cached_estimate(damaged) is None

    older_schema = _report(tmp_path / "old.json", {"schema_version": 1})
    assert read_cached_estimate(older_schema) is None


def test_age_never_reads_as_fresher_than_it_is(tmp_path: Path) -> None:
    """A clock that moved backwards must not render a future report as new."""
    report = _report(tmp_path / "report.json", {"estimate": _ESTIMATE})
    modified = report.stat().st_mtime

    cached = read_cached_estimate(report, now=modified - 3600)

    assert cached is not None
    assert cached.age_seconds == 0.0
    assert cached.age_text == "just now"


def test_a_non_utf8_report_reads_as_absent(tmp_path: Path) -> None:
    """`UnicodeDecodeError` is a `ValueError`, not a `JSONDecodeError`.

    Guarding only the narrower type let a corrupt report escape into
    `laconic status`, which has no handler of its own, aborting the
    command with a traceback after it had already printed half its output.
    """
    damaged = tmp_path / "binary.json"
    damaged.write_bytes(b'{"estimate": "\xff\xfe not utf8"}')

    assert read_cached_estimate(damaged) is None


def test_a_non_finite_value_reads_as_absent(tmp_path: Path) -> None:
    """`json.loads` accepts bare `NaN`, and `float()` passes it through.

    Without this the band renders as "$nan to $inf". The write path already
    refuses non-finite values before serializing; this applies the same
    standard on the way back in.
    """
    report = tmp_path / "nan.json"
    report.write_text(
        '{"estimate": {"avoided_cost_usd_low": NaN, "avoided_cost_usd_high": Infinity,'
        ' "avoided_share_pct_low": 1.0, "avoided_share_pct_high": 2.0,'
        ' "denominator_usd": 3.0}}',
        encoding="utf-8",
    )

    assert read_cached_estimate(report) is None


def test_status_survives_a_corrupt_cache_and_still_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The layer the crash actually reached.

    A damaged cache must cost one line of output, never the command: the
    adapter and storage lines above it are the reason a user ran `status`.
    """
    from laconic import cli as cli_module

    report_dir = tmp_path / "spend"
    report_dir.mkdir()
    (report_dir / "spend-composition.json").write_bytes(b"\xff\xfe not utf8 at all")
    monkeypatch.setattr(cli_module, "DEFAULT_OUTPUT_DIR", report_dir)
    monkeypatch.chdir(tmp_path)

    exit_code = cli_module.main(["status", "--data-dir", str(tmp_path / "data")])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Laconic runtime status" in output
    assert "not estimated yet" in output
