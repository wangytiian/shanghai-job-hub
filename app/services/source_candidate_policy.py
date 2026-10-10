"""Deterministic admission rules shared by B-source trials and collection."""

from dataclasses import dataclass
from datetime import date, datetime
import re
from urllib.parse import urlsplit

from app.services.deadline_policy import extract_application_deadline, parse_known_deadline


RULE_VERSION = "source-candidate-v3"
MULTI_POSITION_PARENT_MARKER = "原公告全文（多岗位，仅供人工核对）："


@dataclass(frozen=True)
class SourceCandidate:
    identity_key: str
    announcement_id: str
    job_id: str
    employer_name: str
    title: str
    location_category: str
    location_detail: str
    published_at: str
    deadline: str
    evidence_text: str
    source_url: str
    application_kind: str
    application_value: str
    application_evidence: str
    source_listing_url: str = ""


@dataclass(frozen=True)
class CandidateDecision:
    verdict: str
    reason_codes: tuple[str, ...]


_NON_JOB_PATTERN = re.compile(r"宣讲会|招聘活动|新闻|录用结果|拟录用|面试名单|体检名单")
_STUDENT_PATTERN = re.compile(r"实习|校招|校园招聘|应届|毕业生|在校生|毕业两年内|\d{2,4}届")
_ROLE_STUDENT_TITLE = re.compile(r"实习(?:生|岗)?|应届|管培生|毕业两年内|\d{2,4}届|校招|校园招聘")
_RECRUITER_TITLE = re.compile(
    r"(?:校招|校园招聘|招聘|人力资源|HR|猎头).{0,6}(?:专员|经理|主管|助理|顾问|运营)", re.I
)
_REQUIREMENT_LABEL = re.compile(r"(?:任职要求|招聘对象|招聘要求|岗位要求|资格条件|应聘条件|申请资格)[：:]")
_STUDENT_AUDIENCE = (
    r"(?:\d{2,4}届(?:应届)?(?:毕业生|学生)?|应届(?:毕业生)?|在校生|毕业生|毕业两年内|实习生)"
    r"(?!毕业生|学生|招聘|就业|管理|培养|服务)"
)
_STUDENT_ELIGIBILITY_START = re.compile(rf"^{_STUDENT_AUDIENCE}")
_STUDENT_ELIGIBILITY_VERB = re.compile(
    rf"(?:面向|接受|接收|招收|欢迎|限|开放给|适合|可投递|可申请|可报名)\s*.{{0,12}}{_STUDENT_AUDIENCE}"
)
_SENIOR_PATTERN = re.compile(
    r"(?:[三四五六七八九十3-9]|10)年(?:以上|及以上)|高级职称|副教授|博士后|负责人|总监|专家岗"
)
_ALL_POSITIONS = re.compile(r"(?:所有|全部|各|每个)(?:招聘)?(?:岗位|职位)|本次招聘(?:的)?(?:岗位|职位)")
_BODY_PATTERN = re.compile(r"岗位职责|工作职责|工作内容|职位描述|任职要求|招聘要求|岗位要求|资格条件|\bResponsibilities\b|\bPerson Specification\b", re.I)
_NCSS_BODY_SECTION = re.compile(r"(?:^|\n)职位详情\n([^\n]+)")
_ROLE_DESCRIPTION_LINE = re.compile(r"(?:^|\n)岗位描述：([^\n]+)")
_PLACEHOLDER_DESCRIPTION = re.compile(
    r"^(?:岗位(?:职责|描述|要求)[：:]?\s*)?(?:请|具体内容)?(?:详见|参见|参考|查看|见)(?:原)?(?:招聘公告|公告|官网|附件|岗位说明|职位说明|职位详情|详情)"
)
_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


_EMAIL_LINE = re.compile(r"官方投递邮箱：\s*([^\s，；;,]+)")
_URL_LINE = re.compile(r"(?:官方报名入口|官方投递入口|简历投递入口)：\s*(https?://[^\s]+)")
_GRADUATE_MINIMUM = re.compile(
    r"(?<!或)(?:硕士|博士)(?:研究生)?\s*(?:及以上|以上|起)(?!\s*(?:学历)?\s*(?:者)?优先)"
)
_QUALIFICATION_FIELD = re.compile(r"(?:学历要求|要求学历|最低学历)[：:]\s*([^\n。；;]+)")
_DEGREE_LIST = re.compile(r"(?:博士|硕士|本科|大专|专科)(?:研究生)?(?:\s*[,，、/或]\s*(?:博士|硕士|本科|大专|专科)(?:研究生)?)+")
_GENERATED_PLACEHOLDER = re.compile(r"可随意填|此处填写|TODO|待生成内容", re.I)
_YEAR_COUNT = r"(?:[二两三四五六七八九十2-9]|\d{2,})\s*年"
STUDENT_UNSUITABLE_EXPERIENCE = re.compile(
    rf"(?:{_YEAR_COUNT}(?:及以上|以上|起)?\s*(?:相关|本岗位|专业)?\s*"
    rf"(?:工作|从业|项目|岗位|行业)?经验|"
    rf"(?:工作|从业|项目|岗位|相关)?经验\s*{_YEAR_COUNT}(?:及以上|以上|起))"
    r"(?!\s*(?:者)?(?:优先|加分|更佳))"
)
NCSS_NATIVE_PROOF = "平台原生投递证据：.job .apply > a[href='#']；按钮=投递简历；原始href=#；需登录后提交"


def _requires_graduate_degree(body: str) -> bool:
    if _GRADUATE_MINIMUM.search(body):
        return True
    for match in _QUALIFICATION_FIELD.finditer(body):
        value = match.group(1).strip()
        if _DEGREE_LIST.fullmatch(value) and re.search(r"本科|大专|专科", value):
            continue  # A platform's accepted-degree set is not a minimum degree.
        if re.match(r"(?:硕士|博士)(?!\s*(?:或|优先))", value):
            return True
    return False


def _is_ncss_detail_url(value: str, identity_key: str) -> bool:
    parsed = urlsplit(value)
    return bool(re.fullmatch(r"[A-Za-z0-9]{1,80}", identity_key)
                and parsed.scheme == "https" and parsed.netloc == "www.ncss.cn"
                and parsed.path == f"/student/jobs/{identity_key}/detail.html"
                and not parsed.query and not parsed.fragment)


def _has_verified_native_application(candidate: SourceCandidate) -> bool:
    return bool(candidate.application_kind == "official_platform"
                and _is_ncss_detail_url(candidate.source_url, candidate.identity_key)
                and candidate.application_value == candidate.source_url
                and candidate.application_evidence == NCSS_NATIVE_PROOF
                and NCSS_NATIVE_PROOF in candidate.evidence_text.splitlines())


def policy_evidence_text(evidence_text: str) -> str:
    """Use only role fields and explicit application fields for split positions."""
    scoped, marker, _ = evidence_text.partition(f"\n{MULTI_POSITION_PARENT_MARKER}")
    if not marker:
        return evidence_text
    role_prefixes = (
        "岗位名称：", "岗位描述：", "岗位地点：", "官方报名入口：",
        "官方招聘链接：", "官方原始链接：", "官方投递邮箱：",
    )
    return "\n".join(line for line in scoped.splitlines() if line.startswith(role_prefixes))


def _has_job_specific_body(body: str, *, split_position: bool) -> bool:
    if not split_position:
        if _BODY_PATTERN.search(body):
            return True
        actual_section = _NCSS_BODY_SECTION.search(body)
        if actual_section is None:
            return False
        description = "".join(actual_section.group(1).split())
        return len(description) >= 12 and _PLACEHOLDER_DESCRIPTION.match(description) is None
    match = _ROLE_DESCRIPTION_LINE.search(body)
    if match is None:
        return False
    description = "".join(match.group(1).split())
    return len(description) >= 12 and _PLACEHOLDER_DESCRIPTION.match(description) is None


def _split_position_student_evidence(title: str, body: str) -> bool:
    if _ROLE_STUDENT_TITLE.search(title) and not (
        _RECRUITER_TITLE.search(title) and not re.search(r"实习(?:生|岗)?|应届", title)
    ):
        return True
    description = _ROLE_DESCRIPTION_LINE.search(body)
    if description is None:
        return False
    requirement = _REQUIREMENT_LABEL.search(description.group(1))
    if requirement is None:
        return False
    requirements = description.group(1)[requirement.end():]
    for clause in re.split(r"[。；;，,]", requirements):
        text = clause.strip()
        if _STUDENT_ELIGIBILITY_START.search(text) or _STUDENT_ELIGIBILITY_VERB.search(text):
            return True
    return False


def _global_senior_requirement(evidence_text: str) -> bool:
    _, marker, parent = evidence_text.partition(f"\n{MULTI_POSITION_PARENT_MARKER}")
    if not marker:
        return False
    return any(
        _ALL_POSITIONS.search(sentence) and _SENIOR_PATTERN.search(sentence)
        for sentence in re.split(r"[。；;\n]", parent)
    )


def _url_identity(value: str) -> tuple[str, str, str] | None:
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or "/"


def candidate_from_detail(detail, *, source_listing_url: str = "") -> SourceCandidate:
    """Normalize an adapter detail into the shared candidate contract."""
    identity_key = str(getattr(detail, "identity_key", "") or "").strip()
    announcement_id, separator, job_id = identity_key.partition(":")
    evidence_text = str(getattr(detail, "evidence_text", "") or "")
    scoped_text = policy_evidence_text(evidence_text)
    email_match = _EMAIL_LINE.search(scoped_text)
    official_url = str(getattr(detail, "official_url", "") or "").strip()
    url_match = _URL_LINE.search(scoped_text)
    if email_match:
        application_kind = "email"
        application_value = email_match.group(1).rstrip("。")
        application_evidence = email_match.group(0)
    elif official_url:
        application_kind = "official_url"
        application_value = official_url
        application_evidence = url_match.group(0) if url_match else ""
    elif (getattr(detail, "native_application_verified", False) is True
          and getattr(detail, "native_application_selector", "") == ".job .apply > a[href='#']"
          and getattr(detail, "native_application_href", "") == "#"
          and _is_ncss_detail_url(str(getattr(detail, "detail_url", "")), identity_key)
          and NCSS_NATIVE_PROOF in evidence_text.splitlines()):
        application_kind = "official_platform"
        application_value = str(detail.detail_url)
        application_evidence = NCSS_NATIVE_PROOF
    else:
        application_kind = application_value = application_evidence = ""
    deadline = extract_application_deadline(evidence_text)
    return SourceCandidate(
        identity_key=identity_key,
        announcement_id=announcement_id,
        job_id=job_id if separator else "",
        employer_name=str(getattr(detail, "employer_name", "") or ""),
        title=str(getattr(detail, "title", "") or ""),
        location_category=str(getattr(detail, "location_category", "") or ""),
        location_detail=str(getattr(detail, "location_detail", "") or ""),
        published_at=str(getattr(detail, "published_at", "") or ""),
        deadline=deadline.isoformat() if deadline else "",
        evidence_text=evidence_text,
        source_url=str(getattr(detail, "detail_url", "") or ""),
        application_kind=application_kind,
        application_value=application_value,
        application_evidence=application_evidence,
        source_listing_url=source_listing_url,
    )


def evaluate_candidate(
    candidate: SourceCandidate,
    now: datetime | date | None = None,
    rule_version: str = RULE_VERSION,
) -> CandidateDecision:
    """Return a conservative, explainable decision without network or AI calls."""
    if rule_version != RULE_VERSION:
        raise ValueError(f"不支持的候选规则版本：{rule_version}")
    excluded: list[str] = []
    needs_evidence: list[str] = []
    title = candidate.title.strip()
    split_position = f"\n{MULTI_POSITION_PARENT_MARKER}" in candidate.evidence_text
    body = policy_evidence_text(candidate.evidence_text).strip()
    combined = f"{title}\n{body}"
    has_student_evidence = (
        _split_position_student_evidence(title, body)
        if split_position else _STUDENT_PATTERN.search(combined) is not None
    )

    if _NON_JOB_PATTERN.search(title):
        excluded.append("NOT_OPEN_RECRUITMENT")
    if _requires_graduate_degree(body):
        excluded.append("GRADUATE_DEGREE_REQUIRED")
    if _GENERATED_PLACEHOLDER.search(body):
        excluded.append("PLACEHOLDER_CONTENT")
    if candidate.location_category not in {"明确上海", "上海"} or "上海" not in candidate.location_detail:
        excluded.append("LOCATION_NOT_SHANGHAI")
    if STUDENT_UNSUITABLE_EXPERIENCE.search(combined) or _global_senior_requirement(candidate.evidence_text) or (
        _SENIOR_PATTERN.search(combined) and not has_student_evidence
    ):
        excluded.append("SENIOR_ROLE")

    deadline = parse_known_deadline(candidate.deadline)
    current = now or datetime.now()
    today = current.date() if isinstance(current, datetime) else current
    if deadline is not None and today is not None and deadline < today:
        excluded.append("APPLICATION_EXPIRED")

    if not candidate.identity_key.strip() or not candidate.source_url.strip():
        needs_evidence.append("TRACEABILITY_MISSING")
    if not candidate.employer_name.strip() or not title:
        needs_evidence.append("CORE_FIELDS_MISSING")
    if not body:
        needs_evidence.append("BODY_MISSING")
    elif not _has_job_specific_body(
        body, split_position=split_position,
    ):
        needs_evidence.append("BODY_NOT_JOB_SPECIFIC")
    if not has_student_evidence and "SENIOR_ROLE" not in excluded:
        needs_evidence.append("TARGET_AUDIENCE_UNCLEAR")

    application_value = candidate.application_value.strip()
    application_evidence = candidate.application_evidence.strip()
    if not application_value or not application_evidence:
        needs_evidence.append("APPLICATION_MISSING")
    elif candidate.application_kind == "email":
        if not _EMAIL_PATTERN.fullmatch(application_value) or application_value not in application_evidence:
            needs_evidence.append("APPLICATION_EVIDENCE_INVALID")
    elif candidate.application_kind in {"official_url", "official_platform"}:
        application_identity = _url_identity(application_value)
        source_identities = {
            identity
            for identity in (
                _url_identity(candidate.source_url),
                _url_identity(candidate.source_listing_url),
            )
            if identity is not None
        }
        listing_host = _url_identity(candidate.source_listing_url)
        is_known_listing_path = bool(
            application_identity
            and listing_host
            and application_identity[1] == listing_host[1]
            and any(marker in application_identity[2].lower() for marker in ("/search", "/list", "/zpxx"))
        )
        explicit_application_label = bool(_URL_LINE.search(application_evidence))
        if _has_verified_native_application(candidate):
            pass  # This exact official detail has a parser-verified native submit control.
        elif application_identity in source_identities or is_known_listing_path:
            needs_evidence.append("APPLICATION_IS_SOURCE_PAGE")
        elif application_identity is None or not explicit_application_label:
            needs_evidence.append("APPLICATION_EVIDENCE_INVALID")
        elif application_value not in application_evidence:
            needs_evidence.append("APPLICATION_EVIDENCE_INVALID")
    else:
        needs_evidence.append("APPLICATION_KIND_UNSUPPORTED")

    if excluded:
        return CandidateDecision("excluded", tuple(dict.fromkeys(excluded + needs_evidence)))
    if needs_evidence:
        return CandidateDecision("needs_evidence", tuple(dict.fromkeys(needs_evidence)))
    return CandidateDecision("qualified", ())
