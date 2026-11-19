import pytest

from src.tts.variant_solver import SpeechUnit, SpeechVariant, solve_variants


def test_selects_shorter_earlier_variant_only_when_required():
    units = [
        SpeechUnit(1, 0, 0, 14, (SpeechVariant('full', 12, 0), SpeechVariant('compact', 8, 1))),
        SpeechUnit(2, 10, 11, 20, (SpeechVariant('full', 9, 0),)),
        SpeechUnit(3, 40, 40, 60, (SpeechVariant('full', 10, 0), SpeechVariant('compact', 5, 1))),
    ]
    result = solve_variants(units, pause=2)
    assert result['penalty'] == 1
    assert [(r['variant'], r['start'], r['end']) for r in result['placements']] == [
        ('compact', 0, 8), ('full', 10, 19), ('full', 40, 50)]


def test_impossible_variants_report_exact_shortage_without_partial_timeline():
    unit = SpeechUnit(55, 1000, 1000, 1500, (SpeechVariant('core', 670, 3),))
    result = solve_variants([unit], pause=40)
    assert result['first_blocked_id'] == 55
    assert result['shortages'] == [{'end': 170, 'start': 0}]
    assert 'placements' not in result


def test_shorter_more_costly_option_does_not_replace_a_feasible_full_version():
    unit = SpeechUnit(1, 0, 0, 10, (SpeechVariant('full', 9, 0), SpeechVariant('core', 5, 3)))
    assert solve_variants([unit], pause=0)['placements'][0]['variant'] == 'full'


def test_rejects_duplicate_units():
    unit = SpeechUnit(1, 0, 0, 10, (SpeechVariant('full', 9, 0),))
    with pytest.raises(ValueError, match='Duplicate'):
        solve_variants([unit,unit], pause=0)


def test_compaction_keeps_numeric_scope_observed_in_pilot():
    from src.translation.timing_shortener import safe_shortening
    previous = 'Mỗi lớp tối đa 10 người'
    assert not safe_shortening(previous, 'Tối đa 10 người', {})
    assert safe_shortening(previous, 'Mỗi lớp tối đa 10', {})


def test_qa_timing_candidate_does_not_authorize_source_borrowing():
    from src.tts.variant_audit import compare_timing_modes
    unit = SpeechUnit(1, 1000, 1000, 1500, (SpeechVariant('full', 700, 0),))
    result = compare_timing_modes([unit], pause=40, sample_rate=1000)
    assert result['strict_40ms']['status'] == 'infeasible'
    assert result['strict_40ms']['min_end_shortage_seconds'] == pytest.approx(.12)
    assert result['qa_timing_only']['needs_source_review']
    assert result['qa_timing_only']['status'] == 'candidate_meets_timing'
    assert result['qa_timing_only']['mean_drift_seconds'] == pytest.approx(.1)


def test_mean_qa_is_an_independent_hard_gate():
    from src.tts.variant_audit import minimum_drift_witness
    variants = (SpeechVariant('long',1500,0),SpeechVariant('short',1200,1))
    unit = SpeechUnit(8,1000,1000,1500,variants)
    result = minimum_drift_witness([unit],40,1000)
    assert result['status']=='mean_drift_infeasible'
    assert result['mean_lower_bound_seconds']==pytest.approx(.35)
    easier = SpeechUnit(8,1000,1000,1500,variants+(SpeechVariant('fits',700,2),))
    result = minimum_drift_witness([easier],40,1000)
    assert result['status']=='candidate_meets_timing'
    assert result['mean_drift_seconds']==pytest.approx(.1)
