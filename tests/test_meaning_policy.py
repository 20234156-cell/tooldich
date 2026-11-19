import pytest

from src.translation.meaning_policy import get_meaning_policy
from src.translation.timing_shortener import safe_shortening


@pytest.mark.parametrize('source,previous,candidate', [
    ('他们要参加航模展','Họ muốn tham gia triển lãm mô hình hàng không','Họ muốn dự triển lãm mô hình bay'),
    ('这次有什么要求','Lần này có yêu cầu gì không?','Lần này yêu cầu gì?'),
    ('你去不去','Anh có đi hay không?','Anh đi chứ?'),
    ('不过啊那几个追你的富家崽知道！','Nhưng mà mấy tên thiếu gia theo đuổi cháu mà biết chuyện này!','Mấy thiếu gia theo đuổi cháu mà biết chuyện!'),
    ('听说前段时间','Nghe nói cách đây không lâu,','Nghe nói gần đây,'),
    ('只要不断完成系统任务','Chỉ cần không ngừng hoàn thành nhiệm vụ của hệ thống,','Chỉ cần hoàn thành nhiệm vụ,'),
    ('不料他突然出现','Không ngờ hắn đột nhiên xuất hiện','Hắn bất ngờ xuất hiện'),
])
def test_nonnegative_contexts_can_reach_semantic_review(source,previous,candidate):
    policy = get_meaning_policy('zh','vi')
    assert safe_shortening(previous,candidate,{},source=source,meaning_policy=policy)


@pytest.mark.parametrize('source,previous,candidate', [
    ('不要参加展览','Đừng tham gia triển lãm hàng không','Hãy dự triển lãm'),
    ('我没去学校','Tôi đã đến trường rồi','Tôi đến trường'),
    ('每班最多10个人','Tối đa là 10 người thôi','Tối đa 10 người'),
])
def test_source_obligation_blocks_loss_even_if_previous_translation_missed_it(source,previous,candidate):
    policy = get_meaning_policy('zh','vi')
    assert not safe_shortening(previous,candidate,{},source=source,meaning_policy=policy)


def test_unknown_pair_is_rejected_before_a_run():
    with pytest.raises(ValueError,match='Unsupported'):
        get_meaning_policy('en','vi')


@pytest.mark.parametrize('source,previous,candidate,issue', [
    ('可控核聚变设施','Cơ sở tổng hợp hạt nhân có kiểm soát','Cơ sở hạt nhân','term:核聚变'),
    ('参加航模展','Dự triển lãm mô hình hàng không','Dự triển lãm mô hình','term:航模'),
    ('煎饼果子','Món bánh kếp','Món bánh','term:煎饼果子'),
    ('您回去上课吧','Cô về dạy học đi','Thầy về dạy đi','address'),
    ('你们班报不报名','Thầy ơi, lớp thầy đăng ký không?','Thầy đăng ký không?','possessor:你们班'),
    ('发现了他们的装置','Đã phát hiện thiết bị của họ','Thấy thiết bị','possessor:他们的'),
    ('参加的班级','Các lớp tham gia','Mỗi lớp','added_distributive_scope'),
    ('大家回去在班里好好安排一下','Mọi người về lớp sắp xếp cho tốt nhé','Các em về lớp sắp xếp nhé','added_address_to_neutral_group'),
    ('能争取到5个班的名额','Có thể xin được suất cho 5 lớp','Đã cố xin 5 suất lớp.','modality'),
    ('就只能是一张狼人牌','thì chỉ có thể là một lá bài ma sói thôi.','Chỉ là sói.','term:狼人牌'),
    ('你不吃就亏了','Nếu bạn không ăn, thì bạn sẽ bị thiệt thòi.','Phí lắm','conditional_scope'),
])
def test_named_rejection_reasons(source,previous,candidate,issue):
    from src.translation.timing_shortener import shortening_issues
    assert issue in shortening_issues(previous,candidate,{},source=source,
                                     meaning_policy=get_meaning_policy('zh','vi'))


def test_equivalent_fusion_term_survives_guard():
    policy = get_meaning_policy('zh','vi')
    assert safe_shortening('Cơ sở tổng hợp hạt nhân có kiểm soát','Cơ sở nhiệt hạch',{},
                           source='可控核聚变设施',meaning_policy=policy)
