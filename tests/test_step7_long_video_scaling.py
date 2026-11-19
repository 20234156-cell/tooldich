"""Offline equivalence oracle and measured long-video scheduler acceptance gate."""
import math
import time
from dataclasses import replace
import numpy as np
import pytest
import soundfile as sf
from src.tts.variant_config import VariantTimingConfig
from src.tts.variant_solver import SpeechUnit, SpeechVariant
from src.tts.variant_audit import minimum_drift_witness, partition_independent_units
from src.tts.cluster_schedule import MeasuredUtterance, source_clusters, solve_measured

def _legacy_witness(units, pause, sample_rate, config=None, *, end_limit=None,
                          source_ends=None):
    """Find a minimum-mean continuous schedule using shortest measured WAVs.

    A longer WAV at the same start cannot improve overlap or drift. Thus the
    optimum on the shortest variants is a lower bound for every other choice.
    Only publish a witness after sample rounding and independent QA checks.
    """
    from scipy.optimize import linprog
    from scipy.sparse import lil_matrix

    config = config or VariantTimingConfig()
    units = list(units)
    if not units or sample_rate <= 0 or pause < 0 or any(not u.variants for u in units):
        raise ValueError('Invalid measured units or timing parameters')
    shortest = [min(u.variants,key=lambda v:(v.samples,v.penalty)) for u in units]
    drift_ends = [u.deadline if source_ends is None else source_ends[u.cue_id] for u in units]
    count = len(units)
    bounds = []
    matrix = lil_matrix((4*count-1,2*count))
    rhs = []
    cap = math.floor(config.max_drift_seconds*sample_rate)
    for i,(unit,variant,drift_end) in enumerate(zip(units,shortest,drift_ends)):
        lower = max(0,unit.earliest-cap)
        upper = min(unit.earliest+cap,drift_end+cap-variant.samples)
        if end_limit is not None:
            upper = min(upper, end_limit - variant.samples)
        if upper < lower:
            return dict(status='max_drift_infeasible',cue_id=unit.cue_id,missing_seconds=(lower-upper)/sample_rate)
        bounds.append((lower/sample_rate,upper/sample_rate))
        anchor = unit.earliest/sample_rate
        for sign,limit in ((1,anchor),(-1,-anchor),(1,(drift_end-variant.samples)/sample_rate)):
            matrix[len(rhs),i] = sign
            matrix[len(rhs),count+i] = -1
            rhs.append(limit)
    for i,variant in enumerate(shortest[:-1]):
        matrix[len(rhs),i] = 1
        matrix[len(rhs),i+1] = -1
        rhs.append(-(variant.samples+pause)/sample_rate)
    result = linprog([0]*count+[1]*count,A_ub=matrix.tocsr(),b_ub=rhs,
                     bounds=bounds+[(0,None)]*count,method='highs')
    if result.status == 2:
        return dict(status='max_drift_infeasible')
    if not result.success:
        return dict(status='solver_unproven',reason=result.message)
    mean_lower_bound = result.fun/count
    if mean_lower_bound > config.mean_drift_seconds+1e-8:
        return dict(status='mean_drift_infeasible',mean_lower_bound_seconds=mean_lower_bound)
    placements, drifts = [], []
    free = 0
    for i,(unit,variant,drift_end) in enumerate(zip(units,shortest,drift_ends)):
        start = max(free,math.ceil(result.x[i]*sample_rate-1e-7))
        end = start+variant.samples
        drift = max(abs(start-unit.earliest),end-drift_end)/sample_rate
        placements.append(dict(cue_id=unit.cue_id,variant=variant.key,start=start,end=end))
        drifts.append(drift)
        free = end+pause
    maximum,mean = max(drifts),sum(drifts)/count
    status = ('candidate_meets_timing' if maximum<=config.max_drift_seconds and mean<=config.mean_drift_seconds
              else 'sample_rounding_unproven')
    return dict(status=status,placements=placements,max_drift_seconds=maximum,mean_drift_seconds=mean,
                mean_lower_bound_seconds=mean_lower_bound,needs_source_review=any(
                    r['start']<u.earliest or r['end']>u.deadline for r,u in zip(placements,units)))



def utterances(count=200):
    sr=1000
    return [SpeechUnit(i,10000+i*10000,10000+i*10000,10800+i*10000,
            (SpeechVariant('measured',800+(i%7)*30,0),)) for i in range(count)]


def test_two_hundred_units_exact_monolithic_equivalence():
    units=utterances()
    old=_legacy_witness(units,40,1000)
    new=minimum_drift_witness(units,40,1000)
    assert new['status']==old['status']=='candidate_meets_timing'
    assert new['placements']==old['placements']
    assert new['max_drift_seconds']==old['max_drift_seconds']
    assert new['mean_drift_seconds']==old['mean_drift_seconds']


@pytest.mark.parametrize('failure',['max','mean'])
def test_infeasible_equivalence(failure):
    units=utterances()
    frames=5000 if failure=='max' else 1600
    units=[replace(u,variants=(SpeechVariant('too_long',frames,0),)) for u in units]
    assert minimum_drift_witness(units,40,1000)['status']==_legacy_witness(units,40,1000)['status']


def test_mean_gate_remains_global_not_per_partition():
    units=utterances(128)
    units=[replace(u,variants=(SpeechVariant('wav',1800 if i<64 else 800,0),)) for i,u in enumerate(units)]
    new=minimum_drift_witness(units,40,1000);old=_legacy_witness(units,40,1000)
    assert new['status']==old['status']=='candidate_meets_timing'
    assert new['mean_drift_seconds']==old['mean_drift_seconds']==.25
    assert new['placements']==old['placements']


def test_nominal_gap_is_not_proof_when_drift_expands_windows():
    units=[SpeechUnit(1,1000,1000,2000,(SpeechVariant('wav',1400,0),)),
           SpeechUnit(2,2100,2100,3100,(SpeechVariant('wav',600,0),))]
    assert len(partition_independent_units(units,40,minimum_block_size=1))==2
    assert len(partition_independent_units(units,40,bounds=[(0,2100),(600,3600)],minimum_block_size=1))==1
    assert minimum_drift_witness(units,40,1000)==_legacy_witness(units,40,1000)


def test_overlapping_gap_queries_preserve_original_scan():
    from src.tts.cluster_schedule import _GapIndex
    rng=np.random.default_rng(33)
    gaps=sorted((int(a),int(a+d)) for a,d in zip(rng.integers(0,20000,2000),rng.integers(1,10000,2000)))
    index=_GapIndex(gaps,40)
    for start in range(0,25000,37):
        end=start+400;left,right=start,end
        for a,b in gaps:
            if a<start<=b:left=min(left,max(a,start-800))
            if a<=end<b:right=max(right,min(b,end+800))
        assert index.expand(start,end,800)==(left,right)
        expected=next(((a,b) for a,b in gaps if start<=a<b<=end and b-a>=40),None)
        assert index.first_between(start,end)==expected


def test_source_clusters_matches_legacy_for_nested_unsorted_gaps():
    rng=np.random.default_rng(7)
    units=[MeasuredUtterance(i,i*1000,i*1000+int(rng.integers(100,3500)),500) for i in range(200)]
    gaps=[(i*1000+600,i*1000+900) for i in range(200)]+[(500,7000),(500,1000)]
    expected=[];current=[]
    for u in units:
        if current and any(max(x.end for x in current)<=a<b<=u.start and b-a>=40 for a,b in gaps):
            expected.append(current);current=[]
        current.append(u)
    if current:expected.append(current)
    assert source_clusters(units,list(reversed(gaps)),40)==expected


def test_eight_thousand_units_under_five_seconds():
    sr=1000
    units=[];gaps=[]
    for i in range(8000):
        start=10000+(i//64)*(64*2200+4000)+(i%64)*2200
        units.append(SpeechUnit(i,start,start,start+800,(SpeechVariant('wav',800+(i%5)*20,0),)))
        if i:gaps.append((units[-2].deadline,start))
    measured=[MeasuredUtterance(u.cue_id,u.earliest,u.deadline,u.variants[0].samples) for u in units]
    started=time.perf_counter()
    result=minimum_drift_witness(units,40,sr)
    clusters=source_clusters(measured,gaps,40)
    elapsed=time.perf_counter()-started
    print(f'BENCHMARK 8000: {elapsed:.6f}s; clusters={len(clusters)}; duration={units[-1].deadline/sr:.1f}s')
    assert result['status']=='candidate_meets_timing'
    assert len(result['placements'])==8000
    assert elapsed<5.0


def test_bounded_uses_header_frames_and_only_reads_each_wav_once(tmp_path,monkeypatch):
    from tests.test_bounded_schedule import measured_case
    import src.tts.bounded_schedule as bounded
    cues,rows=measured_case(tmp_path)
    read=sf.read;calls=[]
    def tracked(path,*args,**kwargs):calls.append(path);return read(path,*args,**kwargs)
    monkeypatch.setattr(bounded.sf,'read',tracked)
    result=bounded.reschedule_measured(tmp_path,cues,rows)
    assert result['status']=='candidate_meets_timing'
    assert result['timeline'][0]['clip_duration']==2.3
    assert len(calls)==len(rows)  # publication only; no whole-file preflight decode


@pytest.mark.parametrize('bad',['silent','nan'])
def test_header_only_does_not_bypass_audio_qa(tmp_path,bad):
    from tests.test_bounded_schedule import measured_case
    from src.tts.bounded_schedule import reschedule_measured
    cues,rows=measured_case(tmp_path)
    sf.write(tmp_path/rows[0]['clip_path'],np.full(2300,0. if bad=='silent' else np.nan),1000,subtype='FLOAT')
    with pytest.raises(ValueError,match='Invalid or silent'):
        reschedule_measured(tmp_path,cues,rows)


@pytest.mark.parametrize('solver',['bounded','source'])
def test_solver_time_limit_never_publishes_partial_schedule(monkeypatch,solver):
    from types import SimpleNamespace
    calls=[]
    def stopped(*args,**kwargs):
        calls.append(kwargs['options']['time_limit'])
        return SimpleNamespace(success=False,status=1,message='Time limit reached')
    monkeypatch.setattr('scipy.optimize.linprog',stopped)
    if solver=='bounded':
        result=minimum_drift_witness(utterances(2),40,1000)
        assert calls==[30.0]
    else:
        result=solve_measured([MeasuredUtterance(1,1000,2000,500)],1000,4000,40)
        assert calls==[45.0]
    assert result['status']=='solver_unproven'
    assert not result.get('placements')


def test_coupled_dense_blocks_preserve_optimum_and_feasibility():
    # Multiple optimal LP witnesses can have different individual starts.
    units=[]
    for i in range(200):
        start=10000+(i//4)*12000+(i%4)*900
        units.append(SpeechUnit(i,start,start,start+800,(SpeechVariant('wav',950,0),)))
    new=minimum_drift_witness(units,40,1000)
    old=_legacy_witness(units,40,1000)
    assert new['status']==old['status']
    assert new['mean_lower_bound_seconds']==pytest.approx(old['mean_lower_bound_seconds'],abs=1e-10)
    if new['status']=='candidate_meets_timing':
        assert new['mean_drift_seconds']==pytest.approx(old['mean_drift_seconds'],abs=.001)
        assert new['max_drift_seconds']<=1.5
        for a,b in zip(new['placements'],new['placements'][1:]):
            assert a['end']+40<=b['start']


def test_missing_header_frame_count_falls_back_without_changing_schedule(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from tests.test_bounded_schedule import measured_case
    import src.tts.bounded_schedule as bounded
    cues,rows=measured_case(tmp_path)
    info=sf.info
    def missing(path):
        value=info(path)
        return SimpleNamespace(samplerate=value.samplerate,channels=value.channels,frames=None) if Path(path).parent.name=='clips' else value
    from pathlib import Path
    monkeypatch.setattr(bounded.sf,'info',missing)
    result=bounded.reschedule_measured(tmp_path,cues,rows)
    assert result['status']=='candidate_meets_timing'
    assert result['timeline'][0]['clip_duration']==2.3
