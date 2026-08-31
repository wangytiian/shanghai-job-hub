from dataclasses import dataclass
import re
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.sources.shanghai_sasac import DATE_PATTERN, USER_AGENT


RECRUITMENT_PATTERN = re.compile(r"招聘|招录|招考|校园|实习|就业|人才|应聘")


@dataclass(frozen=True)
class OfficialListing:
    title: str
    published_at: str
    detail_url: str


@dataclass(frozen=True)
class OfficialAttachment:
    name: str
    url: str


@dataclass(frozen=True)
class OfficialDetail:
    title: str
    published_at: str
    detail_url: str
    evidence_text: str
    attachments: tuple[OfficialAttachment, ...] = ()


def parse_official_listing_html(html: str, base_url: str) -> list[OfficialListing]:
    soup = BeautifulSoup(html, "html.parser")
    listings: list[OfficialListing] = []
    seen_urls: set[str] = set()
    for anchor in soup.select("a[href]"):
        title = anchor.get_text(" ", strip=True).lstrip("•").strip()
        container = anchor.parent
        container_text = container.get_text(" ", strip=True) if container else title
        date_match = DATE_PATTERN.search(container_text)
        detail_url = urljoin(base_url, anchor["href"])
        if not title or not date_match or not RECRUITMENT_PATTERN.search(title):
            continue
        if detail_url in seen_urls or not detail_url.startswith(("http://", "https://")):
            continue
        listings.append(OfficialListing(title, date_match.group(0), detail_url))
        seen_urls.add(detail_url)
    return listings


def parse_official_detail_html(html: str, listing: OfficialListing) -> OfficialDetail:
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    title = heading.get_text(" ", strip=True) if heading else listing.title
    page_text = soup.get_text("\n", strip=True)
    date_match = DATE_PATTERN.search(page_text)
    published_at = date_match.group(0) if date_match else listing.published_at
    if len(page_text) < 20:
        raise ValueError("公告详情页未提取到可保存的正文")
    extensions = (".xlsx", ".xls", ".pdf", ".docx", ".doc", ".zip")
    attachments: list[OfficialAttachment] = []
    seen_urls: set[str] = set()
    for anchor in soup.select("a[href]"):
        url = urljoin(listing.detail_url, anchor["href"])
        name = anchor.get_text(" ", strip=True) or url.rsplit("/", 1)[-1]
        if not url.lower().split("?", 1)[0].endswith(extensions) or url in seen_urls:
            continue
        attachments.append(OfficialAttachment(name=name, url=url))
        seen_urls.add(url)
    return OfficialDetail(title, published_at, listing.detail_url, page_text[:5000], tuple(attachments))


def _get(client, url: str) -> str:
    response = client.get(url, headers={"User-Agent": USER_AGENT}, timeout=12.0)
    response.raise_for_status()
    return response.text


def fetch_official_listings(client, listing_url: str, limit: int = 12) -> list[OfficialListing]:
    if not 1 <= limit <= 12:
        raise ValueError("采集数量必须在1到12条之间")
    return parse_official_listing_html(_get(client, listing_url), listing_url)[:limit]


def fetch_official_detail(client, listing: OfficialListing) -> OfficialDetail:
    detail = parse_official_detail_html(_get(client, listing.detail_url), listing)
    time.sleep(0.5)
    return detail
