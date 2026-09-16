import pytest

from app.sources.shanghai_sasac import ShanghaiSasacListing, parse_detail_html, parse_listing_html


LISTING_HTML = """
<ul>
  <li>2026-06-08 <a href="/article.html">上海示例国企暑期实习启动</a></li>
</ul>
"""


def test_parses_public_listing_with_absolute_detail_url():
    listings = parse_listing_html(LISTING_HTML, base_url="https://www.gzw.sh.gov.cn")

    assert listings[0].title == "上海示例国企暑期实习启动"
    assert listings[0].published_at == "2026-06-08"
    assert listings[0].detail_url == "https://www.gzw.sh.gov.cn/article.html"


def test_rejects_access_denied_page_instead_of_saving_navigation_as_evidence():
    listing = ShanghaiSasacListing("上海示例国企暑期实习启动", "2026-06-08", "https://example.com/a")
    blocked_html = "<html><title>403 Forbidden</title><body>访问受限，请稍后重试</body></html>"

    with pytest.raises(ValueError, match="访问受限"):
        parse_detail_html(blocked_html, listing)


def test_prefers_article_body_and_excludes_navigation_and_footer():
    listing = ShanghaiSasacListing("上海示例国企暑期实习启动", "2026-06-08", "https://example.com/a")
    html = """
    <html><body>
      <nav>首页 信息公开 联系我们</nav>
      <main class="article-content"><h1>上海示例国企暑期实习启动</h1>
      <p>报名时间：2026年6月8日至2026年6月18日。</p><p>面向2027届学生招聘财务实习生。</p></main>
      <footer>版权所有 网站地图</footer>
    </body></html>
    """

    detail = parse_detail_html(html, listing)

    assert "面向2027届学生" in detail.evidence_text
    assert "首页 信息公开" not in detail.evidence_text
    assert "版权所有" not in detail.evidence_text
