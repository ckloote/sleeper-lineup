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

import math
from collections.abc import Iterable, Mapping, Sequence

import numpy as np
from scipy.optimize import linear_sum_assignment

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


class NoValidLineup(ValueError):
    """No way to fill every slot from the available players."""


def assign_slots(
    slots: Sequence[str],
    candidates: Sequence[str],
    positions: Mapping[str, Iterable[str]],
    values: Mapping[str, float],
    *,
    locked: Mapping[str, str] | None = None,
    extra_slots: Mapping[str, frozenset[str]] = OBSERVED_EXTRA_SLOTS,
) -> dict[str, str]:
    """Best legal assignment of players to starting slots. Returns ``{slot: player}``.

    Maximising total ``values`` over a bipartite graph is an assignment problem,
    so it is solved exactly rather than greedily. Greedy fails here for a
    concrete reason: ``C`` accepts only C/PF, so filling ``UTIL`` with the
    highest-valued player left can strand the one centre and cost more than the
    swap gained. Architecture doc §3.2 calls C the structural bottleneck; this
    is where that bites.

    ``locked`` pins players already committed to a slot. Under the lock-in
    mechanic a player must stay in his starting slot for his banked score to
    stand (§7.6), so once he is locked his slot is no longer free — the nightly
    assignment is a real commitment, not an option to be resolved later.
    """
    locked = dict(locked or {})

    # Slot identity has to survive duplicates: this league starts two UTILs, and
    # a dict keyed on the bare name would silently drop one.
    keys = [_slot_key(slots, i) for i in range(len(slots))]
    open_keys = [k for k in keys if k not in locked]
    open_slots = [slots[keys.index(k)] for k in open_keys]

    taken = set(locked.values())
    free = [p for p in candidates if p not in taken]
    if len(free) < len(open_keys):
        raise NoValidLineup(f"{len(free)} players available for {len(open_keys)} open slots")

    # linear_sum_assignment minimises, so negate. Ineligible pairs get a cost
    # large enough that the solver only uses one if there is no legal lineup at
    # all, which is then detected rather than returned.
    cost = np.zeros((len(open_keys), len(free)))
    for i, slot in enumerate(open_slots):
        for j, player in enumerate(free):
            if eligible(
                slot, positions.get(player, ()), sleeper_id=player, extra_slots=extra_slots
            ):
                cost[i, j] = -float(values.get(player, 0.0))
            else:
                cost[i, j] = math.inf

    finite = np.isfinite(cost)
    if not finite.any(axis=1).all():
        empty = [open_slots[i] for i in np.nonzero(~finite.any(axis=1))[0]]
        raise NoValidLineup(f"no eligible player for slot(s) {empty}")

    # scipy rejects inf; use a penalty strictly worse than any legal lineup.
    penalty = float(np.abs(cost[finite]).sum() + 1.0) if finite.any() else 1.0
    cost = np.where(finite, cost, penalty)

    rows, cols = linear_sum_assignment(cost)
    out = dict(locked)
    for i, j in zip(rows, cols, strict=True):
        if not finite[i, j]:
            raise NoValidLineup(f"no legal lineup: {open_slots[i]} cannot be filled")
        out[open_keys[i]] = free[j]
    return out


def _slot_key(slots: Sequence[str], index: int) -> str:
    """Stable identity for a slot, disambiguating repeats (``UTIL`` twice)."""
    name = slots[index]
    if slots.count(name) == 1:
        return name
    return f"{name}#{slots[:index].count(name) + 1}"
