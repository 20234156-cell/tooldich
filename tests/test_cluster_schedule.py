import pytest

from src.tts.cluster_schedule import MeasuredUtterance as U, solve_measured, source_clusters


def solve(units, **kwargs):
    return solve_measured(units,1000,10000,40,**kwargs)


def test_empty_and_single_measured_cue():
    assert solve([])['placements']==[]
    result=solve([U(1,1000,2000,800)])
    assert result['status']=='candidate_meets_timing'
    assert result['placements'][0]['start']==1000
    assert result['max_drift_seconds']==0


def test_no_silence_evidence_means_no_borrow_even_if_total_capacity_is_large():
    result=solve([U(1,0,1000,4000),U(2,1000,2000,1000)])
    assert result['status']=='source_window_infeasible'
    assert result['conflicts'][0]==dict(cue_id=1,reason='window_capacity',missing_seconds=3.)


def test_minimax_accounts_for_tail_and_preserves_mean_gate():
    units=[U(1,1000,2000,2000),U(2,4000,5000,200),U(3,7000,8000,200)]
    result=solve(units,verified_gaps=[(0,1000),(2000,3000)])
    assert result['status']=='candidate_meets_timing'
    first=result['placements'][0]
    assert first['start']==500 and first['end']==2500
    assert result['max_drift_seconds']==.5
    assert result['mean_drift_seconds']<=.3
    # The identical long utterance alone fails mean, despite passing max1.5.
    assert solve(units[:1],verified_gaps=[(0,1000),(2000,3000)])['status']=='constraints_infeasible'


def test_silence_is_capped_at_eight_tenths_not_all_available_silence():
    result=solve([U(1,3000,4000,2700)],verified_gaps=[(0,3000),(4000,9000)])
    assert result['status']=='source_window_infeasible'
    assert result['conflicts'][0]['missing_seconds']==.1


def test_short_followers_are_placed_without_carried_delay():
    units=[U(1,1000,2000,1400),U(2,2100,3000,400),U(3,5000,6000,300)]
    result=solve(units,verified_gaps=[(500,1000),(2000,2100)])
    assert result['status']=='candidate_meets_timing'
    assert result['placements'][1]['start']==2100
    assert result['placements'][2]['start']==5000
    assert result['placements'][0]['end']+40<=result['placements'][1]['start']


def test_verified_anchor_and_frozen_cluster_are_respected():
    units=[U(1,1000,2000,500),U(2,5000,6000,500)]
    assert len(source_clusters(units,[],40))==1
    result=solve(units,verified_gaps=[(2000,5000)],fixed_starts={1:1100})
    assert result['status']=='candidate_meets_timing'
    assert result['clusters']==[[1],[2]]
    assert result['placements'][0]['start']==1100
    assert result['placements'][1]['start']==5000
    with pytest.raises(ValueError,match='Frozen'):
        solve(units,fixed_starts={1:1800})


@pytest.mark.parametrize('units',[
    [U(1,1000,1000,100)],
    [U(1,1000,2000,0)],
    [U(1,1000,2000,100),U(1,3000,4000,100)],
    [U(2,3000,4000,100),U(1,1000,2000,100)],
])
def test_invalid_source_or_waveforms_are_refused(units):
    with pytest.raises(ValueError):solve(units)


def test_identical_start_times_do_not_allow_speech_overlap():
    result=solve([U(1,1000,2000,200),U(2,1000,2000,200)])
    assert result['status']=='candidate_meets_timing'
    left,right=result['placements']
    assert right['start']>=left['end']+40


def test_nested_source_window_cannot_be_cut_by_a_false_anchor():
    units=[U(1,0,9000,100),U(2,2000,3000,100),U(3,5000,6000,100)]
    assert len(source_clusters(units,[(3000,5000)],40))==1
