from pathlib import Path
from app.sources.sbs_jobs import SBS_LIST_URL, fetch_sbs_detail, fetch_sbs_listings, parse_sbs_detail, parse_sbs_listings

FIXTURES = Path(__file__).parent / "fixtures" / "sources" / "sbs"
def _fixture(name): return (FIXTURES / name).read_text(encoding="utf-8")
class _Response:
    def __init__(self, text): self.text = text
    def raise_for_status(self): return None
class _PagedClient:
    def __init__(self): self.calls = []
    def get(self, url, **kwargs): self.calls.append(("get", url, kwargs)); return _Response(_fixture("list-page-1.html"))
    def post(self, url, **kwargs): self.calls.append(("post", url, kwargs)); return _Response(_fixture("list-page-2.html"))

def test_parse_sbs_listings_preserves_job_identity_employer_title_and_publication_date():
    listing = parse_sbs_listings(_fixture("list-page-1.html"))[0]
    assert (listing.job_id, listing.employer_name, listing.title, listing.published_at) == ("1001", "上海示例咨询有限公司", "财务分析实习生", "2026-09-15")
    assert listing.detail_url == "https://jiuye.sbs.edu.cn/PositionDetail.aspx?zwid=1001"

def test_fetch_sbs_listings_uses_verified_aspnet_next_page_postback_and_keeps_hidden_fields():
    client = _PagedClient()
    assert [item.job_id for item in fetch_sbs_listings(client, pages=2)] == ["1001", "1002"]
    assert client.calls[0][0:2] == ("get", SBS_LIST_URL)
    assert client.calls[1][2]["data"] == {"__VIEWSTATE": "viewstate-page-1", "__EVENTVALIDATION": "eventvalidation-page-1", "__EVENTTARGET": "ctl00$content$GvPositionList$ctl13$btnNext"}

def test_parse_sbs_detail_emits_shanghai_candidate_with_explicit_application_evidence():
    detail = parse_sbs_detail(_fixture("detail-shanghai.html"), parse_sbs_listings(_fixture("list-page-1.html"))[0])
    assert (detail.identity_key, detail.deadline, detail.location_category, detail.location_detail) == ("1001", "2026-10-15", "明确上海", "上海市浦东新区")
    assert detail.official_url == "https://jobs.example.com/apply/1001"
    assert "官方报名入口：https://jobs.example.com/apply/1001" in detail.evidence_text
    assert "官方投递邮箱：campus@example.com" in detail.evidence_text

def test_parse_sbs_detail_marks_a_non_shanghai_job_without_promoting_its_location():
    detail = parse_sbs_detail(_fixture("detail-nonshanghai.html"), parse_sbs_listings(_fixture("list-page-2.html"))[0])
    assert (detail.location_category, detail.location_detail, detail.official_url) == ("其他地区", "浙江省杭州市", "")

def test_fetch_sbs_detail_uses_the_listing_detail_url():
    class Client:
        def __init__(self): self.calls = []
        def get(self, url, **kwargs): self.calls.append((url, kwargs)); return _Response(_fixture("detail-shanghai.html"))
    listing = parse_sbs_listings(_fixture("list-page-1.html"))[0]; client = Client()
    assert fetch_sbs_detail(client, listing).identity_key == "1001"
    assert client.calls == [(listing.detail_url, {"headers": {"User-Agent": "LixinRecruitingLocal/0.1 (public-information-research; local-only)"}, "timeout": 12.0})]
