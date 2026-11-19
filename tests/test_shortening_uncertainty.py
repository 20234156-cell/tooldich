import pytest

from src.translation.timing_shortener import safe_shortening


def test_real_cue_35_must_not_turn_supposition_into_fact():
    assert not safe_shortening(
        'Bệnh này của cậu ấy hình như phải mặc váy mới khỏi được',
        'Bệnh này phải mặc váy mới khỏi', {})


@pytest.mark.parametrize('marker', ['hình như', 'dường như', 'có vẻ', 'có lẽ'])
def test_uncertainty_cannot_disappear(marker):
    assert not safe_shortening(f'Anh ấy {marker} vẫn còn đang ở nhà',
                               'Anh ấy đang ở nhà', {})


def test_equivalent_uncertainty_marker_is_allowed():
    assert safe_shortening('Bệnh này của cậu ấy hình như phải mặc váy mới khỏi được',
                           'Có lẽ phải mặc váy mới khỏi', {})


def test_ordinary_shortening_still_allowed():
    assert safe_shortening('Còn có cả bánh mì kẹp thịt nữa', 'Có cả hamburger nữa', {})
