"""Slot eligibility.

The rule is empirically derived (see lockin/core/eligibility.py). These tests
pin the three slots that came back exactly determined across 1,500 observations,
and the override mechanism that carries the one that did not.
"""

from __future__ import annotations

import pytest

from lockin.core.eligibility import (
    UnknownSlot,
    eligible,
    eligible_slots,
    slot_flexibility,
)

LEAGUE_SLOTS = ["PG", "G", "F", "C", "UTIL", "UTIL"]


# --- the three exactly-determined slots -------------------------------------


@pytest.mark.parametrize("positions", [["PG"], ["SG"], ["PG", "SG"], ["SF", "SG"]])
def test_guard_slots_accept_guards(positions):
    assert eligible("PG", positions)
    assert eligible("G", positions)


@pytest.mark.parametrize("positions", [["C"], ["PF"], ["SF"], ["C", "PF"], ["PF", "SF"]])
def test_guard_slots_reject_players_with_no_guard_position(positions):
    """Zero violations across 250 observations each for PG and G."""
    assert not eligible("PG", positions)
    assert not eligible("G", positions)


@pytest.mark.parametrize("positions", [["C"], ["PF"], ["C", "PF"], ["PF", "SF"]])
def test_center_accepts_bigs(positions):
    assert eligible("C", positions)


@pytest.mark.parametrize("positions", [["PG"], ["SG"], ["PG", "SG"], ["SF"], ["SF", "SG"]])
def test_center_rejects_players_with_neither_c_nor_pf(positions):
    """Zero violations across 250 observations. C is the structural bottleneck."""
    assert not eligible("C", positions)


# --- forward and flex -------------------------------------------------------


@pytest.mark.parametrize("positions", [["SF"], ["PF"], ["C"], ["PF", "SF"], ["C", "PF"]])
def test_forward_accepts_forwards_and_centers(positions):
    assert eligible("F", positions)


def test_forward_rejects_a_pure_guard_by_default():
    assert not eligible("F", ["PG", "SG"])


@pytest.mark.parametrize("positions", [["PG"], ["SG"], ["SF"], ["PF"], ["C"]])
def test_util_accepts_everyone(positions):
    assert eligible("UTIL", positions)


# --- the override mechanism -------------------------------------------------


def test_observed_override_grants_forward_eligibility():
    """Amen Thompson is listed ['PG','SG'] but started 7 times at F.

    Sleeper's own eligibility is broader than the fantasy_positions it
    publishes, so the exception is carried per-player rather than by loosening
    F for every guard.
    """
    assert not eligible("F", ["PG", "SG"])
    assert eligible("F", ["PG", "SG"], sleeper_id="2574")


def test_override_does_not_leak_to_other_slots():
    """An F override must not make him a center."""
    assert not eligible("C", ["PG", "SG"], sleeper_id="2574")


def test_override_does_not_leak_to_other_players():
    assert not eligible("F", ["PG", "SG"], sleeper_id="9999")


def test_overrides_can_be_injected():
    custom = {"abc": frozenset({"C"})}
    assert eligible("C", ["PG"], sleeper_id="abc", extra_slots=custom)
    assert not eligible("C", ["PG"], sleeper_id="abc", extra_slots={})


# --- structure --------------------------------------------------------------


def test_unknown_slot_raises_rather_than_guessing():
    """Guessing would silently permit or forbid lineups."""
    with pytest.raises(UnknownSlot, match="ROVER"):
        eligible("ROVER", ["PG"])


def test_bench_is_unrestricted():
    assert eligible("BN", ["C"])


def test_a_center_only_player_can_fill_just_two_slots():
    """Architecture doc §3.2: C-eligible players can only fill C or UTIL."""
    assert eligible_slots(["C"], LEAGUE_SLOTS) == frozenset({"C", "F", "UTIL"})
    assert slot_flexibility(["C"], LEAGUE_SLOTS) == 3


def test_a_combo_guard_is_more_flexible_than_a_center():
    """Multi-slot players preserve assignment freedom once others are locked."""
    guard = slot_flexibility(["PG", "SG"], LEAGUE_SLOTS)
    center = slot_flexibility(["C"], LEAGUE_SLOTS)
    assert guard >= center


def test_eligible_slots_deduplicates_the_two_util_slots():
    slots = eligible_slots(["PG"], LEAGUE_SLOTS)
    assert slots == frozenset({"PG", "G", "UTIL"})
