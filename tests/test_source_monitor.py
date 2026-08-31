from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Source
from app.services.real_collection import REAL_SOURCE_NAME


def test_sources_page_shows_collection_strategy_and_health_fields():
    client = TestClient(create_app("sqlite+pysqlite:///:memory:"))

    response = client.get("/sources")

    assert response.status_code == 200
    assert "当前招聘季节" in response.text
    assert "建议频率" in response.text
    assert "下次建议采集" in response.text
    assert "连续失败" in response.text
    assert "等待首次采集" in response.text


def test_paused_source_can_be_resumed_from_source_monitor():
    app = create_app("sqlite+pysqlite:///:memory:")
    with app.state.session_factory() as session:
        source = session.query(Source).filter_by(name="国务院国资委人事招聘").one()
        source.status = "暂停"
        source.pause_reason = "连续失败"
        session.commit()
        source_id = source.id

    client = TestClient(app)
    page = client.get("/sources")
    response = client.post(f"/sources/{source_id}/resume", follow_redirects=False)

    assert "恢复来源" in page.text
    assert response.status_code == 303
    with app.state.session_factory() as session:
        source = session.get(Source, source_id)
        assert source.status == "正常"
        assert source.pause_reason == ""
        assert source.consecutive_failure_count == 0


def test_real_collection_route_rejects_automatically_paused_source():
    app = create_app("sqlite+pysqlite:///:memory:")
    with app.state.session_factory() as session:
        source = session.query(Source).filter_by(name=REAL_SOURCE_NAME).one()
        source.status = "暂停"
        source.pause_reason = "连续失败"
        session.commit()

    response = TestClient(app).post("/tasks/shanghai-sasac-collection")

    assert response.status_code == 409
    assert "暂停" in response.text


def test_sources_page_shows_v2_library_tiers_and_safe_boundaries():
    response = TestClient(create_app("sqlite+pysqlite:///:memory:")).get("/sources")

    assert response.status_code == 200
    for label in (
        "71 家分层来源库",
        "已验证自动抓取",
        "核心专用适配库",
        "重点监控库",
        "观察库",
        "学生价值分",
        "不会自动抓取",
    ):
        assert label in response.text
    assert "上海银行官方招聘（待专用适配）" in response.text
    assert "西门子中国官方招聘（观察库）" in response.text


def test_sources_page_uses_actual_a_tier_count_not_a_hardcoded_number():
    response = TestClient(create_app("sqlite+pysqlite:///:memory:")).get("/sources")

    assert "目前只有 A 类 5 家已验证官方来源参与每日采集" in response.text
    assert "目前只有 A 类 4 家已验证官方来源参与每日采集" not in response.text


def test_source_health_check_route_returns_feedback_on_sources_page(monkeypatch):
    from app.services.source_health import SourceHealthResult

    app = create_app("sqlite+pysqlite:///:memory:")
    with app.state.session_factory() as session:
        source = session.query(Source).filter_by(name="上海银行官方招聘（待专用适配）").one()
        source_id = source.id

    def fake_check(source, client, checked_at):
        source.last_monitor_summary = "官网连接正常，仍待专用适配，不参与每日采集"
        source.last_error_summary = ""
        source.last_checked_at = checked_at
        return SourceHealthResult("success", source.last_monitor_summary)

    monkeypatch.setattr("app.services.source_health.check_source_connection", fake_check)
    response = TestClient(app).post(f"/sources/{source_id}/health-check")

    assert response.status_code == 200
    assert "官网连接正常，仍待专用适配" in response.text
    assert "验证官网连接" in response.text
