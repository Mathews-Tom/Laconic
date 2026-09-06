"""The report renders the same twice, leaks nothing, and keeps its caveats."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import laconic.spend.privacy as privacy_module
from laconic.spend.cli import REPORT_JSON, REPORT_MARKDOWN, write_report
from laconic.spend.join import join
from laconic.spend.ledger import SessionDecisions
from laconic.spend.omp import SessionUsage, TurnUsage
from laconic.spend.privacy import PrivacyViolationError, validate_report_json
from laconic.spend.report import (
    ALLOWED_REPORT_KEYS,
    LIMITATIONS,
    build_report,
    render_markdown,
    session_hash,
)

MATCHED = "01a078c0-15c2-7000-9389-19db9037f833"
SPEND_ONLY = "01a06d0f-6060-7000-94fc-3301f336bf8b"


def _turn(model: str = "claude-opus-4-8") -> TurnUsage:
    return TurnUsage(
        model=model,
        provider="anthropic",
        input_tokens=4,
        cache_read=87_173,
        cache_write=967,
        output_tokens=218,
        host_cost_usd=0.055,
    )


def _usage(session_id: str, *, nested: bool = False, turns: int = 1) -> SessionUsage:
    return SessionUsage(
        session_id=session_id,
        turns=tuple(_turn() for _ in range(turns)),
        turns_without_usage=0,
        malformed_lines=0,
        unknown_usage_keys=frozenset(),
        nested=nested,
    )


def _decisions(session_id: str) -> SessionDecisions:
    return SessionDecisions(
        session_id=session_id,
        eligible=21,
        emitted=2,
        raw_chars=115_587,
        visible_chars=105_061,
        full_expansions=1,
        span_expansions=0,
    )


def _payload(**kwargs: Any) -> dict[str, Any]:
    composition = join(
        kwargs.pop("usage", [_usage(MATCHED), _usage(SPEND_ONLY, nested=True)]),
        kwargs.pop("decisions", [_decisions(MATCHED)]),
        **kwargs,
    )
    return json.loads(build_report(composition).to_json())


def test_the_report_validates_and_carries_every_limitation() -> None:
    payload = _payload()

    validate_report_json(payload)

    assert tuple(payload["limitations"]) == LIMITATIONS
    assert len(LIMITATIONS) == 7


def test_the_rendering_is_byte_identical_across_two_runs() -> None:
    first = build_report(join([_usage(MATCHED)], [_decisions(MATCHED)]))
    second = build_report(join([_usage(MATCHED)], [_decisions(MATCHED)]))

    assert first.to_json() == second.to_json()
    assert render_markdown(first) == render_markdown(second)


def test_the_rendering_disclaims_savings_and_never_asserts_one() -> None:
    rendered = render_markdown(build_report(join([_usage(MATCHED)], [_decisions(MATCHED)])))

    lowered = rendered.lower()
    assert "makes no savings claim" in lowered
    assert "single-arm corpus" in lowered
    assert "no counterfactual exists" in lowered
    # The failure this guards is a claim appearing, not the word appearing:
    # every phrase below asserts a saving rather than denying one.
    for claim in ("saved", "savings of", "we save", "net saving", "% saving", "reduction of"):
        assert claim not in lowered, f"the report asserts a saving: {claim!r}"


def test_a_real_session_id_never_reaches_the_payload() -> None:
    payload = _payload()

    serialized = json.dumps(payload)
    assert MATCHED not in serialized
    assert SPEND_ONLY not in serialized
    assert payload["sessions"][0]["session_hash"] == session_hash(MATCHED)


def test_only_matched_sessions_are_serialized_row_by_row() -> None:
    payload = _payload()

    assert payload["matched_sessions"] == 1
    assert payload["unmatched_spend_sessions"] == 1
    assert [entry["session_hash"] for entry in payload["sessions"]] == [session_hash(MATCHED)]


def test_subagent_sessions_are_counted_apart_from_root_sessions() -> None:
    payload = _payload()

    assert payload["root_sessions"] == 1
    assert payload["nested_sessions"] == 1
    assert payload["sessions_with_spend"] == 2


def test_an_empty_corpus_reports_no_shares_rather_than_four_zeroes() -> None:
    payload = _payload(usage=[], decisions=[])

    validate_report_json(payload)
    assert payload["corpus_shares"] is None
    assert payload["corpus_cost"]["total"] == 0.0


def test_the_allowlist_rejects_an_added_key() -> None:
    payload = _payload()
    payload["cwd"] = "/Users/owner/WorkSpace/private"

    with pytest.raises(PrivacyViolationError, match="unallowlisted report key"):
        validate_report_json(payload)


def test_the_allowlist_rejects_a_removed_key() -> None:
    payload = _payload()
    del payload["codec"]

    with pytest.raises(PrivacyViolationError, match="missing report key"):
        validate_report_json(payload)


def test_the_allowlist_rejects_a_raw_session_id_in_place_of_a_digest() -> None:
    payload = _payload()
    payload["sessions"][0]["session_hash"] = MATCHED

    with pytest.raises(PrivacyViolationError, match="never a session id"):
        validate_report_json(payload)


@pytest.mark.parametrize(
    "leaked",
    [
        "/Users/owner/WorkSpace/laconic",
        "../../etc/passwd",
        "C:\\Users\\owner",
        "a model name with spaces",
        ".hidden",
        "x" * 65,
    ],
)
def test_the_allowlist_rejects_a_path_shaped_model_identifier(leaked: str) -> None:
    payload = _payload()
    payload["unpriced_models"] = [leaked]

    with pytest.raises(PrivacyViolationError, match="unpriced_models"):
        validate_report_json(payload)


def test_a_real_slash_bearing_model_identifier_is_still_accepted() -> None:
    payload = _payload()
    payload["unpriced_models"] = ["~anthropic/claude-opus-latest", "gpt-5.6-terra"]

    validate_report_json(payload)


def test_a_report_that_drops_its_single_arm_caveat_is_rejected() -> None:
    payload = _payload()
    payload["limitations"] = [
        name
        for name in payload["limitations"]
        if name != "single_arm_corpus_every_session_ran_with_the_codec_enabled"
    ]

    with pytest.raises(PrivacyViolationError, match="single-arm caveat"):
        validate_report_json(payload)


def test_a_reordered_limitations_block_is_rejected() -> None:
    payload = _payload()
    payload["limitations"] = list(reversed(payload["limitations"]))

    with pytest.raises(PrivacyViolationError, match="in order"):
        validate_report_json(payload)


def test_a_negative_counter_is_rejected() -> None:
    payload = _payload()
    payload["codec"]["emitted"] = -1

    with pytest.raises(PrivacyViolationError, match="must not be negative"):
        validate_report_json(payload)


def test_a_boolean_smuggled_in_as_a_counter_is_rejected() -> None:
    payload = _payload()
    payload["priced_turns"] = True

    with pytest.raises(PrivacyViolationError, match="must be an integer"):
        validate_report_json(payload)


def test_writing_the_report_produces_both_artifacts(tmp_path: Path) -> None:
    composition = join([_usage(MATCHED)], [_decisions(MATCHED)])

    written = write_report(composition, tmp_path)

    assert written.json_path == tmp_path / REPORT_JSON
    assert written.markdown_path == tmp_path / REPORT_MARKDOWN
    validate_report_json(json.loads(written.json_path.read_text(encoding="utf-8")))
    assert "Limitations" in written.markdown_path.read_text(encoding="utf-8")


def test_writing_twice_produces_identical_bytes(tmp_path: Path) -> None:
    composition = join([_usage(MATCHED)], [_decisions(MATCHED)])

    first = write_report(composition, tmp_path / "a")
    second = write_report(composition, tmp_path / "b")

    assert first.json_path.read_bytes() == second.json_path.read_bytes()
    assert first.markdown_path.read_bytes() == second.markdown_path.read_bytes()


def test_a_failing_privacy_check_writes_nothing(tmp_path: Path, monkeypatch: Any) -> None:
    import laconic.spend.report as report_module

    monkeypatch.setattr(report_module, "LIMITATIONS", ("only_one_caveat",))
    composition = join([_usage(MATCHED)], [_decisions(MATCHED)])

    with pytest.raises(PrivacyViolationError):
        write_report(composition, tmp_path / "out")

    assert not (tmp_path / "out" / REPORT_JSON).exists()


def test_no_savings_ratio_is_serialized_at_all() -> None:
    # A tokens-per-avoided-character ratio published beside chars_avoided
    # multiplies back to the matched sessions' whole token volume, which a
    # reader relabels 'tokens saved'. The report carries no such quantity.
    payload = _payload()

    assert "tokens_per_avoided_character" not in payload
    assert not any("per_avoided" in key or "per_character" in key for key in payload)


def test_every_allowlisted_key_is_covered_by_a_shape_check() -> None:
    # The gate's completeness must be enforced, not coincidental: a key
    # added to the allowlist and to no shape group lands in the derived
    # integer group and fails loudly instead of serializing unchecked.
    payload = _payload()
    payload["cwd"] = "/Users/owner/WorkSpace/private"
    ALLOWED_REPORT_KEYS_WITH_LEAK = ALLOWED_REPORT_KEYS | {"cwd"}

    assert "cwd" in ALLOWED_REPORT_KEYS_WITH_LEAK - (
        privacy_module._TOKEN_BLOCK_KEYS
        | privacy_module._COST_BLOCK_KEYS
        | privacy_module._SHARE_BLOCK_KEYS
        | privacy_module._USD_KEYS
        | privacy_module._IDENTIFIER_LIST_KEYS
        | privacy_module._INLINE_CHECKED_KEYS
    )
    assert (
        privacy_module._INT_KEYS
        | privacy_module._TOKEN_BLOCK_KEYS
        | (
            privacy_module._COST_BLOCK_KEYS
            | privacy_module._SHARE_BLOCK_KEYS
            | privacy_module._USD_KEYS
            | privacy_module._IDENTIFIER_LIST_KEYS
            | privacy_module._INLINE_CHECKED_KEYS
        )
        == ALLOWED_REPORT_KEYS
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_a_non_finite_dollar_figure_is_refused_before_it_reaches_disk(value: float) -> None:
    payload = _payload()
    payload["corpus_host_cost_usd"] = value

    with pytest.raises(PrivacyViolationError, match="must be finite"):
        validate_report_json(payload)


def test_a_ledger_bearing_session_with_no_priced_turn_is_reported(tmp_path: Path) -> None:
    composition = join([_usage(MATCHED, turns=0)], [_decisions(MATCHED)])

    payload = json.loads(build_report(composition).to_json())

    validate_report_json(payload)
    assert payload["sessions_with_spend"] == 0
    assert payload["sessions_without_priced_turns"] == 1
    assert payload["codec_active_sessions_without_priced_turns"] == 1
    # Its codec work is counted, not dropped.
    assert payload["codec"]["eligible"] == 21
    assert payload["unmatched_ledger_sessions"] == 0


def test_the_report_refuses_to_write_inside_the_runtime_store(tmp_path: Path) -> None:
    store = tmp_path / "store"
    (store / "sessions").mkdir(parents=True)
    composition = join([_usage(MATCHED)], [_decisions(MATCHED)])

    with pytest.raises(OSError, match="read-only source tree"):
        write_report(composition, store / "spend", data_dir=store)

    assert not (store / "spend").exists()


def test_the_report_refuses_to_write_through_a_symlink(tmp_path: Path) -> None:
    destination = tmp_path / "out"
    destination.mkdir()
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text("untouched", encoding="utf-8")
    (destination / REPORT_JSON).symlink_to(elsewhere)
    composition = join([_usage(MATCHED)], [_decisions(MATCHED)])

    with pytest.raises(OSError, match="refusing to write through a symlink"):
        write_report(composition, destination)

    assert elsewhere.read_text(encoding="utf-8") == "untouched"
