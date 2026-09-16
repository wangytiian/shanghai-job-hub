from app.sources.boc import BOC_LIST_URL, fetch_boc_detail, fetch_boc_listings, parse_boc_detail, parse_boc_list
from app.sources.official_list import OfficialListing


LIST_HTML = """
<html><body>
  <nav><a href='/aboutboc/bi4/'>招聘公告</a><a href='/aboutboc/ab8/'>媒体看中行</a></nav>
  <div class='content'>
    <ul><li><a href='bi4/2026/0903/123.html'>中国银行股份有限公司2027年全球校园招聘公告</a><span>[ 2026-09-03 ]</span></li></ul>
  </div>
</body></html>
"""

DETAIL_HTML = """
<html><body>
  <header>中国银行首页 产品服务</header>
  <main class='content'><h1>中国银行股份有限公司2027年全球校园招聘公告</h1>
    <p>发布时间：2026-09-03</p><p>面向境内外院校应届毕业生招聘。</p>
    <a href='../files/2027-campus.pdf'>招聘公告附件.pdf</a>
  </main>
  <footer>服务热线 版权所有</footer>
</body></html>
"""


class Response:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class Client:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(self.pages[url])


def test_parse_boc_list_keeps_dated_recruitment_announcements_and_drops_navigation():
    listings = parse_boc_list(LIST_HTML, BOC_LIST_URL)

    assert len(listings) == 1
    assert listings[0].published_at == "2026-09-03"
    assert listings[0].detail_url == "https://www.boc.cn/aboutboc/bi4/2026/0903/123.html"


def test_parse_boc_detail_extracts_article_body_and_public_attachment_url():
    listing = OfficialListing("中国银行股份有限公司2027年全球校园招聘公告", "2026-09-03", "https://www.boc.cn/aboutboc/bi4/2026/0903/123.html")
    detail = parse_boc_detail(DETAIL_HTML, listing)

    assert "应届毕业生招聘" in detail.evidence_text
    assert "服务热线" not in detail.evidence_text
    assert detail.attachments[0].url == "https://www.boc.cn/aboutboc/bi4/2026/files/2027-campus.pdf"


def test_fetch_boc_uses_the_correct_recruitment_listing_url_and_details():
    detail_url = "https://www.boc.cn/aboutboc/bi4/2026/0903/123.html"
    client = Client({BOC_LIST_URL: LIST_HTML, detail_url: DETAIL_HTML})

    listings = fetch_boc_listings(client)
    detail = fetch_boc_detail(client, listings[0])

    assert client.calls[0][0] == BOC_LIST_URL
    assert detail.detail_url == detail_url
