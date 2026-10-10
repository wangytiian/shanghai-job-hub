from dataclasses import replace

import httpx
import pytest
from datetime import date


def adapter():
    from app.sources import shcpa
    return shcpa


def list_html(href='/recruit/getRecruitDetailedView.do?rid=7439'):
    return f'''<form name="formrecruitquery" method="post"><table class="infotab"><tr><th>序号</th><th>事务所名称</th><th>有效日期</th><th>发布日期</th></tr><tr><td>1</td><td><a href="{href}">上海鑫审事务所</a></td><td>2026-12-31</td><td>2026-08-26</td></tr></table><input name="currentPage" type="hidden"><a onclick="javascript:doSearchByPage('2');">2</a></form>'''


def detail_html(requirements='本科财务专业', contact='请将简历投递至hr@xinshenacc.cn', deadline='2026-12-31', title='实习生'):
    rows = [('职位名称', title), ('职位薪资', '面议'), ('职位要求', requirements), ('事务所简介', '上海鑫审事务所提供审计服务'), ('联系方式', contact), ('有效日期', deadline), ('发布日期', '2026-08-26')]
    return '<table>' + ''.join(f'<tr><td>{k}：</td><td>{v}</td></tr>' for k, v in rows) + '</table><p>由各会计师事务所自行上传招聘信息，本协会/网站并不对其内容的真实性、合法性负责。</p><footer>地址：上海市肇嘉浜路789号</footer>'


def test_list_normalizes_official_443_and_rejects_offsite_and_duplicate():
    a = adapter()
    html = list_html('https://www.shcpa.org.cn:443/recruit/getRecruitDetailedView.do?rid=7439')
    html += list_html('https://evil.example/recruit/getRecruitDetailedView.do?rid=1') + list_html()
    result = a.parse_shcpa_listings(html)
    assert len(result) == 1
    assert result[0].parent_id == '7439'
    assert result[0].detail_url == 'https://www.shcpa.org.cn/recruit/getRecruitDetailedView.do?rid=7439'


def test_detail_preserves_email_deadline_disclaimer_without_invented_location_or_application():
    a = adapter()
    listing = a.parse_shcpa_listings(list_html())[0]
    detail = a.parse_shcpa_detail(detail_html(), listing)
    assert detail.identity_key == '7439'
    assert detail.location_category == '待核验'
    assert detail.location_detail == ''
    assert 'hr@xinshenacc.cn' in detail.evidence_text
    assert '报名截止日期：2026-12-31' in detail.evidence_text
    assert '真实性、合法性' in detail.evidence_text
    assert detail.official_url == ''
    assert '肇嘉浜路' not in detail.evidence_text


@pytest.mark.parametrize('requirements,expected', [('本科；工作地点：上海市浦东新区', '上海'), ('本科；工作地点：北京市朝阳区', '外地')])
def test_actual_job_location(requirements, expected):
    a = adapter()
    d = a.parse_shcpa_detail(detail_html(requirements), a.parse_shcpa_listings(list_html())[0])
    assert d.location_category == expected


@pytest.mark.parametrize('body,title', [('要求三年审计经验', '审计经理'), ('测试示例岗位，待补充正文', '岗位占位')])
def test_senior_and_placeholder_remain_verbatim_for_shared_policy(body, title):
    a = adapter()
    d = a.parse_shcpa_detail(detail_html(body, title=title), a.parse_shcpa_listings(list_html())[0])
    assert body in d.evidence_text
    assert d.title == title


def test_missing_body_rejected_and_missing_date_not_fabricated():
    a = adapter()
    listing = a.parse_shcpa_listings(list_html())[0]
    with pytest.raises(ValueError):
        a.parse_shcpa_detail('<table><tr><td>职位名称：</td><td>实习生</td></tr></table>', listing)
    detail = a.parse_shcpa_detail(detail_html(deadline=''), listing)
    assert '报名截止日期' not in detail.evidence_text


def test_expired_date_preserved_for_policy():
    a = adapter()
    d = a.parse_shcpa_detail(detail_html(deadline='2026-10-07'), a.parse_shcpa_listings(list_html())[0])
    assert '报名截止日期：2026-10-07' in d.evidence_text


def test_fetch_only_observed_pagination_and_detail_no_redirect():
    a = adapter()
    requests = []
    def handler(req):
        requests.append(req)
        return httpx.Response(200, text=detail_html() if 'Detailed' in req.url.path else list_html())
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        listings = a.fetch_shcpa_listings(client, pages=3)
        a.fetch_shcpa_detail(client, listings[0])
        with pytest.raises(ValueError):
            a.fetch_shcpa_detail(client, replace(listings[0], detail_url='https://evil.example/?rid=7439'))
    assert len(requests) == 3
    assert requests[1].method == 'POST'
    assert b'currentPage=2' in requests[1].content


def test_list_hard_cap_30():
    a = adapter()
    html = ''.join(list_html(f'/recruit/getRecruitDetailedView.do?rid={i}') for i in range(40))
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html))) as client:
        assert len(a.fetch_shcpa_listings(client, pages=3)) == 30


def test_shared_review_intake_complete_candidate_and_audience_only_b():
    from app.services.source_candidate_policy import candidate_from_detail
    from app.services.review_intake import evaluate_review_intake
    a = adapter()
    listing = a.parse_shcpa_listings(list_html())[0]
    for title, expected in [('实习生', 'qualified'), ('审计助理', 'TARGET_AUDIENCE_UNCLEAR')]:
        d = a.parse_shcpa_detail(detail_html('本科财务专业；工作地点：上海市浦东新区', title=title), listing)
        c = candidate_from_detail(d, source_listing_url=a.SHCPA_LIST_URL)
        assert c.application_value == 'hr@xinshenacc.cn'
        assert c.deadline == '2026-12-31'
        decision = evaluate_review_intake(c, now=date(2026, 10, 10))
        assert expected == decision.verdict or expected in decision.reason_codes
        assert 'BODY_NOT_JOB_SPECIFIC' not in decision.reason_codes
        assert 'APPLICATION_MISSING' not in decision.reason_codes


def test_intro_email_is_not_application():
    from app.services.source_candidate_policy import candidate_from_detail
    a = adapter()
    html = detail_html(contact='电话12345').replace('提供审计服务', '提供审计服务 info@example.com')
    c = candidate_from_detail(a.parse_shcpa_detail(html, a.parse_shcpa_listings(list_html())[0]))
    assert c.application_value == ''


@pytest.mark.parametrize('pages', [0, 4])
def test_invalid_page_budget_rejected_before_request(pages):
    a = adapter()
    with httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail('must not request'))) as client:
        with pytest.raises(ValueError):
            a.fetch_shcpa_listings(client, pages=pages)
