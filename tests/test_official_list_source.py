from app.sources.official_list import OfficialListing, parse_official_detail_html, parse_official_listing_html
from app.services.real_collection import _save_detail
from app.models import Job, Source


def test_parse_official_listing_html_keeps_only_dated_recruitment_notices():
    html = """
    <ul>
      <li><a href="/jobs/one.html">某集团2027届校园招聘公告</a><span>2026-08-26</span></li>
      <li><a href="/news/two.html">行业工作动态</a><span>2026-08-25</span></li>
      <li><a href="/jobs/three.html">事业单位公开招聘工作人员</a><span>2026-08-24</span></li>
    </ul>
    """

    listings = parse_official_listing_html(html, "https://example.com/list/")

    assert [item.title for item in listings] == [
        "某集团2027届校园招聘公告",
        "事业单位公开招聘工作人员",
    ]
    assert listings[0].detail_url == "https://example.com/jobs/one.html"


def test_parse_official_detail_html_preserves_official_attachment_links():
    listing = OfficialListing("2026年公开招聘公告", "2026-08-31", "https://example.com/notices/1.html")
    html = '''<html><body><h1>2026年公开招聘公告</h1><p>发布时间：2026-08-31</p>
    <p>岗位要求详见附件。</p><a href="files/roles.xlsx">附件1 岗位说明.xlsx</a>
    <a href="files/form.docx">附件2 报名表.docx</a><a href="/about">联系我们</a></body></html>'''

    detail = parse_official_detail_html(html, listing)

    assert [(item.name, item.url) for item in detail.attachments] == [
        ("附件1 岗位说明.xlsx", "https://example.com/notices/files/roles.xlsx"),
        ("附件2 报名表.docx", "https://example.com/notices/files/form.docx"),
    ]


def test_official_detail_save_persists_attachment_links(session):
    source = Source(name="附件测试来源", url="https://example.com", level="一级", source_type="公共平台")
    session.add(source)
    session.commit()
    detail = parse_official_detail_html(
        '<h1>测试招聘公告</h1><p>发布时间：2026-08-31</p><p>正文内容足够保存。</p>'
        '<a href="roles.xlsx">岗位说明.xlsx</a>',
        OfficialListing("测试招聘公告", "2026-08-31", "https://example.com/notices/test.html"),
    )

    _save_detail(session, source, detail, __import__("datetime").datetime.now())
    session.commit()
    job = session.query(Job).filter_by(is_demo=False).one()

    assert "岗位说明.xlsx" in job.attachment_links
    assert "https://example.com/notices/roles.xlsx" in job.attachment_links


def test_official_detail_save_uses_ai_intake_result_when_available(session):
    source = Source(name="AI 初筛来源", url="https://example.com", level="一级", source_type="公共平台")
    session.add(source)
    session.commit()
    detail = parse_official_detail_html(
        '<h1>银行管培生招聘</h1><p>发布时间：2026-08-31</p><p>面向2027届应届毕业生。</p>',
        OfficialListing("银行管培生招聘", "2026-08-31", "https://example.com/notices/ai.html"),
    )

    _save_detail(
        session,
        source,
        detail,
        __import__("datetime").datetime.now(),
        lambda _prompt: '{"grade":"A","reason":"面向应届生","evidence":"2027届应届毕业生","confidence":"高"}',
    )
    session.commit()

    job = session.query(Job).filter_by(is_demo=False).one()
    assert job.intake_grade == "A"
    assert job.intake_evidence == "2027届应届毕业生"
