import pytest

from src.tts.variant_blocks import choose_pilot, independent_blocks
from src.tts.variant_config import VariantTimingConfig
from src.tts.variant_solver import SpeechUnit, SpeechVariant


def unit(i,start,end,samples):
    return SpeechUnit(i,start,start,end,(SpeechVariant('full',samples,0),))


def test_reset_needs_confirmed_silence_and_pause_after_latest_end():
    units = [unit(20,0,120,80),unit(30,125,200,80),unit(50,400,500,80)]
    assert independent_blocks(units,10,[]) == [units]
    assert independent_blocks(units,10,[(120,400)]) == [units[:2],units[2:]]


def test_pilot_selects_by_shortage_without_crossing_block_boundary():
    blocks = [[unit(101,0,50,60),unit(102,100,150,80)],
              [unit(203,300,350,90),unit(204,400,450,95),unit(205,500,550,55)]]
    pilot,score = choose_pilot(blocks,2)
    assert [u.cue_id for u in pilot] == [203,204]
    assert score == 85


@pytest.mark.parametrize('values',[{'max_speed':1.31},{'mean_drift_seconds':.31},
                                    {'max_drift_seconds':1.51},{'strict_seconds':.2,'max_drift_seconds':.1},
                                    {'pause_seconds':float('nan')}])
def test_invalid_or_relaxed_settings_fail_validation(values):
    with pytest.raises(ValueError):
        VariantTimingConfig(**values)
