from datetime import date, datetime
from importlib.util import find_spec

SPDB_LIST_PAYLOAD = {
    "rows": [
        {
            "openningJobId": "10023076",
            "positionName": "客服代表岗（上海-应届生）",
            "deptDescr": "数字平台部",
            "prmLocArea": "上海",
            "desiredStartDt": "2026-08-29",
            "closeDt": "2026-09-19",
        },
        {
            "openningJobId": "10023077",
            "positionName": "过期岗位",
            "deptDescr": "数字平台部",
            "prmLocArea": "上海",
            "desiredStartDt": "2026-08-01",
            "closeDt": "2026-08-30",
        },
        {
            "openningJobId": "10023078",
            "positionName": "非上海岗位",
            "deptDescr": "北京分行",
            "prmLocArea": "北京",
            "desiredStartDt": "2026-08-29",
            "closeDt": "2026-09-19",
        },
        {
            "openningJobId": "10023079",
            "positionName": "区域支行营销负责人（公司）",
            "deptDescr": "上海分行",
            "prmLocArea": "上海",
            "desiredStartDt": "2026-08-29",
            "closeDt": "2026-09-19",
        },
    ]
}


class FakeSpdbClient:
    def post(self, url, **kwargs):
        assert url.endswith("socialJobJsonList")
        assert kwargs["data"]["address"] == "上海"
        return FakeResponse(SPDB_LIST_PAYLOAD)

    def get(self, url, **kwargs):
        assert "jobDetailJSON?jobId=10023076&type=1" in url
        return FakeResponse(
            {
                "cgOpenningJob": {
                    "openningJobId": "10023076",
                    "positionName": "客服代表岗（上海-应届生）",
                    "deptDescr": "数字平台部",
                    "address": "上海",
                    "desiredStartDt": "2026-08-29",
                    "closeDt": "2026-09-19",
                    "hrsJobDuty": "为客户提供远程服务。",
                    "hrsJobRequire": "本科及以上，金融相关背景优先。",
                }
            }
        )


class FakeSpdbClientWithMatureExperience(FakeSpdbClient):
    def get(self, url, **kwargs):
        assert "jobDetailJSON?jobId=10023076&type=1" in url
        return FakeResponse(
            {
                "cgOpenningJob": {
                    "openningJobId": "10023076",
                    "positionName": "客服代表岗（上海）",
                    "deptDescr": "数字平台部",
                    "address": "上海",
                    "desiredStartDt": "2026-08-29",
                    "closeDt": "2026-09-19",
                    "hrsJobDuty": "为客户提供远程服务。",
                    "hrsJobRequire": "要求三年以上银行从业经验。",
                }
            }
        )


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_spdb_adapter_only_keeps_open_shanghai_roles_with_direct_official_urls():
    assert find_spec("app.sources.spdb") is not None
    from app.sources.spdb import parse_spdb_shanghai_listings

    listings = parse_spdb_shanghai_listings(SPDB_LIST_PAYLOAD, today=date(2026, 8, 31))

    assert [item.title for item in listings] == ["客服代表岗（上海-应届生）"]
    assert listings[0].deadline == "2026-09-19"
    assert listings[0].detail_url.endswith("jobDetail?jobId=10023076&type=1")


def test_spdb_student_fit_keeps_explicit_graduate_and_uncertain_roles():
    from app.sources.spdb import is_spdb_student_fit

    assert is_spdb_student_fit("三年以上银行从业经验")[0] is False
    assert is_spdb_student_fit("三年以上经验，优秀应届生可投")[0] is True
    assert is_spdb_student_fit("1-3年相关经验")[0] is True


def test_spdb_adapter_fetches_job_evidence_from_the_official_detail_endpoint():
    from app.sources.spdb import fetch_spdb_shanghai_job_details

    details = fetch_spdb_shanghai_job_details(FakeSpdbClient(), limit=10, today=date(2026, 8, 31))

    assert len(details.details) == 1
    assert details.details[0].official_url.endswith("jobDetail?jobId=10023076&type=1")
    assert "数字平台部" in details.details[0].evidence_text
    assert "本科及以上" in details.details[0].evidence_text


def test_spdb_fetch_reports_student_fit_filtered_count():
    from app.sources.spdb import fetch_spdb_shanghai_job_details

    result = fetch_spdb_shanghai_job_details(
        FakeSpdbClientWithMatureExperience(), limit=10, today=date(2026, 8, 31)
    )

    assert result.details == []
    assert result.filtered_count == 1
    assert result.filter_reasons == ["明确三年及以上经验且未见应届生开放信号"]


def test_spdb_collection_creates_pending_jobs_with_official_application_pages(session):
    from app.models import Job
    from app.services.real_collection import collect_spdb_shanghai_jobs

    result = collect_spdb_shanghai_jobs(
        session, FakeSpdbClient(), now=datetime(2026, 8, 31, 10, 0, 0)
    )
    job = session.query(Job).filter_by(is_demo=False).one()

    assert result.created_jobs == 1
    assert job.status == "待核验"
    assert job.employer_name == "待人工核验（上海浦东发展银行）"
    assert job.official_url.endswith("jobDetail?jobId=10023076&type=1")
    assert job.deadline == "2026-09-19"


def test_spdb_collection_records_student_fit_filter_count_in_source_monitor(session):
    from app.models import Source
    from app.services.real_collection import SPDB_SOURCE_NAME, collect_spdb_shanghai_jobs

    result = collect_spdb_shanghai_jobs(
        session, FakeSpdbClientWithMatureExperience(), now=datetime(2026, 8, 31, 10, 0, 0)
    )
    source = session.query(Source).filter_by(name=SPDB_SOURCE_NAME).one()

    assert result.created_jobs == 0
    assert "学生适配预筛过滤 1 条" in source.last_monitor_summary
