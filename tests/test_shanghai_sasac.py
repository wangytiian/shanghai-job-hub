from app.sources.shanghai_sasac import parse_listing_html


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
