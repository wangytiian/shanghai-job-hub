from app.database import create_database
from app.models import Source
from app.sources.catalog import OFFICIAL_SOURCE_CATALOG, ensure_official_source_catalog


def test_official_source_catalog_creates_seventy_one_sources_with_only_public_adapters_enabled():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        ensure_official_source_catalog(session)
        sources = {source.name: source for source in session.query(Source).all()}

    assert len(OFFICIAL_SOURCE_CATALOG) == 71
    assert len(sources) == 71
    enabled = [source for source in sources.values() if source.is_enabled]
    assert {source.name for source in enabled} == {
        "上海市国资委国企招聘（真实公开来源）",
        "上海市人社局事业单位公开招聘",
        "国务院国资委人事招聘",
        "上海市税务局公务员招录",
        "上海浦东发展银行官方招聘",
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


def test_v2_catalog_has_seventy_one_unique_sources_with_only_four_auto_collectors():
    session_factory = create_database("sqlite+pysqlite:///:memory:")
    with session_factory() as session:
        ensure_official_source_catalog(session)
        sources = session.query(Source).all()

    assert len(OFFICIAL_SOURCE_CATALOG) == 71
    assert len({source.name for source in OFFICIAL_SOURCE_CATALOG}) == 71
    assert len(sources) == 71
    assert {source.library_tier for source in sources} == {"A", "B", "C", "D"}
    assert len([source for source in sources if source.library_tier == "A" and source.is_enabled]) == 5
    assert all(not source.is_enabled for source in sources if source.library_tier != "A")
    assert {source.name for source in sources if source.library_tier == "B"} >= {
        "上海银行官方招聘（待专用适配）",
        "上海农村商业银行官方招聘（待专用适配）",
        "国泰海通证券官方招聘（待专用适配）",
    }


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

    assert len(sources) == 71
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
