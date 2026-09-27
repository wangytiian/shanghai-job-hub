"""Offline, shortened and redacted samples matching the verified public HTML."""

from dataclasses import replace
import importlib
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from app.sources.campus_json import CampusAnnouncement
from app.services.source_request_budget import RequestBudgetController


def adapter():
    assert importlib.util.find_spec("app.sources.sspu_news"), "SSPU news adapter is not implemented"
    return importlib.import_module("app.sources.sspu_news")


def row(news_id="101", **updates):
    result = dict(newsId=news_id, newsTitle="示例（上海）物流科技有限公司招聘公告",
                  releaseDate="2026-09-10", releaseMode="以内容形式发布", newsUrl=None,
                  viewType="10", viewTypeName="公开查看", fbzt="已发布", orgName="上海第二工业大学")
    result.update(updates)
    return result


def payload(html, **updates):
    return {"code": 200, "data": row(newsContent=html, **updates)}


ANNOUNCEMENT = CampusAnnouncement("101", row()["newsTitle"], "2026-09-10",
                                  "https://career.sspu.edu.cn/career/news/view/zpgg/101")
NUMBERED_PREFIX = """<p>官网上海示例家居有限公司 - 校园招聘</p>
<p>投递链接：<a href="file:///C:/sample.docx#/">https://app.mokahr.com/campus-recruitment/example/142521?locale=zh-CN#/</a></p>
<p>联系邮箱：campus@example.com</p><p>联系地址：上海市松江区</p>"""


def numbered(number, title, location="上海", requirement="本科及以上学历，专业不限。", salary="7k~11k"):
    return f"<p><strong>{number}、{title}（1名）</strong></p><p> </p><p>{salary}/{location}/本科及以上</p><p>岗位职责</p><p>1、维护业务数据，执行调研任务。</p><p>任职要求</p><p>{requirement}</p>"


def labelled(title, requirement):
    return f"<p>职位名称：{title}</p><p>岗位职责：</p><p>1. 维护业务订单和客户资料。</p><p>任职要求：</p><p>{requirement}</p>"


COMMON_TAIL = """<p>实习待遇</p><p>实习补贴：4000元/月</p><p>学历要求：大专以上</p>
<p>转正机会：可提供转正</p><p>工作地点：上海市杨浦区示例路88号</p>
<p>招聘方式及联系方式</p><p>简历投递邮箱：hr@example.com</p>"""


def test_numbered_jobs_isolate_audience_location_and_keep_direction():
    body = NUMBERED_PREFIX + "".join([
        numbered(31, "产品运营管培生"),
        numbered(39, "数据分析管培生", "胶州铺集"),
        numbered(44, "外贸管培生"),
        numbered(56, "战略管培生（业务开发方向）", requirement="本科及以上，2027届应届生。"),
        numbered(57, "战略管培生（海外门店方向）", requirement="本科及以上，2027届应届生。"),
        numbered(80, "市场运营管培生", requirement="本科及以上，经济学或金融管理专业。"),
    ]) + "<p>福利待遇：</p><p>五险一金</p>"
    jobs = adapter().parse_sspu_details(payload(body), ANNOUNCEMENT)
    assert len(jobs) == 6
    assert [j.identity_key for j in jobs] == [f"101:{i}" for i in [31, 39, 44, 56, 57, 80]]
    assert [j.location_category for j in jobs] == ["明确上海", "其他地区", "明确上海", "明确上海", "明确上海", "明确上海"]
    assert jobs[3].title == "战略管培生（业务开发方向）"
    assert all(("2027届" in j.evidence_text) == (i in [3, 4]) for i, j in enumerate(jobs))
    assert all("五险一金" in j.evidence_text for j in jobs)
    assert all(j.employer_name == "上海示例家居有限公司" for j in jobs)
    assert all(j.announcement_title == ANNOUNCEMENT.title for j in jobs)
    assert "金融管理" not in jobs[0].evidence_text
    assert "联系地址" not in jobs[0].evidence_text
    assert "file:" not in jobs[0].evidence_text
    assert jobs[0].official_url.startswith("https://app.mokahr.com/campus-recruitment/")
    assert "官方投递邮箱：campus@example.com" in jobs[0].evidence_text


def test_labelled_jobs_share_explicit_tail_but_not_other_job_requirements():
    html = labelled("海运货代销售", "大专以上，2027届应届生。") + labelled("海运货代操作", "现代物流管理专业，大专以上，2027届应届生。") + labelled("海运货代报关", "国际经济与贸易专业，大专以上，2027届应届生。") + COMMON_TAIL
    jobs = adapter().parse_sspu_details(payload(html), ANNOUNCEMENT)
    assert len(jobs) == 3
    assert all(j.location_category == "明确上海" and "杨浦区" in j.location_detail for j in jobs)
    assert all("4000元/月" in j.evidence_text and "官方投递邮箱：hr@example.com" in j.evidence_text for j in jobs)
    assert "国际经济与贸易" not in jobs[1].evidence_text
    assert "现代物流管理" not in jobs[2].evidence_text
    assert jobs[0].employer_name == "示例（上海）物流科技有限公司"


@pytest.mark.parametrize("place, category", [("上海/山东", "明确上海"), ("上海、山东都有", "明确上海"), ("43号楼直播基地", "原文未明确"), ("信创B座", "原文未明确"), ("胶州铺集", "其他地区")])
def test_slash_locations_are_complete_and_building_names_not_inferred(place, category):
    job = adapter().parse_sspu_details(payload(NUMBERED_PREFIX + numbered(8, "新型业务角色", place)), ANNOUNCEMENT)[0]
    assert job.location_detail == place
    assert job.location_category == category


def test_missing_location_and_title_audience_do_not_gain_shanghai_or_2027():
    announcement = replace(ANNOUNCEMENT, title="示例企业2027届应届生招聘公告")
    job = adapter().parse_sspu_details(payload("<p>联系地址：上海市松江区</p>" + labelled("数据助理", "本科及以上。")), announcement)[0]
    assert job.location_category == "原文未明确" and job.location_detail == ""
    assert "2027" not in job.evidence_text
    assert "截止" not in job.evidence_text


def test_identity_is_stable_when_reordered_and_salary_changes():
    parse = adapter().parse_sspu_details
    first = parse(payload(numbered(6, "新业务角色") + numbered(8, "渠道角色")), ANNOUNCEMENT)
    second = parse(payload(numbered(8, "渠道角色", salary="9k~13k") + numbered(6, "新业务角色")), ANNOUNCEMENT)
    assert {j.title: j.identity_key for j in first} == {j.title: j.identity_key for j in second}
    first = parse(payload(labelled("销售", "本科。") + labelled("操作", "大专。")), ANNOUNCEMENT)
    second = parse(payload(labelled("操作", "大专。") + labelled("销售", "本科。")), ANNOUNCEMENT)
    assert {j.title: j.identity_key for j in first} == {j.title: j.identity_key for j in second}


@pytest.mark.parametrize("url", ["file:///C:/resume.docx", "javascript:alert(1)", "http://localhost/apply", "http://127.0.0.1/apply", "http://10.0.0.8/apply", "http://192.168.1.2/apply", "http://[::1]/apply", "http://corp.local/apply", "http://2130706433/apply", "https://user:password@example.com/apply"])
def test_unsafe_application_urls_are_never_returned(url):
    html = f'<p>简历投递：<a href="{url}">点击申请</a></p>' + labelled("数据助理", "本科。")
    job = adapter().parse_sspu_details(payload(html), ANNOUNCEMENT)[0]
    assert job.official_url == ""
    assert url not in job.evidence_text


def test_only_application_links_are_kept_and_active_html_is_removed():
    html = '<p><a href="https://example.com">公司主页</a></p><script>2027届应届生 上海 投递 evil@example.com</script><style>bad</style><p>简历投递：<a href="https://jobs.example.com/apply">申请入口</a></p>' + labelled("资料助理", "本科。")
    job = adapter().parse_sspu_details(payload(html), ANNOUNCEMENT)[0]
    assert job.official_url == "https://jobs.example.com/apply"
    assert "evil@" not in job.evidence_text and "2027" not in job.evidence_text and "<script" not in job.evidence_text


@pytest.mark.parametrize("update", [{"viewType": "20", "viewTypeName": "校内查看"}, {"fbzt": "未发布"}, {"viewType": None}, {"releaseMode": "未知"}])
def test_nonpublic_or_unknown_modes_raise(update):
    with pytest.raises(ValueError):
        adapter().parse_sspu_details(payload(labelled("数据助理", "本科。"), **update), ANNOUNCEMENT)


@pytest.mark.parametrize("html", ["", "<script>fake vacancy</script>", "<p>公司介绍</p><p>欢迎加入</p>", "<p>职位名称：空岗位</p>"])
def test_no_empty_or_unsegmented_fallback(html):
    with pytest.raises(ValueError):
        adapter().parse_sspu_details(payload(html), ANNOUNCEMENT)


def test_external_link_list_is_retained_but_detail_is_explicitly_unsupported():
    source = adapter()
    external = row(releaseMode="以链接形式发布", newsUrl="https://example.com/apply")
    announcements = source.parse_sspu_announcements({"code": 200, "data": {"list": [external], "total": "1"}})
    assert len(announcements) == 1
    assert announcements[0].detail_url == ANNOUNCEMENT.detail_url
    with pytest.raises(source.UnsupportedSspuAnnouncement):
        source.parse_sspu_details({"code": 200, "data": external}, announcements[0])


def test_list_keeps_parent_fields_and_deduplicates():
    result = adapter().parse_sspu_announcements({"code": 200, "data": {"list": [row(), row()], "total": "2"}})
    assert result == [ANNOUNCEMENT]


@pytest.mark.parametrize("bad", [{"code": 403, "data": {}}, {"code": 200, "data": []}, {"code": 200, "data": {"list": [None]}}, {"code": 200, "data": {"list": [row(viewType="20")]}}])
def test_invalid_lists_are_explicit_errors(bad):
    with pytest.raises(ValueError):
        adapter().parse_sspu_announcements(bad)


def controller():
    return RequestBudgetController(total_seconds=180, request_timeout_seconds=12, interval_seconds=0)


def test_fetch_stops_at_total_and_posts_empty_form():
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"code": 200, "data": {"list": [row()], "total": "1", "pages": 1, "isLastPage": True}})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = adapter().fetch_sspu_announcements(client, controller=controller())
    assert result == [ANNOUNCEMENT] and len(requests) == 1
    assert requests[0].method == "POST" and requests[0].content == b""
    assert requests[0].url.path == "/career/news/search/zpgg"


def test_fetch_stops_when_page_has_no_new_ids():
    paths = []
    def handle(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"code": 200, "data": {"list": [row()], "total": "30"}})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert len(adapter().fetch_sspu_announcements(client, controller=controller())) == 1
    assert paths == ["/career/news/search/zpgg", "/career/news/search/zpgg/2/10"]


def test_fetch_is_bounded_and_propagates_failed_request():
    source = adapter()
    with pytest.raises(ValueError):
        source.fetch_sspu_announcements(object(), pages=4)
    calls = []
    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("offline timeout", request=request)
    budget = RequestBudgetController(total_seconds=180, request_timeout_seconds=12, interval_seconds=0, sleeper=lambda _: None)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.ReadTimeout):
            source.fetch_sspu_details(client, ANNOUNCEMENT, controller=budget)
    assert len(calls) == 2
    assert calls[0].url.path == "/career/news/data/zpgg/101"
    assert calls[0].extensions["timeout"]["read"] == 12


def test_default_controller_enforces_budget_and_interval(monkeypatch):
    source = adapter()
    original = source.RequestBudgetController
    options = []
    def capture(**kwargs):
        options.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(source, "RequestBudgetController", capture)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"code": 200, "data": {"list": [], "total": "0"}}))) as client:
        assert source.fetch_sspu_announcements(client) == []
    assert options == [{"total_seconds": 180, "request_timeout_seconds": 12, "interval_seconds": 1}]


def test_verified_numbered_sample_keeps_all_83_boundaries_with_missing_degree_slots():
    sample = json.loads((Path(__file__).parent / "fixtures/sources/sspu/numbered-redacted.json").read_text(encoding="utf-8"))
    jobs = adapter().parse_sspu_details(sample, replace(ANNOUNCEMENT, title=sample["data"]["newsTitle"]))
    assert {j.identity_key for j in jobs} == {f"101:{n}" for n in range(1, 84)}
    by_id = {j.identity_key: j for j in jobs}
    for number in [31, 44, 80]:
        assert "2027" not in by_id[f"101:{number}"].evidence_text
    for number in [56, 57]:
        assert "2027" in by_id[f"101:{number}"].evidence_text
    assert "胶州" in by_id["101:39"].location_detail
    assert by_id["101:50"].location_category == "其他地区"
    assert by_id["101:51"].location_category == "其他地区"
    assert by_id["101:21"].location_detail == "北京/济南"
    assert "线上业务中心销售培训生" not in by_id["101:17"].evidence_text


def test_verified_labelled_sample_has_three_isolated_jobs_with_common_location():
    sample = json.loads((Path(__file__).parent / "fixtures/sources/sspu/labelled-redacted.json").read_text(encoding="utf-8"))
    jobs = adapter().parse_sspu_details(sample, replace(ANNOUNCEMENT, title=sample["data"]["newsTitle"]))
    assert len(jobs) == 3
    assert all("杨浦区" in j.location_detail and "2027" in j.evidence_text and "4000" in j.evidence_text for j in jobs)
    assert "现代物流管理" in jobs[1].evidence_text and "国际经济与贸易" not in jobs[1].evidence_text
    assert "国际经济与贸易" in jobs[2].evidence_text and "现代物流管理" not in jobs[2].evidence_text


def test_explicit_school_restriction_survives_but_general_company_intro_does_not():
    html = "<p>公司规划：2027年建设新园区。</p><p>本次招聘仅限上海第二工业大学学生。</p>" + labelled("数据助理", "本科及以上。")
    job = adapter().parse_sspu_details(payload(html), ANNOUNCEMENT)[0]
    assert "仅限上海第二工业大学学生" in job.evidence_text
    assert "2027" not in job.evidence_text


def test_business_contact_email_is_not_an_application_channel():
    html = "<p>业务合作联系邮箱：sales@example.com</p><p>联系邮箱：office@example.com</p>" + labelled("数据助理", "本科及以上。")
    job = adapter().parse_sspu_details(payload(html), ANNOUNCEMENT)[0]
    assert "sales@example.com" not in job.evidence_text
    assert "office@example.com" not in job.evidence_text


def test_known_numbered_heading_with_unknown_pay_format_is_not_merged_into_previous_job():
    html = numbered(1, "数据助理") + "<p>2、技术助理（1名）</p><p>薪酬另议，工作地点待定</p><p>硕士及以上，2027届。</p>"
    with pytest.raises(ValueError, match="分段"):
        adapter().parse_sspu_details(payload(html), ANNOUNCEMENT)


@pytest.mark.parametrize("place,category", [
    ("山东（前期上海培养）", "其他地区"),
    ("山东，前期在上海培训", "其他地区"),
    ("前期上海培训，后期山东工作", "其他地区"),
    ("上海（培训地）/山东（正式工作地）", "其他地区"),
    ("上海总部", "原文未明确"),
    ("上海面试", "原文未明确"),
    ("上海/山东", "明确上海"),
    ("base上海，需出差", "明确上海"),
])
def test_transient_shanghai_training_or_headquarters_is_not_workplace(place, category):
    job = adapter().parse_sspu_details(payload(numbered(50, "财务管培生", place)), ANNOUNCEMENT)[0]
    assert job.location_detail == place
    assert job.location_category == category


@pytest.mark.parametrize("restriction", [
    "只限本校学生报名", "只接受上海第二工业大学毕业生", "仅接受本校学生", "只招本校毕业生", "仅招本校毕业生", "限本校学生", "仅面向二工大学生", "仅限本校",
    "学历要求：硕士及以上", "招聘对象：仅博士", "报名条件：仅限硕士研究生", "应聘要求：不接受本科生",
    "报名截止：2026年10月30日", "投递截止时间：2026-10-30", "截止日期：2026-10-30",
])
def test_explicit_announcement_constraints_are_preserved(restriction):
    html = f"<p>{restriction}</p>" + labelled("数据助理", "维护业务数据。")
    job = adapter().parse_sspu_details(payload(html), ANNOUNCEMENT)[0]
    assert restriction in job.evidence_text


def test_announcement_constraint_heading_applies_to_its_following_value():
    html = "<p>招聘对象：</p><p>仅硕士、博士研究生。</p><p>报名截止时间：</p><p>2026年10月30日。</p>" + labelled("数据助理", "维护业务数据。")
    evidence = adapter().parse_sspu_details(payload(html), ANNOUNCEMENT)[0].evidence_text
    assert "仅硕士、博士研究生" in evidence and "2026年10月30日" in evidence


@pytest.mark.parametrize("body,code", [("<p><img src='/poster.jpg'></p>", "IMAGE_ONLY_UNSUPPORTED"), ("<p>招聘岗位清单：研发、设计、营销。</p><p>详见下方投递页面。</p>", "LAYOUT_UNSUPPORTED")])
def test_unsupported_layouts_are_explicit_skips(body, code):
    source = adapter()
    assert hasattr(source, "UnsupportedSspuStructure")
    with pytest.raises(source.UnsupportedSspuStructure) as error:
        source.parse_sspu_details(payload(body), ANNOUNCEMENT)
    assert isinstance(error.value, source.UnsupportedSspuAnnouncement)
    assert error.value.reason_code == code


def test_empty_body_is_data_error_not_an_unsupported_skip():
    source = adapter()
    with pytest.raises(ValueError) as error:
        source.parse_sspu_details(payload(""), ANNOUNCEMENT)
    assert not isinstance(error.value, source.UnsupportedSspuAnnouncement)


@pytest.mark.parametrize("later_job", [
    labelled("北京高级工程师", "博士及以上，工作地点：北京。"),
    "<p>岗位名称：北京高级工程师</p><p>博士及以上，工作地点：北京。</p>",
    numbered(2, "高级工程师", "北京", "博士及以上。"),
    "<p>2、高级工程师（1名）</p><p>博士及以上，工作地点：北京。</p>",
])
def test_later_job_after_benefits_is_unsupported_not_shared_qualifications(later_job):
    source = adapter()
    html = labelled("上海助理", "本科及以上，工作地点：上海。") + "<p>福利待遇</p><p>五险一金</p>" + later_job
    with pytest.raises(source.UnsupportedSspuStructure) as error:
        source.parse_sspu_details(payload(html), ANNOUNCEMENT)
    assert error.value.reason_code == "LAYOUT_UNSUPPORTED"
