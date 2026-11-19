"""Find independent speech blocks and select a diagnostic pilot."""
from collections import deque


def independent_blocks(units, pause, confirmed_gaps):
    """Split only when approved silence covers a gap between legal schedules."""
    units = list(units)
    if pause < 0 or any(a.earliest > b.earliest for a,b in zip(units,units[1:])):
        raise ValueError('Invalid pause or unordered units')
    blocks = []
    current = []
    latest_end = 0
    for unit in units:
        if (current and latest_end + pause <= unit.earliest
                and any(a <= latest_end and unit.earliest <= b for a,b in confirmed_gaps)):
            blocks.append(current)
            current = []
            latest_end = 0
        current.append(unit)
        latest_end = max(latest_end,unit.deadline)
    if current:
        blocks.append(current)
    return blocks


def choose_pilot(blocks, max_units):
    """Find the contiguous pilot with highest summed minimum own-window deficit."""
    if max_units < 1:
        raise ValueError('Pilot size must be positive')
    best_score, best = -1, []
    for block in blocks:
        queue = deque()
        score = 0
        for unit in block:
            if not unit.variants:
                raise ValueError(f'Missing measured audio for unit {unit.cue_id}')
            shortest = min(v.samples for v in unit.variants)
            deficit = max(0,shortest - (unit.deadline-unit.earliest))
            queue.append((unit,deficit))
            score += deficit
            if len(queue) > max_units:
                score -= queue.popleft()[1]
            if score > best_score:
                best_score,best = score,[item for item,_ in queue]
    return best, max(0,best_score)
