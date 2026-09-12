"""Cost model behaviour: pricing lookup, cache multipliers, cost splits."""

from __future__ import annotations

import pytest

from laconic.costs import (
    DEFAULT_PRICE,
    CostBreakdown,
    CostShares,
    ModelUsage,
    ZeroCostError,
    active_registry,
    price_for,
    session_cost,
    unpriced_models,
)


def test_a_registry_price_is_never_shadowed_by_a_second_source() -> None:
    """The registry is the only price source, structurally.

    A hand-written seven-model table used to sit in front of it and win.
    It had gone stale on two of its seven entries, and because it won it
    was the stale price every published figure used -- `claude-sonnet-5`
    at $3/$15 where both the registry and the host's own per-turn
    accounting say $2/$10. Two sources for one price is the defect; the
    wrong entries were only how it surfaced. This fails the moment any
    second source is reintroduced in front of the registry, for any model,
    rather than pinning the two entries that happened to be wrong.
    """
    registry = active_registry()
    assert registry.rates, "the bundled snapshot must price something"

    for model, rate in registry.rates.items():
        price = price_for(model)
        assert price.input_per_mtok == pytest.approx(rate.input * 1e6), model
        assert price.output_per_mtok == pytest.approx(rate.output * 1e6), model


def test_the_registry_prices_sonnet_5_as_the_host_bills_it() -> None:
    """The concrete entry the removed table shadowed.

    $2/$10 is what the bundled registry publishes and what OMP's own
    per-turn cost back-solves to on every one of 57,700 recorded turns.
    The shadowing table said $3/$15, and this model alone accounted for
    $2,371.83 of the $2,331.82 the shadow added to the development
    corpus net of `claude-fable-5`'s $40.01 correction in the other
    direction -- either figure being more than the whole gap against the
    host's own accounting.
    """
    price = price_for("claude-sonnet-5")

    assert price.input_per_mtok == pytest.approx(2.0)
    assert price.output_per_mtok == pytest.approx(10.0)


def test_unknown_model_falls_back_to_default_price() -> None:
    assert price_for("some-model-we-have-never-seen") == DEFAULT_PRICE


def test_cache_read_is_billed_at_a_tenth_of_uncached_input() -> None:
    uncached = ModelUsage(input_tokens=1_000_000).cost("claude-sonnet-5")
    cached = ModelUsage(cache_read=1_000_000).cost("claude-sonnet-5")
    assert cached.cache_read == pytest.approx(uncached.uncached_input * 0.10)


def test_cache_write_is_billed_above_uncached_input() -> None:
    uncached = ModelUsage(input_tokens=1_000_000).cost("claude-sonnet-5")
    written = ModelUsage(cache_write=1_000_000).cost("claude-sonnet-5")
    assert written.cache_write == pytest.approx(uncached.uncached_input * 1.25)


def test_cache_components_use_the_models_own_input_price() -> None:
    """A model priced above the fallback must not be billed at the fallback."""
    cost = ModelUsage(cache_read=1_000_000, cache_write=1_000_000).cost("claude-opus-4-8")
    assert cost.cache_read == pytest.approx(0.5)
    assert cost.cache_write == pytest.approx(6.25)


def test_cost_components_use_the_right_price_axis() -> None:
    cost = ModelUsage(input_tokens=1_000_000, output_tokens=1_000_000).cost("claude-opus-4-8")
    assert cost.uncached_input == pytest.approx(5.0)
    assert cost.output == pytest.approx(25.0)
    assert cost.total == pytest.approx(30.0)


def test_add_turn_accumulates_without_mutating() -> None:
    first = ModelUsage()
    second = first.add_turn(input_tokens=10, cache_read=20, cache_write=30, output_tokens=40)
    third = second.add_turn(input_tokens=1, cache_read=2, cache_write=3, output_tokens=4)
    assert first == ModelUsage()
    assert second.turns == 1
    assert third == ModelUsage(
        turns=2, input_tokens=11, cache_read=22, cache_write=33, output_tokens=44
    )


def test_shares_sum_to_one_hundred_percent() -> None:
    usage = ModelUsage(
        input_tokens=1_234, cache_read=987_654, cache_write=54_321, output_tokens=6_789
    )
    shares = usage.cost("claude-sonnet-5").shares()
    assert shares.total == pytest.approx(100.0, abs=1e-9)


def test_shares_reproduce_the_documented_cost_ordering() -> None:
    """Cache reads dominate a realistic session; output is a minority slice."""
    usage = ModelUsage(
        input_tokens=50_000,
        cache_read=28_000_000,
        cache_write=1_000_000,
        output_tokens=150_000,
    )
    shares = usage.cost("claude-sonnet-5").shares()
    assert shares.cache_read > shares.cache_write > shares.output
    assert shares.output > shares.uncached_input
    assert shares.total == pytest.approx(100.0, abs=1e-9)


def test_zero_spend_raises_instead_of_reporting_a_meaningless_split() -> None:
    with pytest.raises(ZeroCostError):
        CostBreakdown().shares()


def test_session_cost_sums_across_models_at_their_own_prices() -> None:
    usage = {
        "claude-opus-4-8": ModelUsage(output_tokens=1_000_000),
        "claude-haiku-4-5": ModelUsage(output_tokens=1_000_000),
    }
    assert session_cost(usage).output == pytest.approx(30.0)


def test_session_cost_of_no_usage_is_zero() -> None:
    assert session_cost({}).total == 0.0


def test_unknown_models_are_reported_as_guessed_prices() -> None:
    usage = {
        "claude-sonnet-5": ModelUsage(turns=1),
        "some-model-we-have-never-seen": ModelUsage(turns=1),
        "unknown": ModelUsage(turns=1),
    }
    assert unpriced_models(usage) == ["some-model-we-have-never-seen", "unknown"]


def test_shares_that_do_not_sum_to_one_hundred_are_rejected() -> None:
    with pytest.raises(ValueError, match="not 100"):
        CostShares(uncached_input=1.0, cache_read=2.0, cache_write=3.0, output=4.0)
