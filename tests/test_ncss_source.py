from pathlib import Path
import json

import httpx
import pytest

from app.sources.ncss import (
    NCSS_LIST_URL,
    NcssListing,
    fetch_ncss_shanghai_listings,
    parse_ncss_detail,
    parse_ncss_list,
)


FIXTURES = Path(__file__).parent / "fixtures" / "sources" / "ncss"


def _listing():
    return NcssListing("real-id", "列表标题", "列表公司", "NCSS", "2026-10-09",
                       "https://www.ncss.cn/student/jobs/real-id/detail.html")


def test_maincard_facts_and_native_apply_are_preserved_without_hidden_modal():
    html = '''<nav>上海 在校生</nav><div class="job"><li id="jobName">财务储备岗</li>
    <ul class="salary"><li>本科及以上</li></ul><ul class="address"><div class="site-tag">上海市</div></ul>
    <div class="apply"><a href="#">投递简历</a></div></div><p id="corpName">真实公司</p>
    <div class="jobdetail-box">岗位职责：负责财务数据整理、凭证核对和报表制作，面向2027届毕业生。</div>
    <div class="modal"><p>博士起 中国人民银行 北京 投递简历</p></div>'''
    detail = parse_ncss_detail(html, _listing())
    assert all(text in detail.evidence_text for text in ("财务储备岗", "真实公司", "本科及以上", "上海市", "投递简历"))
    assert "中国人民银行" not in detail.evidence_text
    assert "在校生" not in detail.evidence_text
    assert detail.native_application_verified is True
    assert detail.native_application_href == "#"


def test_navigation_shanghai_does_not_prove_job_location():
    html = '''<nav>上海</nav><div class="jobdetail-box"><h1>财务岗位</h1>
    岗位职责：负责财务数据整理、凭证核对和报表制作。<button>投递简历</button></div>'''
    assert parse_ncss_detail(html, _listing()).location_category == "原文未明确"


def test_actual_same_parent_position_heading_is_preserved():
    html = '''<div class="job"><div class="apply"><a href="#">投递简历</a></div></div>
    <div class="details"><p class="title"><span>职位详情</span></p>
    <div class="jobdetail-box"><div class="mainContent">1.负责费用报销审核、凭证录入及往来账目核对。</div></div></div>'''
    assert "职位详情\n1.负责费用报销审核" in parse_ncss_detail(html, _listing()).evidence_text


def test_unrelated_position_heading_is_not_attached_to_job_body():
    html = '''<p class="title">职位详情</p><div class="job"><div class="apply"><a href="#">投递简历</a></div></div>
    <div class="details"><p class="title">公司简介</p>
    <div class="jobdetail-box"><div class="mainContent">1.负责费用报销审核、凭证录入及往来账目核对。</div></div></div>'''
    assert "职位详情" not in parse_ncss_detail(html, _listing()).evidence_text


@pytest.mark.parametrize("hidden", ['class="modal"', 'style="display:none"', 'hidden', 'aria-hidden="true"', 'class="d-none"'])
def test_hidden_apply_does_not_prove_active_job(hidden):
    html = f'''<div class="jobdetail-box">岗位职责：负责财务数据整理、凭证核对和报表制作。</div>
    <div {hidden}><button>投递简历</button></div>'''
    with pytest.raises(ValueError, match="未确认"):
        parse_ncss_detail(html, _listing())


def test_native_apply_hidden_by_d_none_ancestor_is_not_verified():
    html = '''<div class="job"><div class="d-none"><div class="apply"><a href="#">投递简历</a></div>
    <div class="address"><div class="site-tag">上海市</div></div></div></div>
    <div class="jobdetail-box">岗位职责：负责财务数据整理、凭证核对和报表制作。</div>'''
    with pytest.raises(ValueError, match="未确认"):
        parse_ncss_detail(html, _listing())


def test_fetch_explicitly_disables_inherited_redirect_following():
    client = _Client('{"data":{"list":[]}}')
    fetch_ncss_shanghai_listings(client, pages=1)
    assert client.calls[0][1].get("follow_redirects") is False


def test_public_redirect_is_rejected_without_requesting_offsite_target():
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://untrusted.invalid/job"})
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_ncss_shanghai_listings(client, pages=1)
    assert len(calls) == 1


def test_optional_keywords_share_total_three_request_budget():
    client = _Client((FIXTURES / "shanghai-list.json").read_text(encoding="utf-8"))
    fetch_ncss_shanghai_listings(client, pages=3, keywords=["财务", "会计", "审计", "税务"])
    assert len(client.calls) == 3
    assert [call[1]["params"]["jobName"] for call in client.calls] == ["财务", "会计", "审计"]


def test_server_oversized_page_cannot_exceed_thirty_listing_budget():
    rows = [{"jobId": str(i), "jobName": "财务", "recName": "公司", "areaCodeName": "上海"}
            for i in range(40)]
    assert len(fetch_ncss_shanghai_listings(_Client(json.dumps({"data": {"list": rows}})), pages=3)) <= 30


def test_parse_ncss_list_keeps_only_shanghai_records_and_preserves_identity():
    listings = parse_ncss_list((FIXTURES / "shanghai-list.json").read_text(encoding="utf-8"))

    assert [listing.job_id for listing in listings] == ["job-a", "job-b"]
    assert listings[0].title == "AI 产品运营实习生"
    assert listings[0].employer_name == "上海示例科技有限公司"
    assert listings[0].detail_url.endswith("/student/jobs/job-a/detail.html")


def test_parse_ncss_detail_accepts_only_public_active_job():
    listing = NcssListing(
        "job-a",
        "AI 产品运营实习生",
        "上海示例科技有限公司",
        "上海市学生事务中心",
        "2026-09-06",
        "https://www.ncss.cn/student/jobs/job-a/detail.html",
    )

    detail = parse_ncss_detail((FIXTURES / "active-detail.html").read_text(encoding="utf-8"), listing)

    assert detail.identity_key == "job-a"
    assert detail.location_category == "上海"
    assert "岗位职责" in detail.evidence_text


def test_parse_ncss_detail_rejects_offline_job():
    listing = NcssListing(
        "job-offline",
        "过期岗位",
        "上海示例科技有限公司",
        "上海市学生事务中心",
        "2026-09-06",
        "https://www.ncss.cn/student/jobs/job-offline/detail.html",
    )

    with pytest.raises(ValueError, match="已下线"):
        parse_ncss_detail((FIXTURES / "offline-detail.html").read_text(encoding="utf-8"), listing)


class _Response:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        return None


class _Client:
    def __init__(self, payload: str):
        self.payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.payload)


def test_fetch_ncss_shanghai_listings_limits_pages_and_uses_public_shanghai_filter():
    client = _Client((FIXTURES / "shanghai-list.json").read_text(encoding="utf-8"))

    listings = fetch_ncss_shanghai_listings(client, pages=2)

    assert [listing.job_id for listing in listings] == ["job-a", "job-b"]
    assert [call[1]["params"]["offset"] for call in client.calls] == ["1", "2"]
    assert all(call[0] == NCSS_LIST_URL for call in client.calls)
    assert all(call[1]["params"]["areaCode"] == "310000" for call in client.calls)
    with pytest.raises(ValueError, match="1到3"):
        fetch_ncss_shanghai_listings(client, pages=4)


def test_ncss_request_retries_one_real_httpx_timeout():
    from app.services.source_request_budget import RequestBudgetController

    class TimeoutThenSuccess(_Client):
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if len(self.calls) == 1:
                raise httpx.ReadTimeout("socket stalled", request=httpx.Request("GET", url))
            return _Response(self.payload)

    client = TimeoutThenSuccess('{"data":{"list":[]}}')
    controller = RequestBudgetController(
        total_seconds=10,
        request_timeout_seconds=3,
        interval_seconds=0,
        sleeper=lambda _seconds: None,
    )

    assert fetch_ncss_shanghai_listings(client, pages=1, controller=controller) == []
    assert len(client.calls) == 2
