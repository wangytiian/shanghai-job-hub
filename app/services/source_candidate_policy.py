"""Deterministic admission rules shared by B-source trials and collection."""

from dataclasses import dataclass
from datetime import date, datetime
import re
from urllib.parse import urlsplit

from app.services.deadline_policy import extract_application_deadline, parse_known_deadline


RULE_VERSION = "source-candidate-v1"


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
_SENIOR_PATTERN = re.compile(
    r"(?:[三四五六七八九十3-9]|10)年(?:以上|及以上)|高级职称|副教授|博士后|负责人|总监|专家岗"
)
_BODY_PATTERN = re.compile(r"岗位职责|工作职责|工作内容|职位描述|任职要求|招聘要求|岗位要求|资格条件")
_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


_EMAIL_LINE = re.compile(r"官方投递邮箱：\s*([^\s，；;,]+)")
_URL_LINE = re.compile(r"(?:官方报名入口|官方投递入口|简历投递入口)：\s*(https?://[^\s]+)")


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
    email_match = _EMAIL_LINE.search(evidence_text)
    official_url = str(getattr(detail, "official_url", "") or "").strip()
    url_match = _URL_LINE.search(evidence_text)
    if email_match:
        application_kind = "email"
        application_value = email_match.group(1).rstrip("。")
        application_evidence = email_match.group(0)
    elif official_url:
        application_kind = "official_url"
        application_value = official_url
        application_evidence = url_match.group(0) if url_match else ""
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
    body = candidate.evidence_text.strip()
    combined = f"{title}\n{body}"

    if _NON_JOB_PATTERN.search(title):
        excluded.append("NOT_OPEN_RECRUITMENT")
    if candidate.location_category not in {"明确上海", "上海"} or "上海" not in candidate.location_detail:
        excluded.append("LOCATION_NOT_SHANGHAI")
    if _SENIOR_PATTERN.search(combined) and not _STUDENT_PATTERN.search(combined):
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
    elif not _BODY_PATTERN.search(body):
        needs_evidence.append("BODY_NOT_JOB_SPECIFIC")
    if not _STUDENT_PATTERN.search(combined) and "SENIOR_ROLE" not in excluded:
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
        if application_identity in source_identities or is_known_listing_path:
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
