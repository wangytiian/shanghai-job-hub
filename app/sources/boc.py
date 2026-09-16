"""Public China Bank recruitment announcements parser."""

import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.sources.content_extraction import extract_article_text
from app.sources.official_list import OfficialAttachment, OfficialDetail, OfficialListing
from app.sources.shanghai_sasac import DATE_PATTERN, USER_AGENT


BOC_LIST_URL = "https://www.boc.cn/aboutboc/bi4/"
_ATTACHMENT_SUFFIXES = (".xlsx", ".xls", ".pdf", ".docx", ".doc", ".zip")


def parse_boc_list(html: str, base_url: str = BOC_LIST_URL) -> list[OfficialListing]:
    soup = BeautifulSoup(html, "html.parser")
    listings: list[OfficialListing] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        title = anchor.get_text(" ", strip=True)
        parent_text = anchor.parent.get_text(" ", strip=True) if anchor.parent else title
        date = DATE_PATTERN.search(parent_text)
        href = anchor["href"]
        url = urljoin("https://www.boc.cn/aboutboc/", href) if href.startswith("bi4/") else urljoin(base_url, href)
        if not title or not date or not any(word in title for word in ("招聘", "招收", "录用公示")):
            continue
        if url in seen or not url.startswith(("http://", "https://")):
            continue
        listings.append(OfficialListing(title, date.group(0), url))
        seen.add(url)
    return listings


def parse_boc_detail(html: str, listing: OfficialListing) -> OfficialDetail:
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else listing.title)
    evidence_text = extract_article_text(soup)
    if len(evidence_text) < 20:
        raise ValueError("中国银行公告详情页未提取到可保存的正文")
    attachments: list[OfficialAttachment] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        url = urljoin(listing.detail_url, anchor["href"])
        if not url.lower().split("?", 1)[0].endswith(_ATTACHMENT_SUFFIXES) or url in seen:
            continue
        attachments.append(OfficialAttachment(anchor.get_text(" ", strip=True) or url.rsplit("/", 1)[-1], url))
        seen.add(url)
    return OfficialDetail(title, listing.published_at, listing.detail_url, evidence_text, tuple(attachments))


def _get(client, url: str) -> str:
    response = client.get(url, headers={"User-Agent": USER_AGENT}, timeout=12.0)
    response.raise_for_status()
    return response.text


def fetch_boc_listings(client, limit: int = 12) -> list[OfficialListing]:
    if not 1 <= limit <= 12:
        raise ValueError("采集数量必须在1到12条之间")
    return parse_boc_list(_get(client, BOC_LIST_URL))[:limit]


def fetch_boc_detail(client, listing: OfficialListing) -> OfficialDetail:
    detail = parse_boc_detail(_get(client, listing.detail_url), listing)
    time.sleep(0.5)
    return detail
