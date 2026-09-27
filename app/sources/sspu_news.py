"""Strict adapter for SSPU's anonymous, public recruitment news endpoints.

Only explicit job sections are expanded. Announcement titles remain provenance,
not evidence of every individual job's graduate year or qualifications.
"""

from hashlib import sha256
from ipaddress import ip_address
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from app.services.source_request_budget import RequestBudgetController
from app.sources.campus_json import CampusAnnouncement, CampusJobDetail, CampusJsonSource, _post_json


SSPU_NEWS_SOURCE = CampusJsonSource(
    name="上海第二工业大学就业网",
    base_url="https://career.sspu.edu.cn",
    list_path="/career/news/search/zpgg",
    paged_list_path="/career/news/search/zpgg/{page_num}/{page_size}",
    detail_path="/career/news/data/zpgg/{announcement_id}",
    detail_page_path="/career/news/view/zpgg/{announcement_id}",
)


class UnsupportedSspuAnnouncement(ValueError):
    """A public external-link notice has no locally verifiable job body."""

    reason_code = "EXTERNAL_LINK_UNSUPPORTED"


class UnsupportedSspuStructure(UnsupportedSspuAnnouncement):
    """Readable public content cannot safely be expanded with supported layouts."""

    def __init__(self, message: str, reason_code: str = "LAYOUT_UNSUPPORTED"):
        super().__init__(message)
        self.reason_code = reason_code


_NUMBERED = re.compile(r"^(\d{1,4})\s*[、.．]\s*(.{1,100})$")
_LABELLED = re.compile(r"^(?:职位名称|岗位名称)\s*[:：]\s*(.{1,100})$")
_HEADCOUNT = re.compile(r"\s*[（(]\s*\d+\s*(?:名|人)\s*[）)]\s*$")
_COMMON_HEADING = re.compile(r"^(?:[一二三四五六七八九十\d]+[、.．]\s*)?(?:福利待遇|实习待遇|薪酬福利|招聘方式及联系方式|投递方式|应聘方式)\s*[:：]?$" )
_DEGREE = re.compile(r"本科|大专|专科|硕士|博士|学历不限|中专|高中")
_PAY = re.compile(r"\d\s*(?:[kKwW万千元]|[~～\-–—至])|面议")
_WORKPLACE = re.compile(r"(?:工作地点|工作地址|岗位地点)\s*[:：]\s*([^。；;\n]+)")
_APPLICATION = re.compile(r"投递|应聘|申请入口|报名入口|招聘链接|招聘邮箱|校招链接|招聘方式及联系方式")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL = re.compile(r"https?://[^\s<>\"'，。；）]+", re.I)
_RAW_URL = re.compile(r"(?:https?|file|javascript|mailto):[^\s<>]+", re.I)
_COMPANY = re.compile(r"([A-Za-z\u4e00-\u9fff（）()·]{2,70}(?:有限责任公司|股份有限公司|有限公司|集团公司))")
_OTHER_AREA = re.compile(r"北京|天津|重庆|河北|山西|辽宁|吉林|黑龙江|江苏|浙江|安徽|福建|江西|山东|河南|湖北|湖南|广东|海南|四川|贵州|云南|陕西|甘肃|青海|台湾|内蒙古|广西|西藏|宁夏|新疆|香港|澳门|青岛|胶州|苏州|杭州|深圳|广州|南京|成都|武汉|长沙|海外|全国|[\u4e00-\u9fff]{2,}(?:市|省|县)")


def _text(value: object) -> str:
    return " ".join(str(value or "").split())


def _data(payload: object) -> dict:
    if not isinstance(payload, dict) or payload.get("code") != 200:
        raise ValueError("二工大公开 JSON 响应必须包含 code=200")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("二工大响应缺少对象 data")
    return data


def _validate(row: dict) -> None:
    if row.get("fbzt") != "已发布" or str(row.get("viewType")) != "10" or row.get("viewTypeName") != "公开查看":
        raise ValueError("二工大公告未明确已发布且公开查看")
    if row.get("releaseMode") not in {"以内容形式发布", "以链接形式发布"}:
        raise ValueError("二工大公告发布模式未知")
    if not re.fullmatch(r"[0-9]+", _text(row.get("newsId"))):
        raise ValueError("二工大公告缺少合法 newsId")


def parse_sspu_announcements(payload: object) -> list[CampusAnnouncement]:
    data = _data(payload)
    rows = data.get("list")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("二工大列表缺少合法 data.list")
    announcements = []
    seen = set()
    for row in rows:
        _validate(row)
        parent_id = _text(row["newsId"])
        title, published_at = _text(row.get("newsTitle")), _text(row.get("releaseDate"))
        if not title or not published_at:
            raise ValueError("二工大公告缺少标题或发布日期")
        if parent_id not in seen:
            announcements.append(CampusAnnouncement(parent_id, title, published_at, SSPU_NEWS_SOURCE.detail_page_url(parent_id)))
            seen.add(parent_id)
    return announcements


def _paragraphs(html: str) -> list[tuple[str, list[tuple[str, str]]]]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "iframe", "object", "embed", "noscript", "template"]):
        tag.decompose()
    # Keep leaf blocks so nested layout wrappers cannot duplicate qualifications.
    blocks = soup.find_all(["p", "h1", "h2", "h3", "h4", "li", "td", "div"])
    paragraphs = []
    for block in blocks:
        if block.find(["p", "h1", "h2", "h3", "h4", "li", "td", "div"]):
            continue
        text = _text(block.get_text(" ", strip=True))
        if text:
            links = [(_text(a.get("href")), _text(a.get_text(" ", strip=True))) for a in block.find_all("a")]
            paragraphs.append((text, links))
    if not paragraphs:
        if soup.find("img"):
            raise UnsupportedSspuStructure("二工大图片公告没有可验证文字岗位，不自动识图", "IMAGE_ONLY_UNSUPPORTED")
        if soup.get_text(" ", strip=True):
            raise UnsupportedSspuStructure("二工大正文缺少支持的岗位分段结构")
        raise ValueError("二工大公告缺少可分段的公开正文")
    return paragraphs


def _pay_location(text: str) -> str | None:
    parts = [part.strip() for part in re.split(r"[/／]", text)]
    if len(parts) >= 3 and _PAY.search(parts[0]) and (not parts[-1] or parts[-1] == "不限" or _DEGREE.search(parts[-1])):
        return "/".join(parts[1:-1])
    return None


def _public_url(value: str) -> str:
    value = value.strip().rstrip("。；，")
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not host:
            return ""
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return ""
        try:
            if not ip_address(host).is_global:
                return ""
        except ValueError:
            # Reject local names and alternative numeric forms such as 127.1.
            if "." not in host or not re.search(r"\.[a-z]{2,}$", host):
                return ""
            if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home", ".intranet")):
                return ""
        if any(char.isspace() for char in value) or "\\" in value:
            return ""
        return value
    except ValueError:
        return ""


def _applications(paragraphs: list) -> tuple[list[str], list[str]]:
    urls, emails = [], []
    previous_label = False
    application_context = False
    for text, links in paragraphs:
        labelled = bool(_APPLICATION.search(text))
        contact_in_recruitment = application_context and bool(re.match(r"^联系邮箱\s*[:：]", text))
        if labelled or previous_label or contact_in_recruitment:
            emails.extend(_EMAIL.findall(text))
            for href, visible in links:
                # A visibly printed HTTP address may repair a broken Word file href.
                visible_urls = _URL.findall(visible)
                candidate = next((u for u in map(_public_url, visible_urls) if u), "") or _public_url(href)
                if candidate:
                    urls.append(candidate)
            urls.extend(u for u in map(_public_url, _URL.findall(text)) if u)
        previous_label = labelled and not links and not _URL.search(text) and not _EMAIL.search(text)
        application_context = application_context or labelled
    return list(dict.fromkeys(urls)), list(dict.fromkeys(emails))


def _evidence_line(text: str) -> str:
    # Emit application URLs/emails only through the explicit-label validation path.
    return _text(_EMAIL.sub("", _RAW_URL.sub("", text)))


def _employer(data: dict, prefix: list, announcement: CampusAnnouncement) -> str:
    for key in ("employerName", "companyName", "dwmc"):
        value = _text(data.get(key))
        if value and value != _text(data.get("orgName")):
            return value
    for text, _ in prefix:
        if re.match(r"^(?:官网|招聘单位|单位名称|公司名称)\s*[:：]?", text):
            text = re.sub(r"^(?:官网|招聘单位|单位名称|公司名称)\s*[:：]?\s*", "", text)
            match = _COMPANY.search(text)
            if match:
                return match.group(1)
    match = _COMPANY.search(announcement.title)
    return match.group(1) if match else ""


def _location(own_text: str, metadata_location: str | None, common_text: str) -> tuple[str, str]:
    own_match = _WORKPLACE.search(own_text)
    common_match = _WORKPLACE.search(common_text)
    detail = (own_match.group(1).strip() if own_match else metadata_location) or (common_match.group(1).strip() if common_match else "")
    workplace = re.sub(r"上海\s*[（(][^）)]*(?:培训|培养|面试|总部)[^）)]*[）)]", "", detail)
    workplace = re.sub(r"[（(][^）)]*上海[^）)]*(?:培训|培养|面试|总部)[^）)]*[）)]", "", workplace)
    # A city mentioned solely for training, interviews, or company headquarters
    # does not establish where the position will actually be based.
    workplace = "，".join(part for part in re.split(r"[，,、/；;]", workplace)
                         if not ("上海" in part and re.search(r"培训|培养|面试|总部", part)))
    if "上海" in workplace:
        return "明确上海", detail
    if _OTHER_AREA.search(workplace):
        return "其他地区", detail
    return "原文未明确", detail


def _common_constraints(prefix: list) -> list[str]:
    constraints = []
    in_section = False
    header = re.compile(r"^(?:本次|统一)?(?:招聘对象|应聘对象|学历要求|应聘要求|报名条件|资格要求|招聘范围|报名截止(?:时间)?|投递截止(?:时间)?|申请截止(?:时间)?|截止日期|截止时间)\s*[:：]?")
    exclusive = re.compile(r"(?:仅限|只限|只接受|仅接受|只招|仅招|仅面向|限).{0,45}(?:学生|毕业生|应届|本校|二工大|硕士|博士|研究生|本科)")
    deadline = re.compile(r"(?:报名|投递|申请).{0,10}(?:截止|截至)|截止(?:日期|时间)")
    for text, _ in prefix:
        labelled = header.search(text)
        if labelled or exclusive.search(text) or deadline.search(text):
            constraints.append(_evidence_line(text))
            in_section = bool(labelled)
        elif in_section and re.search(r"硕士|博士|本科|专科|研究生|毕业生|应届|本校|二工大|20\d{2}[年./-]", text):
            constraints.append(_evidence_line(text))
        else:
            in_section = False
    return constraints


def parse_sspu_details(payload: object, announcement: CampusAnnouncement) -> list[CampusJobDetail]:
    data = _data(payload)
    _validate(data)
    if _text(data["newsId"]) != announcement.parent_id:
        raise ValueError("二工大详情 newsId 与请求公告不一致")
    if data["releaseMode"] == "以链接形式发布":
        raise UnsupportedSspuAnnouncement("二工大外链型公告未提供可验证正文，不自动追踪外站")
    paragraphs = _paragraphs(str(data.get("newsContent") or ""))
    boundaries = []
    tail_start = len(paragraphs)
    for index, (text, _) in enumerate(paragraphs):
        if boundaries and _COMMON_HEADING.fullmatch(text):
            tail_start = index
            break
        labelled = _LABELLED.fullmatch(text)
        numbered = _NUMBERED.fullmatch(text)
        metadata_location = _pay_location(paragraphs[index + 1][0]) if index + 1 < len(paragraphs) else None
        if labelled:
            title = _HEADCOUNT.sub("", labelled.group(1)).strip()
            key = "title-" + sha256(re.sub(r"\s+", "", title).encode("utf-8")).hexdigest()[:20]
            boundaries.append((index, title, key, None))
        elif numbered and metadata_location is not None:
            title = _HEADCOUNT.sub("", numbered.group(2)).strip()
            boundaries.append((index, title, str(int(numbered.group(1))), metadata_location))
        elif numbered and _HEADCOUNT.search(text):
            raise ValueError("二工大疑似岗位标题无法可靠分段，需人工确认")
    if not boundaries:
        raise UnsupportedSspuStructure("二工大公告未识别到支持的独立岗位分段")
    prefix = paragraphs[:boundaries[0][0]]
    # Keep explicit access/audience restrictions; never infer them from a parent title.
    restrictions = _common_constraints(prefix)
    common = paragraphs[tail_start:]
    common_text = "\n".join(_evidence_line(text) for text, _ in common)
    employer_name = _employer(data, prefix, announcement)
    details, seen = [], set()
    for offset, (start, title, key, metadata_location) in enumerate(boundaries):
        end = boundaries[offset + 1][0] if offset + 1 < len(boundaries) else tail_start
        own = paragraphs[start:end]
        if len(own) < 2 or not any(len(text) >= 6 and not re.fullmatch(r"(?:岗位职责|任职要求|岗位要求)\s*[:：]?", text) for text, _ in own[1:]):
            raise ValueError("二工大岗位分段缺少具体职责或资格事实")
        if key in seen:
            raise ValueError("二工大公告岗位身份重复，需人工确认")
        seen.add(key)
        own_text = "\n".join(_evidence_line(text) for text, _ in own)
        urls, emails = _applications(prefix + own + common)
        evidence = [own_text]
        evidence.extend(restrictions)
        if common_text:
            evidence.append(common_text)
        evidence.extend(f"官方报名入口：{url}" for url in urls)
        evidence.extend(f"官方投递邮箱：{email}" for email in emails)
        location_category, location_detail = _location(own_text, metadata_location, common_text)
        details.append(CampusJobDetail(
            title=title, published_at=announcement.published_at,
            detail_url=announcement.detail_url, evidence_text="\n".join(evidence),
            identity_key=f"{announcement.parent_id}:{key}", announcement_title=announcement.title,
            employer_name=employer_name, location_category=location_category,
            location_detail=location_detail, official_url=urls[0] if urls else "",
            recruitment_type="实习" if "实习" in title or "实习待遇" in common_text else "待核验",
        ))
    return details


def _budget(controller: RequestBudgetController | None) -> RequestBudgetController:
    return controller if controller is not None else RequestBudgetController(total_seconds=180, request_timeout_seconds=12, interval_seconds=1)


def fetch_sspu_announcements(client, pages: int = 3, *, controller=None) -> list[CampusAnnouncement]:
    if not isinstance(pages, int) or isinstance(pages, bool) or not 1 <= pages <= 3:
        raise ValueError("二工大采集页数必须在1到3页之间")
    controller = _budget(controller)
    announcements, seen = [], set()
    for page in range(1, pages + 1):
        payload = _post_json(client, SSPU_NEWS_SOURCE.list_endpoint(page, 10), controller=controller)
        page_announcements = parse_sspu_announcements(payload)[:10]
        new = [a for a in page_announcements if a.parent_id not in seen]
        announcements.extend(new)
        seen.update(a.parent_id for a in new)
        data = _data(payload)
        total = int(str(data["total"])) if str(data.get("total", "")).isdigit() else None
        page_count = int(str(data["pages"])) if str(data.get("pages", "")).isdigit() else None
        if not new or data.get("isLastPage") is True or data.get("hasNextPage") is False or (page_count is not None and page >= page_count) or (total is not None and page * 10 >= total):
            break
    return announcements


def fetch_sspu_details(client, announcement: CampusAnnouncement, *, controller=None) -> list[CampusJobDetail]:
    if not re.fullmatch(r"[0-9]+", announcement.parent_id):
        raise ValueError("二工大公告 ID 非法")
    payload = _post_json(client, SSPU_NEWS_SOURCE.detail_endpoint(announcement.parent_id), controller=_budget(controller))
    return parse_sspu_details(payload, announcement)
