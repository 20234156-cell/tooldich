"""Sample-based source-window scheduling, minimax first and mean as a hard gate.

No provider, database writes or audio edits belong in this module. Silence
intervals supplied here must already have been verified against the source.
An empty list means unknown, never permission to borrow.
"""
from dataclasses import dataclass
import math
from bisect import bisect_left, bisect_right


@dataclass(frozen=True)
class MeasuredUtterance:
    cue_id: int
    start: int
    end: int
    frames: int


class _GapIndex:
    """Exact queries for overlapping/nested intervals, without scanning prefixes."""
    def __init__(self, gaps, pause=0):
        self.gaps = sorted(gaps)
        self.starts = [a for a,b in self.gaps]
        self.prefix_end = []
        maximum = -math.inf
        for a,b in self.gaps:
            maximum=max(maximum,b);self.prefix_end.append(maximum)
        self.size=1
        while self.size < len(self.gaps): self.size*=2
        self.tree=[math.inf]*(2*self.size)
        for i,(a,b) in enumerate(self.gaps):
            if a < b and b-a >= pause:self.tree[self.size+i]=b
        for i in range(self.size-1,0,-1):self.tree[i]=min(self.tree[2*i],self.tree[2*i+1])

    def first_between(self, lower, upper):
        begin=bisect_left(self.starts,lower)
        def find(node,left,right):
            if right<=begin or self.tree[node]>upper:return None
            if right-left==1:return self.gaps[left]
            middle=(left+right)//2
            return find(2*node,left,middle) or find(2*node+1,middle,right)
        return find(1,0,self.size)

    def expand(self, start, end, borrow):
        left,right=start,end
        stop=bisect_left(self.starts,start)
        if stop and self.prefix_end[stop-1]>=start:
            first=bisect_left(self.prefix_end,start,0,stop)
            left=min(left,max(self.starts[first],start-borrow))
        stop=bisect_right(self.starts,end)
        if stop and self.prefix_end[stop-1]>end:
            right=max(right,min(self.prefix_end[stop-1],end+borrow))
        return left,right


def _source_clusters(units,index):
    clusters,current=[],[]
    current_max_end=0
    for unit in units:
        if current and index.first_between(current_max_end,unit.start) is not None:
            clusters.append(current);current=[]
        current_max_end=max(current_max_end,unit.end) if current else unit.end
        current.append(unit)
    if current:clusters.append(current)
    return clusters


def source_clusters(units, verified_gaps, pause):
    return _source_clusters(units,_GapIndex(verified_gaps,pause))


def solve_measured(units, sample_rate, duration_frames, pause, verified_gaps=(),
                   max_borrow_seconds=.8, fixed_starts=None,
                   max_drift_seconds=1.5, mean_drift_seconds=.3, verified_windows=None):
    """Solve a complete ordered schedule; never return partial work as success.

    Minimize the maximum drift subject to the mean gate, then minimize total
    drift at that maximum. All bounds and durations are integer samples.
    Fixed starts allow independently completed clusters to remain unchanged.
    """
    import numpy as np
    from scipy.optimize import linprog
    from scipy.sparse import lil_matrix

    units = list(units)
    fixed_starts = fixed_starts or {}
    verified_windows = verified_windows or {}
    scalars = (sample_rate, duration_frames, pause, max_borrow_seconds,
               max_drift_seconds, mean_drift_seconds)
    if (not all(math.isfinite(x) for x in scalars) or sample_rate <= 0
            or duration_frames <= 0 or pause < 0 or not 0 <= max_borrow_seconds <= .8
            or not 0 <= mean_drift_seconds <= .3 or not 0 <= max_drift_seconds <= 1.5):
        raise ValueError('Invalid scheduling policy')
    if not units:
        return dict(status='candidate_meets_timing', placements=[], clusters=[],
                    max_drift_seconds=0., mean_drift_seconds=0.)
    if (len({u.cue_id for u in units}) != len(units)
            or any(not all(type(x) is int for x in (u.cue_id,u.start,u.end,u.frames))
                   or not 0 <= u.start < u.end <= duration_frames or u.frames <= 0 for u in units)
            or any(a.start > b.start for a,b in zip(units,units[1:]))):
        raise ValueError('Invalid measured utterances')
    gaps = sorted(verified_gaps)
    if any(not all(type(x) is int for x in (a,b)) or not 0 <= a < b <= duration_frames
           for a,b in gaps):
        raise ValueError('Invalid verified source intervals')
    known = {u.cue_id for u in units}
    if (not set(verified_windows).issubset(known) or any(
            not all(type(x) is int for x in (a,b)) or not 0<=a<b<=duration_frames
            for spans in verified_windows.values() for a,b in spans)):
        raise ValueError('Invalid verified speech windows')
    if not set(fixed_starts).issubset(known) or any(type(v) is not int for v in fixed_starts.values()):
        raise ValueError('Invalid frozen placements')

    gap_index = _GapIndex(gaps,pause)
    blocks = _source_clusters(units,gap_index)
    # A reset lies inside confirmed silence. Neither neighboring block may
    # cross it, even if the global QA cap would allow a later placement.
    cuts = []
    for left,right in zip(blocks,blocks[1:]):
        a,b = gap_index.first_between(max(u.end for u in left),right[0].start)
        cuts.append((a+b)//2)
    block_bounds = {}
    for i,block in enumerate(blocks):
        for unit in block:
            block_bounds[unit.cue_id] = (cuts[i-1] if i else 0,
                                         cuts[i] if i<len(cuts) else duration_frames)
    borrow = math.floor(max_borrow_seconds*sample_rate)
    cap = max(0, math.floor(max_drift_seconds*sample_rate)-1)/sample_rate
    bounds, shortage,legal_windows = [], [],[]
    for unit in units:
        left,right = gap_index.expand(unit.start,unit.end,borrow)
        for a,b in verified_windows.get(unit.cue_id,()):
            if a < unit.start <= b:left=min(left,max(a,unit.start-borrow))
            if a <= unit.end < b:right=max(right,min(b,unit.end+borrow))
        block_left,block_right = block_bounds[unit.cue_id]
        lo = max(left,block_left,0)
        hi = min(right,block_right,duration_frames)-unit.frames
        legal_windows.append(dict(cue_id=unit.cue_id,start=lo,end=hi+unit.frames))
        if unit.cue_id in fixed_starts:
            fixed = fixed_starts[unit.cue_id]
            if not lo <= fixed <= hi:
                raise ValueError('Frozen placement no longer matches source windows')
            lo = hi = fixed
        if hi < lo:
            shortage.append(dict(cue_id=unit.cue_id,reason='window_capacity',
                                 missing_seconds=(lo-hi)/sample_rate))
        bounds.append((lo/sample_rate,hi/sample_rate))
    if shortage:
        return dict(status='source_window_infeasible',conflicts=shortage,
                    windows=legal_windows,
                    clusters=[[u.cue_id for u in block] for block in blocks])

    n = len(units)
    matrix = lil_matrix((6*n,2*n+1))
    rhs = []
    for i,unit in enumerate(units):
        anchor = unit.start/sample_rate
        for sign,limit in ((1,anchor),(-1,-anchor),
                           (1,(unit.end-unit.frames)/sample_rate)):
            row = len(rhs); matrix[row,i]=sign; matrix[row,n+i]=-1; rhs.append(limit)
        row=len(rhs); matrix[row,n+i]=1; matrix[row,2*n]=-1; rhs.append(0.)
    for i,unit in enumerate(units[:-1]):
        row=len(rhs); matrix[row,i]=1; matrix[row,i+1]=-1
        rhs.append(-(unit.frames+pause)/sample_rate)
    row=len(rhs)
    for i in range(n):
        matrix[row,n+i]=1
    # One-sample rounding can increase a drift; reserve that amount rather
    # than silently relaxing the acceptance threshold after rounding.
    rhs.append(max(0.,mean_drift_seconds-1/sample_rate)*n)
    a = matrix[:len(rhs)].tocsr()
    all_bounds = bounds+[(0,None)]*n+[(0,cap)]
    primary = linprog([0.]*(2*n)+[1.],A_ub=a,b_ub=rhs,bounds=all_bounds,method='highs',options={'time_limit':45.0})
    if not primary.success:
        return dict(status='constraints_infeasible' if primary.status==2 else 'solver_unproven',
                    reason='order_or_pause_or_qa',
                    windows=legal_windows,
                    clusters=[[u.cue_id for u in block] for block in blocks])
    all_bounds[-1]=(0,min(cap,primary.x[-1]+1e-8))
    secondary = linprog([0.]*n+[1.]*n+[0.],A_ub=a,b_ub=rhs,bounds=all_bounds,method='highs',options={'time_limit':45.0})
    if not secondary.success:
        return dict(status='solver_unproven',reason='secondary_optimization')
    placements,drifts = [],[]
    previous_end = -pause
    for i,unit in enumerate(units):
        start = max(previous_end+pause,math.ceil(secondary.x[i]*sample_rate-1e-7))
        end = start+unit.frames
        drift = max(abs(start-unit.start),end-unit.end,0)/sample_rate
        lo,hi = bounds[i]
        if not math.ceil(lo*sample_rate-1e-7) <= start <= math.floor(hi*sample_rate+1e-7):
            return dict(status='sample_rounding_unproven',cue_id=unit.cue_id)
        placements.append(dict(cue_id=unit.cue_id,start=start,end=end,drift=drift))
        drifts.append(drift);previous_end=end
    maximum,mean=max(drifts),float(np.mean(drifts))
    if maximum>max_drift_seconds or mean>mean_drift_seconds:
        return dict(status='sample_rounding_unproven',max_drift_seconds=maximum,
                    mean_drift_seconds=mean)
    return dict(status='candidate_meets_timing',placements=placements,
                windows=legal_windows,
                max_drift_seconds=maximum,mean_drift_seconds=mean,
                minimax_lower_bound_seconds=float(primary.fun),
                clusters=[[u.cue_id for u in block] for block in blocks])
