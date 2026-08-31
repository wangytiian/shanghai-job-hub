from dataclasses import dataclass
from datetime import date
import re
import time

from app.sources.shanghai_sasac import USER_AGENT


BASE_URL = "https://job.spdb.com.cn/"
SOCIAL_LIST_URL = f"{BASE_URL}socialJobJsonList"
SENIOR_TITLE_PATTERN = re.compile(r"处级|负责人|总经理|副总经理|支行行长|支行副行长|团队管理")
GRADUATE_SIGNAL_PATTERN = re.compile(r"应届(?:毕业生)?|毕业生|校招|校园招聘|20(?:24|25|26)届")
MATURE_EXPERIENCE_PATTERN = re.compile(
    r"(?:3|三|4|四|5|五|6|六|7|七|8|八|9|九|10|十)\s*年(?:以上|及以上).{0,24}经验|"
    r"(?:工作|从业|相关|行业|岗位)经验.{0,24}(?:3|三|4|四|5|五|6|六|7|七|8|八|9|九|10|十)\s*年(?:以上|及以上)"
)


@dataclass(frozen=True)
class SpdbListing:
    job_id: str
    title: str
    department: str
    location: str
    published_at: str
    deadline: str
    detail_url: str


@dataclass(frozen=True)
class SpdbDetail:
    title: str
    published_at: str
    deadline: str
    detail_url: str
    official_url: str
    identity_key: str
    employer_name: str
    location_detail: str
    location_category: str
    recruitment_type: str
    evidence_text: str


@dataclass(frozen=True)
class SpdbFetchResult:
    details: list[SpdbDetail]
    filtered_count: int
    filter_reasons: list[str]


def is_spdb_student_fit(requirement: str) -> tuple[bool, str]:
    text = requirement.strip()
    if GRADUATE_SIGNAL_PATTERN.search(text):
        return True, "含应届生开放信号"
    if MATURE_EXPERIENCE_PATTERN.search(text):
        return False, "明确三年及以上经验且未见应届生开放信号"
    return True, "经验要求未触发预筛"


def parse_spdb_shanghai_listings(payload: dict, today: date | None = None) -> list[SpdbListing]:
    current_day = today or date.today()
    listings: list[SpdbListing] = []
    seen_ids: set[str] = set()
    for row in payload.get("rows", []):
        job_id = str(row.get("openningJobId", "")).strip()
        title = str(row.get("positionName", "")).strip()
        location = str(row.get("prmLocArea") or row.get("address") or "").strip()
        published_at = str(row.get("desiredStartDt", "")).strip()
        deadline = str(row.get("closeDt", "")).strip()
        try:
            is_open = date.fromisoformat(deadline) >= current_day
        except ValueError:
            is_open = False
        if (
            not job_id
            or not title
            or job_id in seen_ids
            or location != "上海"
            or not is_open
            or SENIOR_TITLE_PATTERN.search(title)
        ):
            continue
        listings.append(
            SpdbListing(
                job_id=job_id,
                title=title,
                department=str(row.get("deptDescr", "")).strip() or "官方页面未明确部门",
                location=location,
                published_at=published_at or deadline,
                deadline=deadline,
                detail_url=f"{BASE_URL}jobDetail?jobId={job_id}&type=1",
            )
        )
        seen_ids.add(job_id)
    return listings


def _post_json(client, url: str, data: dict) -> dict:
    response = client.post(
        url,
        data=data,
        headers={"User-Agent": USER_AGENT},
        timeout=12.0,
    )
    response.raise_for_status()
    return response.json()


def _get_json(client, url: str) -> dict:
    response = client.get(url, headers={"User-Agent": USER_AGENT}, timeout=12.0)
    response.raise_for_status()
    return response.json()


def fetch_spdb_shanghai_job_details(
    client, limit: int = 20, today: date | None = None
) -> SpdbFetchResult:
    if not 1 <= limit <= 30:
        raise ValueError("采集数量必须在1到30条之间")
    payload = _post_json(
        client,
        SOCIAL_LIST_URL,
        {"pageNo": "1", "pageSize": str(limit), "deptDescr": "", "address": "上海", "jobNature": "", "jobKey": ""},
    )
    details: list[SpdbDetail] = []
    filter_reasons: list[str] = []
    for listing in parse_spdb_shanghai_listings(payload, today=today)[:limit]:
        detail_payload = _get_json(client, f"{BASE_URL}jobDetailJSON?jobId={listing.job_id}&type=1")
        job = detail_payload.get("cgOpenningJob") or {}
        duty = str(job.get("hrsJobDuty", "")).strip()
        requirement = str(job.get("hrsJobRequire", "")).strip()
        is_student_fit, filter_reason = is_spdb_student_fit(requirement)
        if not is_student_fit:
            filter_reasons.append(filter_reason)
            continue
        evidence_text = "\n".join(
            item
            for item in (
                "上海浦东发展银行官方招聘岗位",
                f"岗位名称：{listing.title}",
                f"所属部门：{listing.department}",
                f"工作地点：{listing.location}",
                f"发布时间：{listing.published_at}",
                f"截止日期：{listing.deadline}",
                f"岗位职责：{duty}" if duty else "",
                f"应聘条件：{requirement}" if requirement else "",
            )
            if item
        )
        details.append(
            SpdbDetail(
                title=str(job.get("positionName") or listing.title).strip(),
                published_at=str(job.get("desiredStartDt") or listing.published_at).strip(),
                deadline=str(job.get("closeDt") or listing.deadline).strip(),
                detail_url=listing.detail_url,
                official_url=listing.detail_url,
                identity_key=listing.job_id,
                employer_name="待人工核验（上海浦东发展银行）",
                location_detail=listing.location,
                location_category="明确上海",
                recruitment_type="社会招聘",
                evidence_text=evidence_text[:5000],
            )
        )
        time.sleep(0.3)
    return SpdbFetchResult(details, len(filter_reasons), filter_reasons)
