from app.models import Source
from app.services.source_library import can_auto_collect, monitoring_message


def test_only_enabled_a_tier_source_can_auto_collect():
    source = Source(
        name="B层测试",
        url="https://example.com",
        level="一级",
        source_type="企业官网",
        library_tier="B",
        is_enabled=True,
    )

    assert can_auto_collect(source) is False


def test_monitoring_message_never_promises_job_collection_for_c_or_d_tier():
    source = Source(
        name="观察测试",
        url="https://example.com",
        level="一级",
        source_type="企业官网",
        library_tier="D",
        adaptation_status="观察中",
    )

    assert "不抓取岗位" in monitoring_message(source)
