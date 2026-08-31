from dataclasses import dataclass
from datetime import datetime
import re
import time
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup


LISTING_URL = "https://www.gzw.sh.gov.cn/shgzw_xxgk_cqzp/"
USER_AGENT = "LixinRecruitingLocal/0.1 (public-information-research; local-only)"
DATE_PATTERN = re.compile(r"20\d{2}-\d{2}-\d{2}")


@dataclass(frozen=True)
class ShanghaiSasacListing:
    title: str
    published_at: str
    detail_url: str


@dataclass(frozen=True)
class ShanghaiSasacDetail:
    title: str
    published_at: str
    detail_url: str
    body_text: str
    evidence_text: str


def parse_listing_html(html: str, base_url: str = LISTING_URL) -> list[ShanghaiSasacListing]:
    soup = BeautifulSoup(html, "html.parser")
    listings: list[ShanghaiSasacListing] = []
    seen_urls: set[str] = set()
    for anchor in soup.select("a[href]"):
        container_text = anchor.parent.get_text(" ", strip=True)
        match = DATE_PATTERN.search(container_text)
        title = anchor.get_text(" ", strip=True).lstrip("•").strip()
        detail_url = urljoin(base_url, anchor["href"])
        if not match or not title or detail_url in seen_urls:
            continue
        listings.append(
            ShanghaiSasacListing(
                title=title,
                published_at=match.group(0),
                detail_url=detail_url,
            )
        )
        seen_urls.add(detail_url)
    return listings


def parse_detail_html(html: str, listing: ShanghaiSasacListing) -> ShanghaiSasacDetail:
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    title = heading.get_text(" ", strip=True) if heading else listing.title
    page_text = soup.get_text("\n", strip=True)
    date_match = DATE_PATTERN.search(page_text)
    published_at = date_match.group(0) if date_match else listing.published_at
    paragraphs = [
        item.get_text(" ", strip=True)
        for item in soup.find_all(["p", "div"])
        if item.get_text(" ", strip=True)
    ]
    body_text = "\n".join(dict.fromkeys(paragraphs))
    if len(body_text) < 20:
        body_text = page_text
    if len(body_text) < 20:
        raise ValueError("公告详情页未提取到可保存的正文")
    evidence_text = body_text[:5000]
    return ShanghaiSasacDetail(
        title=title,
        published_at=published_at,
        detail_url=listing.detail_url,
        body_text=body_text,
        evidence_text=evidence_text,
    )


def _get(client: httpx.Client, url: str) -> str:
    response = client.get(url, headers={"User-Agent": USER_AGENT}, timeout=10.0)
    response.raise_for_status()
    return response.text


def fetch_shanghai_sasac_listings(
    client: httpx.Client, limit: int = 12
) -> list[ShanghaiSasacListing]:
    if not 1 <= limit <= 12:
        raise ValueError("采集数量必须在1到12条之间")
    return parse_listing_html(_get(client, LISTING_URL))[:limit]


def fetch_shanghai_sasac_detail(
    client: httpx.Client, listing: ShanghaiSasacListing
) -> ShanghaiSasacDetail:
    detail = parse_detail_html(_get(client, listing.detail_url), listing)
    time.sleep(0.5)
    return detail
