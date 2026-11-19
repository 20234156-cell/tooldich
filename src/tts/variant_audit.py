"""Compare sample-exact strict and QA timing limits without authorizing borrowing."""
from dataclasses import replace
import math

from src.tts.variant_solver import solve_variants
from src.tts.variant_config import VariantTimingConfig


def partition_independent_units(units, pause_frames, *, bounds=None, minimum_block_size=64):
    """Partition only across redundant adjacent nonoverlap constraints.

    bounds, when supplied, are actual integer start bounds including drift and
    end_limit. Nominal deadline/earliest are valid only for unexpanded windows.
    Group small components to amortize solver startup; never invent a boundary.
    """
    units = list(units)
    blocks, begin = [], 0
    for i in range(len(units)-1):
        if bounds is None:
            independent = units[i].deadline + pause_frames <= units[i+1].earliest
        else:
            frames = min(v.samples for v in units[i].variants)
            independent = bounds[i][1] + frames + pause_frames <= bounds[i+1][0]
        if independent and i+1-begin >= minimum_block_size:
            blocks.append(units[begin:i+1]); begin = i+1
    if begin < len(units): blocks.append(units[begin:])
    return blocks


def minimum_drift_witness(units, pause, sample_rate, config=None, *, end_limit=None,
                          source_ends=None):
    """Minimum global mean over provably independent blocks; QA stays global."""
    from scipy.optimize import linprog
    from scipy.sparse import lil_matrix

    config = config or VariantTimingConfig()
    units = list(units)
    if not units or sample_rate <= 0 or pause < 0 or any(not u.variants for u in units):
        raise ValueError('Invalid measured units or timing parameters')
    shortest = [min(u.variants,key=lambda v:(v.samples,v.penalty)) for u in units]
    drift_ends = [u.deadline if source_ends is None else source_ends[u.cue_id] for u in units]
    cap = math.floor(config.max_drift_seconds*sample_rate)
    bounds = []
    for unit,variant,drift_end in zip(units,shortest,drift_ends):
        lower = max(0,unit.earliest-cap)
        upper = min(unit.earliest+cap,drift_end+cap-variant.samples)
        if end_limit is not None: upper = min(upper,end_limit-variant.samples)
        if upper < lower:
            return dict(status='max_drift_infeasible',cue_id=unit.cue_id,missing_seconds=(lower-upper)/sample_rate)
        bounds.append((lower,upper))
    blocks = partition_independent_units(units,pause,bounds=bounds)
    starts, objectives, offset = [], [], 0
    for block in blocks:
        count = len(block)
        variants = shortest[offset:offset+count]
        ends = drift_ends[offset:offset+count]
        matrix = lil_matrix((4*count-1,2*count)); rhs=[]
        for i,(unit,variant,drift_end) in enumerate(zip(block,variants,ends)):
            anchor=unit.earliest/sample_rate
            for sign,limit in ((1,anchor),(-1,-anchor),(1,(drift_end-variant.samples)/sample_rate)):
                matrix[len(rhs),i]=sign; matrix[len(rhs),count+i]=-1; rhs.append(limit)
        for i,variant in enumerate(variants[:-1]):
            matrix[len(rhs),i]=1; matrix[len(rhs),i+1]=-1
            rhs.append(-(variant.samples+pause)/sample_rate)
        local_bounds=[(lo/sample_rate,hi/sample_rate) for lo,hi in bounds[offset:offset+count]]
        result=linprog([0]*count+[1]*count,A_ub=matrix.tocsr(),b_ub=rhs,
            bounds=local_bounds+[(0,None)]*count,method='highs',options={'time_limit':30.0})
        if result.status==2:return dict(status='max_drift_infeasible')
        if not result.success:return dict(status='solver_unproven',reason=result.message)
        starts.extend(result.x[:count]);objectives.append(result.fun);offset+=count
    count=len(units)
    mean_lower_bound=sum(objectives)/count
    if mean_lower_bound > config.mean_drift_seconds+1e-8:
        return dict(status='mean_drift_infeasible',mean_lower_bound_seconds=mean_lower_bound)
    placements, drifts = [], []
    free = 0
    for i,(unit,variant,drift_end) in enumerate(zip(units,shortest,drift_ends)):
        start = max(free,math.ceil(starts[i]*sample_rate-1e-7))
        end = start+variant.samples
        drift = max(abs(start-unit.earliest),end-drift_end)/sample_rate
        placements.append(dict(cue_id=unit.cue_id,variant=variant.key,start=start,end=end))
        drifts.append(drift);free=end+pause
    maximum,mean=max(drifts),sum(drifts)/count
    status=('candidate_meets_timing' if maximum<=config.max_drift_seconds and mean<=config.mean_drift_seconds
            else 'sample_rounding_unproven')
    return dict(status=status,placements=placements,max_drift_seconds=maximum,mean_drift_seconds=mean,
        mean_lower_bound_seconds=mean_lower_bound,needs_source_review=any(
            r['start']<u.earliest or r['end']>u.deadline for r,u in zip(placements,units)))


def compare_timing_modes(units, pause, sample_rate, config=None):
    units = list(units)
    config = config or VariantTimingConfig()
    results = {}
    for name, tolerance, mean_limit in (
        ('strict_40ms',config.strict_seconds,config.strict_seconds),
        ('qa_timing_only',config.max_drift_seconds,config.mean_drift_seconds),
    ):
        allowance = math.floor(tolerance * sample_rate)
        expanded = [replace(unit, earliest=max(0,unit.earliest-allowance),
                            latest_start=min(unit.earliest+allowance,unit.deadline+allowance-1),
                            deadline=unit.deadline+allowance) for unit in units]
        result = solve_variants(expanded,pause)
        if result['status'] == 'placed':
            # Earliest placement proves feasibility under the max cap. Move
            # the chosen WAVs closer to the original anchors where the next
            # placement leaves space. This is a witness, not a mean optimum.
            next_start = None
            for row, unit, bound in reversed(list(zip(result['placements'],units,expanded))):
                length = row['end']-row['start']
                balanced = min(unit.earliest,(unit.earliest+unit.deadline-length)//2)
                latest = min(bound.latest_start,bound.deadline-length)
                if next_start is not None:
                    latest = min(latest,next_start-pause-length)
                row['start'] = max(row['start'],min(latest,balanced))
                row['end'] = row['start']+length
                next_start = row['start']
            drift = [max(abs(row['start']-unit.earliest),row['end']-unit.deadline)/sample_rate
                     for row,unit in zip(result['placements'],units)]
            result['max_drift_seconds'] = max(drift,default=0.)
            result['mean_drift_seconds'] = sum(drift)/len(drift) if drift else 0.
            result['status'] = ('candidate_meets_timing' if result['mean_drift_seconds'] <= mean_limit
                                else 'mean_unproven')
            result['needs_source_review'] = any(row['start'] < unit.earliest or row['end'] > unit.deadline
                                                for row,unit in zip(result['placements'],units))
        else:
            result['min_end_shortage_seconds'] = min((r['end']/sample_rate for r in result['shortages']),default=None)
            result['min_start_shortage_seconds'] = min((r['start']/sample_rate for r in result['shortages']),default=None)
        results[name] = result
    return results
