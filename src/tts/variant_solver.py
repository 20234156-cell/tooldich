"""Select measured speech variants in source order without dropping units."""
from dataclasses import dataclass


@dataclass(frozen=True)
class SpeechVariant:
    key: str
    samples: int
    penalty: int


@dataclass(frozen=True)
class SpeechUnit:
    cue_id: int
    earliest: int
    latest_start: int
    deadline: int
    variants: tuple[SpeechVariant, ...]


def solve_variants(units, pause):
    """Minimize total penalty over the supplied WAVs and fixed unit order.

    Earlier completion at equal or lower penalty dominates a later state.
    Earliest placement suffices because all constraints are lower bounds on
    starts and upper bounds on starts and ends. Use sample-exact bounds.
    """
    if pause < 0:
        raise ValueError('Negative pause')
    units = list(units)
    ids = {unit.cue_id for unit in units}
    if len(ids) != len(units):
        raise ValueError('Duplicate speech unit')
    for unit in units:
        if not 0 <= unit.earliest <= unit.latest_start < unit.deadline:
            raise ValueError('Invalid speech window')
        keys = {variant.key for variant in unit.variants}
        if len(keys) != len(unit.variants) or any(v.samples <= 0 or v.penalty < 0 for v in unit.variants):
            raise ValueError('Invalid measured variants')

    # end, penalty, linked placement history
    frontier = [(0, 0, None)]
    for index, unit in enumerate(units):
        candidates = []
        shortages = []
        for end, penalty, history in frontier:
            start = max(unit.earliest, end + (pause if index else 0))
            for variant in unit.variants:
                finish = start + variant.samples
                if start > unit.latest_start or finish > unit.deadline:
                    shortages.append((max(0, finish - unit.deadline), max(0, start - unit.latest_start)))
                    continue
                row = dict(cue_id=unit.cue_id, variant=variant.key, start=start, end=finish)
                candidates.append((finish, penalty + variant.penalty, (row, history)))
        if not candidates:
            return dict(status='infeasible', first_blocked_id=unit.cue_id,
                        shortages=[dict(end=s[0], start=s[1]) for s in sorted(set(shortages))],
                        reason='window_conflict' if unit.variants else 'no_measured_variant')
        frontier = []
        best_penalty = None
        for state in sorted(candidates, key=lambda item: item[:2]):
            if best_penalty is None or state[1] < best_penalty:
                frontier.append(state)
                best_penalty = state[1]
    _, penalty, history = min(frontier, key=lambda state: (state[1], state[0]))
    placements = []
    while history:
        row, history = history
        placements.append(row)
    return dict(status='placed', penalty=penalty, placements=placements[::-1])
