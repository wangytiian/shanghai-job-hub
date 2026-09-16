"""Public job pages from Shanghai Business School's employment website.

The site uses ordinary HTML pages and ASP.NET postbacks for pagination.  This
adapter only follows the public list and detail pages verified in the source
probe; it does not log in or bypass any access restriction.
"""

from dataclasses import dataclass
from datetime import datetime
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from app.services.source_request_budget import RequestBudgetController, is_transport_timeout
from app.sources.shanghai_sasac import USER_AGENT


SBS_BASE_URL = "https://jiuye.sbs.edu.cn"
SBS_LIST_URL = f"{SBS_BASE_URL}/PositionList.aspx/"
_DETAIL_PATH = re.compile(r"/PositionDetail\.aspx\?zwid=(\d+)", re.IGNORECASE)
_NEXT_EVENT = re.compile(r"__doPostBack\('([^']*btnNext)'\s*,\s*''\)")
_URL = re.compile(r"https?://[^\s<>\"']+")


@dataclass(frozen=True)
class SbsListing:
    job_id: str
    employer_name: str
    title: str
    published_at: str
    detail_url: str


@dataclass(frozen=True)
class SbsJobDetail:
    title: str
    published_at: str
    detail_url: str
    evidence_text: str
    identity_key: str
    employer_name: str
    location_category: str
    location_detail: str
    official_url: str
    deadline: str = ""
    recruitment_type: str = "待核验"


def _text(value: object) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def _date(value: str) -> str:
    normalized = _text(value).replace("年", "-").replace("月", "-").replace("日", "")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(normalized, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def _detail_id(url: str) -> str:
    parsed = urlsplit(url)
    query_id = parse_qs(parsed.query).get("zwid", [""])[0]
    if query_id.isdigit():
        return query_id
    match = _DETAIL_PATH.search(parsed.path + (f"?{parsed.query}" if parsed.query else ""))
    return match.group(1) if match else ""


def parse_sbs_listings(html: str) -> list[SbsListing]:
    """Parse one public list page without assuming an unverified page API."""
    soup = BeautifulSoup(html, "html.parser")
    listings: list[SbsListing] = []
    seen_ids: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        detail_url = urljoin(SBS_BASE_URL + "/", anchor["href"])
        job_id = _detail_id(detail_url)
        if not job_id or job_id in seen_ids:
            continue
        row = anchor.find_parent("tr")
        cells = row.find_all("td", recursive=False) if row else []
        if len(cells) < 5:
            continue
        employer_name = _text(cells[0].get_text(" ", strip=True))
        title = _text(cells[1].get_text(" ", strip=True))
        published_at = _date(cells[-2].get_text(" ", strip=True))
        if not employer_name or not title or not published_at:
            continue
        listings.append(SbsListing(job_id, employer_name, title, published_at, detail_url))
        seen_ids.add(job_id)
    return listings


def _field_map(soup: BeautifulSoup) -> dict[str, str]:
    fields: dict[str, str] = {}
    for row in soup.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        for index in range(0, len(cells) - 1, 2):
            label = _text(cells[index].get_text(" ", strip=True)).rstrip("：:")
            value = _text(cells[index + 1].get_text(" ", strip=True))
            if label and value and label not in fields:
                fields[label] = value
    return fields


def _description_html(soup: BeautifulSoup) -> str:
    for row in soup.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if cells and _text(cells[0].get_text(" ", strip=True)).rstrip("：:") == "职位描述":
            return _text(" ".join(cell.get_text(" ", strip=True) for cell in cells[1:]))
    return ""


def parse_sbs_detail(html: str, listing: SbsListing) -> SbsJobDetail:
    """Normalize one verified public detail table into the shared detail shape."""
    soup = BeautifulSoup(html, "html.parser")
    fields = _field_map(soup)
    description = _description_html(soup)
    if not description:
        raise ValueError("上海商学院就业网详情缺少职位描述")
    employer_name = fields.get("公司名称", "") or listing.employer_name
    title = fields.get("职位名称", "") or listing.title
    location_detail = fields.get("工作地区", "")
    location_category = "明确上海" if "上海" in location_detail else "其他地区" if location_detail else "原文未明确"
    official_url_match = _URL.search(description)
    official_url = official_url_match.group(0).rstrip("。；，,;") if official_url_match else ""
    deadline = _date(fields.get("截止日期", ""))
    published_at = _date(fields.get("发布日期", "")) or listing.published_at
    evidence_lines = [
        f"招聘公告：{listing.title}", f"发布时间：{published_at}", f"招聘单位：{employer_name}",
        f"岗位名称：{title}", f"岗位地点：{location_detail}", f"职位类别：{fields.get('职位类别', '')}",
        f"专业要求：{fields.get('要求专业', '')}", f"学历要求：{fields.get('要求学历', '')}",
        f"截止日期：{deadline}", f"正文：{description}",
    ]
    if official_url:
        evidence_lines.append(f"官方报名入口：{official_url}")
    email = fields.get("简历投递邮箱", "")
    if email:
        evidence_lines.append(f"官方投递邮箱：{email}")
    return SbsJobDetail(title, published_at, listing.detail_url, "\n".join(evidence_lines), listing.job_id, employer_name, location_category, location_detail, official_url, deadline, fields.get("职位类别", "") or "待核验")


def _request(client, method: str, url: str, *, data: dict[str, str] | None = None, timeout: float = 12.0, controller: RequestBudgetController | None = None):
    for attempt in range(2):
        request_timeout = controller.before_request() if controller else timeout
        try:
            kwargs = {"headers": {"User-Agent": USER_AGENT}, "timeout": request_timeout}
            if data is not None:
                kwargs["data"] = data
            response = getattr(client, method)(url, **kwargs)
            response.raise_for_status()
            if controller:
                controller.after_request()
            return response
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = is_transport_timeout(exc) or (status_code is not None and status_code >= 500)
            if not controller or not retryable or attempt == 1:
                raise
            controller.wait(2)
    raise RuntimeError("unreachable")


def _next_post_data(html: str) -> dict[str, str] | None:
    soup = BeautifulSoup(html, "html.parser")
    anchor = next((item for item in soup.find_all("a", href=True) if _NEXT_EVENT.search(item["href"])), None)
    if anchor is None:
        return None
    event = _NEXT_EVENT.search(anchor["href"])
    hidden = {item["name"]: item.get("value", "") for item in soup.select('input[type="hidden"][name]')}
    hidden["__EVENTTARGET"] = event.group(1)
    return hidden


def fetch_sbs_listings(client, pages: int = 1, *, timeout: float = 12.0, controller: RequestBudgetController | None = None) -> list[SbsListing]:
    """Read one to three public list pages via the site's verified postback flow."""
    if not 1 <= pages <= 3:
        raise ValueError("采集页数必须在1到3页之间")
    response = _request(client, "get", SBS_LIST_URL, timeout=timeout, controller=controller)
    listings: list[SbsListing] = []
    seen_ids: set[str] = set()
    for page_index in range(pages):
        for listing in parse_sbs_listings(response.text):
            if listing.job_id not in seen_ids:
                listings.append(listing)
                seen_ids.add(listing.job_id)
        if page_index + 1 == pages:
            break
        payload = _next_post_data(response.text)
        if payload is None:
            break
        response = _request(client, "post", SBS_LIST_URL, data=payload, timeout=timeout, controller=controller)
    return listings


def fetch_sbs_detail(client, listing: SbsListing, *, timeout: float = 12.0, controller: RequestBudgetController | None = None) -> SbsJobDetail:
    """Read one public position detail page."""
    response = _request(client, "get", listing.detail_url, timeout=timeout, controller=controller)
    return parse_sbs_detail(response.text, listing)
