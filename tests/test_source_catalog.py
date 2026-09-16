from app.database import create_database
from app.models import Source, SourceDiagnostic
from app.sources.catalog import OFFICIAL_SOURCE_CATALOG, ensure_official_source_catalog
from app.services.source_library import summarize_source_library


def test_official_source_catalog_creates_eighty_sources_with_only_public_adapters_enabled():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        ensure_official_source_catalog(session)
        sources = {source.name: source for source in session.query(Source).all()}

    assert len(OFFICIAL_SOURCE_CATALOG) == 81
    assert len(sources) == 81
    enabled = [source for source in sources.values() if source.is_enabled]
    assert {source.name for source in enabled} == {
        "上海市国资委国企招聘（真实公开来源）",
        "上海市人社局事业单位公开招聘",
        "国务院国资委人事招聘",
        "上海市税务局公务员招录",
        "上海浦东发展银行官方招聘",
        "中国银行官方招聘",
        "上海商学院就业网（待专用适配）",
    }
    assert sources["中信银行官方招聘（待专用适配）"].is_enabled is False
    assert sources["中信银行官方招聘（待专用适配）"].adapter_key == "pending_validation"


def test_catalog_does_not_overwrite_a_paused_source_health_state():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        ensure_official_source_catalog(session)
        source = session.query(Source).filter_by(name="国务院国资委人事招聘").one()
        source.status = "暂停"
        source.pause_reason = "人工复查中"
        session.commit()

        ensure_official_source_catalog(session)
        source = session.query(Source).filter_by(name="国务院国资委人事招聘").one()
        assert source.status == "暂停"
        assert source.pause_reason == "人工复查中"


def test_catalog_sync_preserves_existing_operator_configuration():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        ensure_official_source_catalog(session)
        source = session.query(Source).filter_by(name="上海交通大学就业网（待专用适配）").one()
        source.url = "https://operator.example/sjtu"
        source.adapter_key = "sjtu_verified_adapter"
        source.library_tier = "A"
        source.is_enabled = True
        source.adaptation_status = "已验收"
        source.next_action = "按运营计划采集"
        session.commit()

        ensure_official_source_catalog(session)
        source = session.query(Source).filter_by(name="上海交通大学就业网（待专用适配）").one()

    assert source.url == "https://operator.example/sjtu"
    assert source.adapter_key == "sjtu_verified_adapter"
    assert source.library_tier == "A"
    assert source.is_enabled is True
    assert source.adaptation_status == "已验收"
    assert source.next_action == "按运营计划采集"


def test_catalog_rename_reassigns_existing_diagnostics_before_removing_duplicate_legacy_row():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        legacy = Source(
            name="花旗官方招聘（待专用适配）",
            url="https://jobs.citi.com/",
            level="一级",
            source_type="企业官网",
        )
        canonical = Source(
            name="花旗官方招聘（重点监控）",
            url="https://careers.example/citi",
            level="一级",
            source_type="企业官网",
        )
        session.add_all((legacy, canonical))
        session.flush()
        diagnostic = SourceDiagnostic(
            source_id=legacy.id,
            connection_status="ok",
            content_status="recruitment_list",
            adapter_status="missing",
        )
        session.add(diagnostic)
        session.commit()
        canonical_id = canonical.id

        ensure_official_source_catalog(session)
        diagnostic = session.query(SourceDiagnostic).one()

    assert diagnostic.source_id == canonical_id


def test_source_library_stats_separate_catalog_demo_and_schedulable_sources():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        ensure_official_source_catalog(session)
        session.add(
            Source(
                name="测试来源（演示）",
                url="https://example.com/demo",
                level="一级",
                source_type="演示",
            )
        )
        paused = session.query(Source).filter_by(name="国务院国资委人事招聘").one()
        paused.status = "暂停"
        session.commit()

        stats = summarize_source_library(session.query(Source).all(), OFFICIAL_SOURCE_CATALOG)

    assert stats.catalog_sources == 81
    assert stats.demo_sources == 1
    assert stats.schedulable_sources == 6


def test_v2_catalog_has_eighty_one_unique_sources_with_seven_auto_collectors():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        ensure_official_source_catalog(session)
        sources = session.query(Source).all()

    assert len(OFFICIAL_SOURCE_CATALOG) == 81
    assert len({source.name for source in OFFICIAL_SOURCE_CATALOG}) == 81
    assert len(sources) == 81
    assert {source.library_tier for source in sources} == {"A", "B", "C", "D"}
    assert len([source for source in sources if source.library_tier == "A" and source.is_enabled]) == 7
    assert all(not source.is_enabled for source in sources if source.library_tier != "A")
    source_by_name = {source.name: source for source in sources}
    assert {source.name for source in sources if source.library_tier == "B"} >= {
        "上海银行官方招聘（待专用适配）",
        "上海农村商业银行官方招聘（待专用适配）",
        "国泰海通证券官方招聘（待专用适配）",
        "华东理工大学就业网（待专用适配）",
        "上海交通大学就业网（待专用适配）",
        "上海财经大学就业网（待专用适配）",
        "华东师范大学就业网（待专用适配）",
        "上海大学就业网（待专用适配）",
        "上海理工大学就业网（待专用适配）",
        "上海对外经贸大学就业网（待专用适配）",
        "上海公共就业服务平台（待专用适配）",
    }
    assert source_by_name["上海商学院就业网（待专用适配）"].library_tier == "A"
    assert all(
        source_by_name[name].is_enabled is False
        for name in (
            "华东理工大学就业网（待专用适配）",
            "上海交通大学就业网（待专用适配）",
            "上海财经大学就业网（待专用适配）",
            "华东师范大学就业网（待专用适配）",
            "上海大学就业网（待专用适配）",
            "上海理工大学就业网（待专用适配）",
            "上海对外经贸大学就业网（待专用适配）",
            "上海公共就业服务平台（待专用适配）",
        )
    )


def test_catalog_migrates_legacy_citi_record_without_creating_a_duplicate():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        session.add(
            Source(
                name="花旗官方招聘（待专用适配）",
                url="https://jobs.citi.com/",
                level="一级",
                source_type="企业官网",
                adapter_key="pending_validation",
                scope_group="外资金融",
                is_enabled=False,
            )
        )
        session.commit()

        ensure_official_source_catalog(session)
        sources = session.query(Source).all()

    assert len(sources) == 81
    assert [source.name for source in sources].count("花旗官方招聘（重点监控）") == 1
    assert "花旗官方招聘（待专用适配）" not in {source.name for source in sources}


def test_catalog_includes_huazhi_wechat_as_non_collecting_c_tier_source():
    source = next(
        item for item in OFFICIAL_SOURCE_CATALOG if item.name == "上海华智公考（公众号招聘线索）"
    )

    assert source.library_tier == "C"
    assert source.is_enabled is False
    assert source.adapter_key == "wechat_article_lead"
    assert source.source_type == "公众号线索源"


def test_boc_catalog_uses_the_recruitment_announcement_entry_and_enables_verified_adapter():
    source = next(item for item in OFFICIAL_SOURCE_CATALOG if item.name == "中国银行官方招聘")

    assert source.url == "https://www.boc.cn/aboutboc/bi4/"
    assert source.adapter_key == "boc_announcements"
    assert source.library_tier == "A"
    assert source.is_enabled is True


def test_catalog_registers_ncss_shanghai_public_jobs_as_disabled_until_trial_acceptance():
    source = next(item for item in OFFICIAL_SOURCE_CATALOG if item.name == "国家大学生就业服务平台上海岗位")

    assert source.library_tier == "B"
    assert source.adapter_key == "ncss_shanghai_jobs"
    assert source.is_enabled is False
    assert source.scope_group == "上海学生就业"
