"""Public Shanghai job listings from the National College Student Employment Service Platform."""

from dataclasses import dataclass
from datetime import datetime
import json
import re
import time

from bs4 import BeautifulSoup

from app.sources.shanghai_sasac import USER_AGENT
from app.services.source_request_budget import RequestBudgetController, is_transport_timeout


NCSS_BASE_URL = "https://www.ncss.cn"
NCSS_LIST_URL = f"{NCSS_BASE_URL}/student/jobs/jobslist/ajax/"


@dataclass(frozen=True)
class NcssListing:
    job_id: str
    title: str
    employer_name: str
    source_name: str
    published_at: str
    detail_url: str


@dataclass(frozen=True)
class NcssDetail:
    title: str
    published_at: str
    detail_url: str
    evidence_text: str
    identity_key: str
    employer_name: str
    location_category: str
    location_detail: str
    recruitment_type: str = "待核验"
    official_url: str = ""
    native_application_verified: bool = False
    native_application_selector: str = ""
    native_application_href: str = ""


def _published_date(value: object) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def parse_ncss_list(payload: str) -> list[NcssListing]:
    parsed = json.loads(payload)
    rows = parsed.get("data", {}).get("list", [])
    if not isinstance(rows, list):
        raise ValueError("国家大学生就业服务平台列表结构异常")
    listings: list[NcssListing] = []
    seen_ids: set[str] = set()
    for row in rows[:10]:
        if not isinstance(row, dict) or str(row.get("areaCodeName", "")).strip() != "上海":
            continue
        job_id = str(row.get("jobId", "")).strip()
        title = str(row.get("jobName", "")).strip()
        employer_name = str(row.get("recName", "")).strip()
        if not job_id or not title or not employer_name or job_id in seen_ids:
            continue
        listings.append(
            NcssListing(
                job_id=job_id,
                title=title,
                employer_name=employer_name,
                source_name=str(row.get("sourcesNameCh", "")).strip() or "国家大学生就业服务平台",
                published_at=_published_date(row.get("publishDate")),
                detail_url=f"{NCSS_BASE_URL}/student/jobs/{job_id}/detail.html",
            )
        )
        seen_ids.add(job_id)
    return listings


def parse_ncss_detail(html: str, listing: NcssListing) -> NcssDetail:
    soup = BeautifulSoup(html, "html.parser")
    for node in list(soup.find_all(True)):
        if node.parent is None:
            continue
        classes = node.get("class", [])
        style = re.sub(r"\s+", "", node.get("style", "").lower())
        if (node.name in {"script", "style", "nav", "header", "footer"}
                or any(c in classes for c in ("modal", "hide", "hidden", "d-none"))
                or node.has_attr("hidden") or node.get("aria-hidden") == "true"
                or "display:none" in style or "visibility:hidden" in style):
            node.decompose()
    visible_text = soup.get_text(" ", strip=True)
    if "职位已下线" in visible_text:
        raise ValueError("国家大学生就业服务平台职位已下线")
    root = soup.select_one(".jobdetail-box")
    card = soup.select_one(".job")
    native_selector = ".job .apply > a[href='#']"
    native_control = soup.select_one(native_selector)
    native_verified = bool(native_control and native_control.get_text(" ", strip=True) == "投递简历"
                           and not native_control.has_attr("disabled")
                           and native_control.get("aria-disabled") != "true")
    legacy_control = root and next((n for n in root.select("button, a")
                                   if n.get_text(" ", strip=True) == "投递简历"
                                   and not n.has_attr("disabled")
                                   and n.get("aria-disabled") != "true"), None)
    if not native_verified and not legacy_control:
        raise ValueError("国家大学生就业服务平台职位未确认仍可投递")
    evidence_text = root.get_text("\n", strip=True) if root else ""
    if len(evidence_text) < 20:
        raise ValueError("国家大学生就业服务平台职位详情正文不足")
    if root and root.parent and "details" in root.parent.get("class", []):
        heading = root.parent.find("p", class_="title", recursive=False)
        if heading and heading.get_text(" ", strip=True) == "职位详情":
            evidence_text = "职位详情\n" + evidence_text
    title_node = soup.select_one("#jobName, .job-title, h1")
    employer_node = soup.select_one("#corpName, .corpName")
    title = title_node.get_text(" ", strip=True) if title_node else listing.title
    employer_name = employer_node.get_text(" ", strip=True) if employer_node else listing.employer_name
    address_nodes = card.select(".address .site-tag") if card else []
    location_detail = "、".join(n.get_text(" ", strip=True) for n in address_nodes)
    if not location_detail:
        match = re.search(r"工作地点[：:]\s*([^\n。；]+)", root.get_text("\n", strip=True) if root else "")
        location_detail = match.group(1).strip() if match else ""
    facts = [f"岗位名称：{title}", f"企业名称：{employer_name}"]
    if card:
        facts.append(card.get_text(" ", strip=True))
    if location_detail:
        facts.append(f"工作地点：{location_detail}")
    if native_verified:
        facts.append("平台原生投递证据：.job .apply > a[href='#']；按钮=投递简历；原始href=#；需登录后提交")
    evidence_text = "\n".join([*facts, evidence_text])
    return NcssDetail(
        title=title or listing.title,
        published_at=listing.published_at,
        detail_url=listing.detail_url,
        evidence_text=evidence_text,
        identity_key=listing.job_id,
        employer_name=employer_name or listing.employer_name,
        location_category="上海" if "上海" in location_detail else ("异地" if location_detail else "原文未明确"),
        location_detail=location_detail,
        native_application_verified=native_verified,
        native_application_selector=native_selector if native_verified else "",
        native_application_href="#" if native_verified else "",
    )


def _get(client, url: str, *, params: dict[str, str] | None = None, timeout: float = 12.0, controller: RequestBudgetController | None = None):
    for attempt in range(2):
        request_timeout = controller.before_request() if controller else timeout
        try:
            response = client.get(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=request_timeout,
                                  follow_redirects=False)
            response.raise_for_status()
            if controller:
                controller.after_request()
            return response
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = is_transport_timeout(exc) or (
                status_code is not None and status_code >= 500
            )
            if not controller or not retryable or attempt == 1:
                raise
            controller.wait(2)
    raise RuntimeError("unreachable")


def fetch_ncss_shanghai_listings(
    client,
    pages: int = 3,
    *,
    timeout: float = 12.0,
    request_interval: float = 0.0,
    controller: RequestBudgetController | None = None,
    keywords: list[str] | tuple[str, ...] | None = None,
) -> list[NcssListing]:
    if not 1 <= pages <= 3:
        raise ValueError("采集页数必须在1到3页之间")
    listings: list[NcssListing] = []
    seen_ids: set[str] = set()
    terms = list(dict.fromkeys(str(k).strip() for k in (keywords or [""]))) or [""]
    for index in range(pages):
        keyword = terms[index % len(terms)]
        offset = index // len(terms) + 1
        response = _get(
            client,
            NCSS_LIST_URL,
            params={"areaCode": "310000", "offset": str(offset), "limit": "10", "jobType": "", "jobName": keyword},
            timeout=timeout,
            controller=controller,
        )
        page_listings = parse_ncss_list(response.text)
        if not page_listings and len(terms) == 1:
            break
        for listing in page_listings:
            if listing.job_id not in seen_ids:
                listings.append(listing)
                seen_ids.add(listing.job_id)
        if request_interval and controller is None and index < pages - 1:
            time.sleep(request_interval)
    return listings


def fetch_ncss_detail(
    client,
    listing: NcssListing,
    *,
    timeout: float = 12.0,
    request_interval: float = 0.5,
    controller: RequestBudgetController | None = None,
) -> NcssDetail:
    detail = parse_ncss_detail(
        _get(client, listing.detail_url, timeout=timeout, controller=controller).text,
        listing,
    )
    if controller is None:
        time.sleep(request_interval)
    return detail
