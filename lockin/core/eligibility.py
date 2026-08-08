"""Which players may occupy which starting slots.

Pure. The rule below was derived from 1,500 real starter-slot assignments across
all 25 weeks and 10 rosters of 2025-26 — Sleeper does not publish it.

    slot   allowed positions        violations in 1,500 observations
    PG     PG, SG                   0
    G      PG, SG                   0
    F      SF, PF, C                17  (see OBSERVED_EXTRA_SLOTS)
    C      C, PF                    0
    UTIL   any                      0

Three slots are exactly determined. `F` is not, and the exception matters: 17
assignments put a player listed ``['PG', 'SG']`` into F. Those are three
specific players — Sleeper's own eligibility is broader than the
``fantasy_positions`` it publishes for them.

Being too STRICT here silently removes legal lineups from consideration, which
is invisible. Being too PERMISSIVE recommends a lineup Sleeper will reject,
which the user notices immediately. Neither is free, so the base rule stays
tight and the exceptions are carried explicitly rather than by loosening F for
everyone.

`C` is the structural bottleneck the architecture doc §3.2 predicts: only
C/PF-eligible players can fill it, and only C or UTIL will take them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

ALL_POSITIONS = frozenset({"PG", "SG", "SF", "PF", "C"})

# Base rule. Slots this league does not use are included so a change to
# roster_positions does not silently fall through to "ineligible".
SLOT_POSITIONS: Mapping[str, frozenset[str]] = {
    "PG": frozenset({"PG", "SG"}),
    "SG": frozenset({"PG", "SG"}),
    "G": frozenset({"PG", "SG"}),
    "SF": frozenset({"SF", "PF", "C"}),
    "PF": frozenset({"SF", "PF", "C"}),
    "F": frozenset({"SF", "PF", "C"}),
    "C": frozenset({"C", "PF"}),
    "UTIL": ALL_POSITIONS,
}

# Players observed in a slot their published positions do not permit. Learned
# from 2025-26, not hand-written: `lockin locks` reports any new ones, and they
# should be added here rather than by widening the base rule.
#
#   2574 Amen Thompson             ['PG','SG'] started at F  x7
#   2055 Nickeil Alexander-Walker  ['PG','SG'] started at F  x5
#   2255 Ayo Dosunmu               ['PG','SG'] started at F  x5
OBSERVED_EXTRA_SLOTS: Mapping[str, frozenset[str]] = {
    "2574": frozenset({"F"}),
    "2055": frozenset({"F"}),
    "2255": frozenset({"F"}),
}

# Slots that are never restricted, whatever a player's positions say.
UNRESTRICTED_SLOTS = frozenset({"UTIL", "BN"})


class UnknownSlot(KeyError):
    """A slot name with no eligibility rule. Never guess — guessing here would
    silently permit or forbid lineups."""


def eligible(
    slot: str,
    positions: Iterable[str],
    *,
    sleeper_id: str | None = None,
    extra_slots: Mapping[str, frozenset[str]] = OBSERVED_EXTRA_SLOTS,
) -> bool:
    """Can a player with these positions occupy this slot?"""
    if slot in UNRESTRICTED_SLOTS:
        return True
    if sleeper_id is not None and slot in extra_slots.get(sleeper_id, frozenset()):
        return True
    try:
        allowed = SLOT_POSITIONS[slot]
    except KeyError as exc:
        raise UnknownSlot(f"no eligibility rule for slot {slot!r}") from exc
    return bool(set(positions) & allowed)


def eligible_slots(
    positions: Iterable[str],
    slots: Iterable[str],
    *,
    sleeper_id: str | None = None,
    extra_slots: Mapping[str, frozenset[str]] = OBSERVED_EXTRA_SLOTS,
) -> frozenset[str]:
    """Every slot in `slots` this player could fill."""
    pos = frozenset(positions)
    return frozenset(
        s for s in slots if eligible(s, pos, sleeper_id=sleeper_id, extra_slots=extra_slots)
    )


def slot_flexibility(
    positions: Iterable[str],
    slots: Iterable[str],
    *,
    sleeper_id: str | None = None,
) -> int:
    """How many distinct starting slots a player can fill.

    Multi-slot players carry option value above their raw projection because
    they preserve assignment freedom once others are locked (architecture doc
    §3.2). This is the crude version of that; the rollout engine prices it
    properly by simulating.
    """
    return len(eligible_slots(positions, set(slots), sleeper_id=sleeper_id))
