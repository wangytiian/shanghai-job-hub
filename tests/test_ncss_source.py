from pathlib import Path

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
