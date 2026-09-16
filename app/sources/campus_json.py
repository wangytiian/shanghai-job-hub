"""Reusable adapter for public campus-recruitment JSON endpoints.

The caller supplies each school's publicly verified POST paths.  This module
does not discover endpoints, authenticate, or emulate a browser session.
"""

from dataclasses import dataclass
from html import unescape
import re
from time import sleep
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.sources.shanghai_sasac import USER_AGENT
from app.services.source_request_budget import RequestBudgetController, is_transport_timeout


@dataclass(frozen=True)
class CampusJsonSource:
    """Configuration for one school's public JSON list and detail requests."""

    name: str
    base_url: str
    list_path: str
    paged_list_path: str
    detail_path: str
    detail_page_path: str = ""

    def endpoint(self, path: str) -> str:
        return urljoin(f"{self.base_url.rstrip('/')}/", path.lstrip("/"))

    def list_endpoint(self, page_num: int, page_size: int) -> str:
        path = self.list_path if page_num == 1 else self.paged_list_path.format(
            page_num=page_num, page_size=page_size
        )
        return self.endpoint(path)

    def detail_endpoint(self, announcement_id: str) -> str:
        return self.endpoint(self.detail_path.format(announcement_id=announcement_id))

    def detail_page_url(self, announcement_id: str) -> str:
        if not self.detail_page_path:
            return ""
        return self.endpoint(self.detail_page_path.format(announcement_id=announcement_id))


SJTU_INTERNSHIP_SOURCE = CampusJsonSource(
    name="上海交通大学就业网实习",
    base_url="https://www.job.sjtu.edu.cn",
    list_path="/career/zpxx/search/sxzpxx",
    paged_list_path="/career/zpxx/search/sxzpxx/{page_num}/{page_size}",
    detail_path="/career/zpxx/data/zpxx/{announcement_id}",
    detail_page_path="/career/zpxx/sxzpxx",
)

SUFE_JOB_SOURCE = CampusJsonSource(
    name="上海财经大学就业网招聘",
    base_url="https://career.sufe.edu.cn",
    list_path="/career/zpxx/search/zpxx",
    paged_list_path="/career/zpxx/search/zpxx/{page_num}/{page_size}",
    detail_path="/career/zpxx/data/zpxx/{announcement_id}",
    detail_page_path="/career/zpxx/zpxx",
)


@dataclass(frozen=True)
class CampusAnnouncement:
    parent_id: str
    title: str
    published_at: str
    detail_url: str


@dataclass(frozen=True)
class CampusJobDetail:
    title: str
    published_at: str
    detail_url: str
    evidence_text: str
    identity_key: str
    announcement_title: str
    employer_name: str
    location_category: str
    location_detail: str
    official_url: str
    recruitment_type: str = "待核验"


_LIST_ID_KEYS = ("zpxxid", "id")
_LIST_TITLE_KEYS = ("zpzt", "title")
_LIST_DATE_KEYS = ("fbrq", "publishedAt", "publishDate")
_LIST_DETAIL_URL_KEYS = ("detailUrl", "detail_url", "zpxxurl", "url")
_POSITION_ID_KEYS = ("zwid", "zwxxid", "positionId", "id")
_POSITION_TITLE_KEYS = ("zwmc", "positionName", "title")
_POSITION_DESCRIPTION_KEYS = ("zwms", "zwxq", "gznr", "positionDescription", "description")
_POSITION_LOCATION_KEYS = ("gzdz", "gzdd", "gzdq", "workLocation", "location", "address")
_EXPLICIT_WORK_LOCATION_PATTERN = re.compile(
    r"(?:工作地点|工作地址|岗位地点)\s*(?:为|是|：|:)?\s*([^。；;\n]+)"
)


def _text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(unescape(str(value)).split())


def _article_text(value: object) -> str:
    return BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)


def _first(row: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = _text(row.get(key))
        if value:
            return value
    return ""


def _data(payload: object) -> dict:
    if not isinstance(payload, dict) or payload.get("code") != 200:
        raise ValueError("校园就业网公开 JSON 响应必须包含 code=200")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("校园就业网公开 JSON 响应缺少对象 data")
    return data


def _list_rows(data: dict) -> list[dict]:
    for key in ("records", "list", "rows", "data"):
        rows = data.get(key)
        if isinstance(rows, list):
            if not all(isinstance(row, dict) for row in rows):
                raise ValueError("校园就业网列表包含非对象记录")
            return rows
    raise ValueError("校园就业网列表响应缺少记录数组")


def parse_campus_announcements(payload: object, source: CampusJsonSource) -> list[CampusAnnouncement]:
    """Parse a public list response without dropping parent announcement facts."""
    announcements: list[CampusAnnouncement] = []
    seen_ids: set[str] = set()
    for row in _list_rows(_data(payload)):
        parent_id = _first(row, _LIST_ID_KEYS)
        title = _first(row, _LIST_TITLE_KEYS)
        published_at = _first(row, _LIST_DATE_KEYS)
        raw_detail_url = _first(row, _LIST_DETAIL_URL_KEYS)
        detail_url = (
            urljoin(f"{source.base_url.rstrip('/')}/", raw_detail_url)
            if raw_detail_url
            else source.detail_page_url(parent_id)
        )
        if not parent_id or not title or not published_at or not detail_url or parent_id in seen_ids:
            continue
        announcements.append(
            CampusAnnouncement(
                parent_id=parent_id,
                title=title,
                published_at=published_at,
                detail_url=detail_url,
            )
        )
        seen_ids.add(parent_id)
    return announcements


def _evidence_lines(data: dict, announcement: CampusAnnouncement, position: dict | None) -> tuple[str, str, str]:
    body = _article_text(data.get("zpxxEditor"))
    if not body:
        raise ValueError("校园就业网详情缺少招聘公告正文")
    employer_name = _first(data, ("dwmc", "companyName", "employerName"))
    unit_address = _text(data.get("xxdz"))
    position_title = _first(position, _POSITION_TITLE_KEYS) if position else ""
    position_description = _first(position, _POSITION_DESCRIPTION_KEYS) if position else ""
    position_location = _first(position, _POSITION_LOCATION_KEYS) if position else ""
    lines = [
        f"招聘公告：{announcement.title}",
        f"发布时间：{announcement.published_at}",
        f"正文：{body}",
    ]
    if employer_name:
        lines.append(f"招聘单位：{employer_name}")
    if unit_address:
        lines.append(f"单位地址：{unit_address}")
    if position_title:
        lines.append(f"岗位名称：{position_title}")
    if position_description:
        lines.append(f"岗位描述：{position_description}")
    if position_location:
        lines.append(f"岗位地点：{position_location}")
    for label, key in (
        ("官方报名入口", "zpxxwz"),
        ("官方招聘链接", "zpurl"),
        ("官方原始链接", "yurl"),
    ):
        value = _text(data.get(key))
        if value:
            lines.append(f"{label}：{value}")
    mail = _text(position.get("zpyx")) if position else ""
    mail = mail or _text(data.get("jltdyx"))
    if mail:
        lines.append(f"官方投递邮箱：{mail}")
    return "\n".join(lines), position_title, position_location


def _location(position_location: str, body: str, has_position: bool) -> tuple[str, str]:
    if position_location:
        return ("明确上海", position_location) if "上海" in position_location else ("其他地区", position_location)
    if has_position:
        return "原文未明确", ""
    match = _EXPLICIT_WORK_LOCATION_PATTERN.search(body)
    if not match:
        return "原文未明确", ""
    explicit_location = match.group(1).strip()
    if "上海" in explicit_location:
        return "明确上海", explicit_location
    if explicit_location:
        return "其他地区", explicit_location
    return "原文未明确", ""


def parse_campus_details(payload: object, announcement: CampusAnnouncement) -> list[CampusJobDetail]:
    """Expand a parent announcement into one detail per explicit position."""
    data = _data(payload)
    positions = data.get("zwxxList")
    if positions is None:
        positions = []
    if not isinstance(positions, list) or not all(isinstance(item, dict) for item in positions):
        raise ValueError("校园就业网详情 zwxxList 结构异常")
    body = _article_text(data.get("zpxxEditor"))
    official_url = _first(data, ("zpxxwz",))
    employer_name = _first(data, ("dwmc", "companyName", "employerName"))
    detail_payloads: list[tuple[str, dict | None]]
    if positions:
        detail_payloads = []
        for position in positions:
            position_id = _first(position, _POSITION_ID_KEYS)
            if not position_id:
                raise ValueError("校园就业网岗位明细缺少 zwxxid")
            detail_payloads.append((f"{announcement.parent_id}:{position_id}", position))
    else:
        detail_payloads = [(announcement.parent_id, None)]
    details: list[CampusJobDetail] = []
    for identity_key, position in detail_payloads:
        evidence_text, position_title, position_location = _evidence_lines(data, announcement, position)
        location_category, location_detail = _location(position_location, body, position is not None)
        details.append(
            CampusJobDetail(
                title=position_title or announcement.title,
                published_at=announcement.published_at,
                detail_url=announcement.detail_url,
                evidence_text=evidence_text,
                identity_key=identity_key,
                announcement_title=announcement.title,
                employer_name=employer_name,
                location_category=location_category,
                location_detail=location_detail,
                official_url=official_url,
            )
        )
    return details


def _post_json(client, url: str, *, timeout: float = 12.0, controller: RequestBudgetController | None = None) -> object:
    for attempt in range(2):
        request_timeout = controller.before_request() if controller else timeout
        try:
            response = client.post(url, data={}, headers={"User-Agent": USER_AGENT}, timeout=request_timeout)
            response.raise_for_status()
            payload = response.json()
            if controller:
                controller.after_request()
            return payload
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = is_transport_timeout(exc) or (
                status_code is not None and status_code >= 500
            )
            if not controller or not retryable or attempt == 1:
                raise
            controller.wait(2)
    raise RuntimeError("unreachable")


def fetch_campus_announcements(
    client,
    source: CampusJsonSource,
    pages: int = 3,
    *,
    timeout: float = 12.0,
    request_interval: float = 0.0,
    controller: RequestBudgetController | None = None,
) -> list[CampusAnnouncement]:
    """Request exactly one to three public list pages, with ten records per page."""
    if not 1 <= pages <= 3:
        raise ValueError("采集页数必须在1到3页之间")
    announcements: list[CampusAnnouncement] = []
    seen_ids: set[str] = set()
    for page in range(1, pages + 1):
        payload = _post_json(
            client,
            source.list_endpoint(page, 10),
            timeout=timeout,
            controller=controller,
        )
        page_announcements = parse_campus_announcements(payload, source)
        new_count = 0
        for announcement in page_announcements:
            if announcement.parent_id not in seen_ids:
                announcements.append(announcement)
                seen_ids.add(announcement.parent_id)
                new_count += 1
        if not page_announcements or new_count == 0:
            break
        if request_interval and controller is None and page < pages:
            sleep(request_interval)
    return announcements


def fetch_campus_details(
    client,
    source: CampusJsonSource,
    announcement: CampusAnnouncement,
    *,
    timeout: float = 12.0,
    controller: RequestBudgetController | None = None,
) -> list[CampusJobDetail]:
    """Request an announcement detail through its configured public POST path."""
    payload = _post_json(
        client,
        source.detail_endpoint(announcement.parent_id),
        timeout=timeout,
        controller=controller,
    )
    return parse_campus_details(payload, announcement)
