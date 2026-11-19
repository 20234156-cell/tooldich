"""Test that TimingShortener and MeaningVerifier respect configured batch size."""
from unittest.mock import patch
from config.settings import settings
from src.translation.timing_shortener import TimingShortener, MeaningVerifier


def test_timing_shortener_default_batch_size():
    shortener = TimingShortener({})
    assert shortener.batch_size == 20
    assert shortener.batch_size == settings.TIMING_SHORTENER_BATCH_SIZE

    verifier = MeaningVerifier({})
    assert verifier.batch_size == 20
    assert verifier.batch_size == settings.TIMING_SHORTENER_BATCH_SIZE


def test_timing_shortener_configured_batch_size():
    with patch.object(settings, 'TIMING_SHORTENER_BATCH_SIZE', 24):
        shortener = TimingShortener({})
        assert shortener.batch_size == 24

        verifier = MeaningVerifier({})
        assert verifier.batch_size == 24
