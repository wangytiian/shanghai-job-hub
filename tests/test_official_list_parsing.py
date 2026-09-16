from app.sources.official_list import OfficialListing, parse_official_detail_html


def test_official_detail_prefers_explicit_article_container_and_keeps_attachment_metadata():
    listing = OfficialListing("招聘公告", "2026-09-01", "https://example.com/notices/1")
    html = """
    <html><body>
      <div class="nav">首页 信息公开 通知公告</div>
      <article id="article-content"><h1>招聘公告</h1><p>发布时间：2026-09-01</p>
      <p>报名时间：2026年9月1日至2026年9月20日。</p>
      <a href="files/roles.xlsx">岗位明细表</a></article>
      <div class="footer">联系我们 版权所有</div>
    </body></html>
    """

    detail = parse_official_detail_html(html, listing)

    assert "报名时间" in detail.evidence_text
    assert "首页 信息公开" not in detail.evidence_text
    assert "版权所有" not in detail.evidence_text
    assert detail.attachments[0].url == "https://example.com/notices/files/roles.xlsx"
