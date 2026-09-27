from dataclasses import dataclass
from collections import Counter
from datetime import datetime
from hashlib import sha256
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, Source, SourceDiagnostic, TaskRun
from app.services.intake_screening import screen_intake, screen_intake_with_ai
from app.services.deadline_policy import extract_application_deadline, mark_job_expired
from app.services.collection_strategy import build_collection_plan
from app.services.source_library import can_auto_collect
from app.services.source_candidate_policy import CandidateDecision, candidate_from_detail, evaluate_candidate
from app.services.review_intake import REVIEW_NOTE, evaluate_review_intake
from app.services.source_request_budget import RequestBudgetController
from app.sources.catalog import ensure_official_source_catalog
from app.sources.official_list import fetch_official_detail, fetch_official_listings
from app.sources.shanghai_sasac import (
    LISTING_URL,
    fetch_shanghai_sasac_detail,
    fetch_shanghai_sasac_listings,
)
from app.sources.spdb import fetch_spdb_shanghai_job_details
from app.sources.boc import fetch_boc_detail, fetch_boc_listings
from app.sources.ncss import fetch_ncss_detail, fetch_ncss_shanghai_listings
from app.sources.campus_json import (
    SJTU_INTERNSHIP_SOURCE,
    SUFE_JOB_SOURCE,
    fetch_campus_announcements,
    fetch_campus_details,
)
from app.sources.sbs_jobs import fetch_sbs_detail, fetch_sbs_listings
from app.sources.sspu_news import (
    UnsupportedSspuAnnouncement,
    fetch_sspu_announcements,
    fetch_sspu_details,
)


REAL_SOURCE_NAME = "上海市国资委国企招聘（真实公开来源）"
SPDB_SOURCE_NAME = "上海浦东发展银行官方招聘"
BOC_SOURCE_NAME = "中国银行官方招聘"
NCSS_SOURCE_NAME = "国家大学生就业服务平台上海岗位"
SJTU_SOURCE_NAME = "上海交通大学就业网（待专用适配）"
SUFE_SOURCE_NAME = "上海财经大学就业网（待专用适配）"
SBS_SOURCE_NAME = "上海商学院就业网（待专用适配）"
SSPU_SOURCE_NAME = "上海第二工业大学就业网"
REAL_RISK_FLAG = "真实线索：尚未人工核验，不得对外发布"


@dataclass(frozen=True)
class RealCollectionResult:
    created_jobs: int
    updated_jobs: int
    failed_jobs: int
    unchanged_jobs: int = 0
    source_status: str = ""


@dataclass(frozen=True)
class DailyCollectionResult:
    attempted_sources: int
    successful_sources: int
    skipped_sources: int
    created_jobs: int
    updated_jobs: int
    unchanged_jobs: int
    failed_jobs: int


def _ensure_source(session: Session, now: datetime) -> Source:
    source = session.scalar(select(Source).where(Source.name == REAL_SOURCE_NAME))
    if source is None:
        source = Source(
            name=REAL_SOURCE_NAME,
            url=LISTING_URL,
            level="一级",
            source_type="公共平台",
            status="正常",
            check_frequency_hours=4,
        )
        session.add(source)
        session.flush()
    source.last_checked_at = now
    return source


def _content_fingerprint(evidence_text: str) -> str:
    normalized = " ".join(evidence_text.split())
    return sha256(normalized.encode("utf-8")).hexdigest()


def _parse_published_at(value: object) -> datetime | None:
    """Store an explicit source publication date separately from collection time."""
    normalized = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    return None


def _attachment_links(detail) -> str:
    return json.dumps(
        [{"name": item.name, "url": item.url} for item in getattr(detail, "attachments", ())],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _evidence_status(detail) -> tuple[str, str]:
    attachments = tuple(getattr(detail, "attachments", ()) or ())
    if attachments:
        return "正文已提取", f"发现 {len(attachments)} 个公开附件入口，需按附件类型单独核验。"
    return "正文已提取", ""


def _record_source_success(source: Source, now: datetime) -> None:
    plan = build_collection_plan(source.level, now, now)
    source.status = "正常"
    source.last_success_at = now
    source.next_due_at = plan.next_due_at
    source.consecutive_failure_count = 0
    source.last_error_summary = ""
    source.pause_reason = ""


def _record_source_failure(source: Source, exc: Exception) -> None:
    source.consecutive_failure_count += 1
    source.last_error_summary = str(exc)[:300]
    if source.consecutive_failure_count >= 3:
        source.status = "暂停"
        source.pause_reason = "连续 3 次采集失败，等待人工恢复"
    else:
        source.status = "异常"


def _save_detail(
    session: Session,
    source: Source,
    detail,
    collected_now: datetime,
    intake_ai_complete=None,
    *, review_note: str = "", fingerprint_context: str = "",
) -> str:
    identity_key = str(getattr(detail, "identity_key", "") or "").strip()
    if not identity_key:
        identity_key = str(getattr(detail, "detail_url", "") or "").rstrip("/")
    if not identity_key:
        identity_key = str(getattr(detail, "title", "") or "").strip()
    fingerprint = f"{source.name}|{identity_key}"
    job = session.scalar(select(Job).where(Job.fingerprint == fingerprint))
    extracted_deadline = extract_application_deadline(detail.evidence_text)
    if extracted_deadline is not None and extracted_deadline < collected_now.date():
        if job is not None:
            mark_job_expired(job, extracted_deadline)
        return "expired"
    attachment_links = _attachment_links(detail)
    evidence_status, evidence_note = _evidence_status(detail)
    if review_note:
        evidence_note = " ".join(filter(None, (evidence_note, review_note)))
    content_material = f"{detail.evidence_text}\n{attachment_links}"
    if fingerprint_context:
        content_material += "\n" + fingerprint_context
    content_fingerprint = _content_fingerprint(content_material)
    if job is None:
        intake = (
            screen_intake_with_ai(detail.title, detail.evidence_text, intake_ai_complete)
            if intake_ai_complete is not None and not review_note
            else screen_intake(detail.title, detail.evidence_text)
        )
        session.add(
            Job(
                fingerprint=fingerprint,
                employer_name=getattr(detail, "employer_name", f"待人工核验（{source.name}）"),
                announcement_title=detail.title,
                job_title=detail.title,
                job_family="待分类",
                recruitment_type=getattr(detail, "recruitment_type", "待核验"),
                location_category=getattr(detail, "location_category", "原文未明确"),
                location_detail=getattr(detail, "location_detail", ""),
                target_audience="待人工判断",
                direction_tags="待人工分类",
                deadline=getattr(detail, "deadline", "") or (
                    extracted_deadline.isoformat() if extracted_deadline else "原文待人工确认"
                ),
                official_url=getattr(detail, "official_url", ""),
                source_url=detail.detail_url,
                evidence_text=detail.evidence_text,
                evidence_status=evidence_status,
                evidence_note=evidence_note,
                attachment_links=attachment_links,
                quality_score=0,
                risk_flags=REAL_RISK_FLAG,
                is_demo=False,
                published_at=_parse_published_at(getattr(detail, "published_at", "")),
                collected_at=collected_now,
                content_fingerprint=content_fingerprint,
                last_verified_at=collected_now,
                lifecycle_status="正常",
                last_change_summary="",
                status="待核验",
                intake_grade="C" if review_note else intake.grade,
                intake_route="人工复核" if review_note else intake.route,
                intake_reason=review_note or intake.reason,
                intake_evidence=intake.evidence,
                intake_confidence=intake.confidence,
            )
        )
        return "created"
    if job.content_fingerprint == content_fingerprint:
        job.last_verified_at = collected_now
        return "unchanged"
    job.evidence_text = detail.evidence_text
    job.announcement_title = getattr(detail, "title", "") or job.announcement_title
    job.evidence_status = evidence_status
    job.evidence_note = evidence_note
    job.employer_name = getattr(detail, "employer_name", job.employer_name)
    job.official_url = getattr(detail, "official_url", job.official_url)
    job.attachment_links = attachment_links
    job.last_verified_at = collected_now
    job.published_at = _parse_published_at(getattr(detail, "published_at", "")) or job.published_at
    job.content_fingerprint = content_fingerprint
    job.lifecycle_status = "有更新"
    job.last_change_summary = "原文内容发生变化，待人工确认"
    job.status = "待核验"
    job.risk_flags = REAL_RISK_FLAG
    job.verification_checks = "{}"
    job.verification_version = 0
    if review_note:
        job.intake_grade = "C"
        job.intake_route = "人工复核"
        job.intake_reason = review_note
        job.intake_confidence = "低"
    job.version += 1
    return "updated"


def collect_official_list_source(
    session: Session, client, source: Source, limit: int = 12, now: datetime | None = None,
    intake_ai_complete=None,
) -> RealCollectionResult:
    collected_now = now or datetime.now()
    source.last_checked_at = collected_now
    created_jobs = updated_jobs = failed_jobs = unchanged_jobs = 0
    try:
        listings = fetch_official_listings(client, source.url, limit=limit)
        if not listings:
            raise ValueError("来源列表未发现带日期的招聘公告，未将其视为没有新招聘")
        for listing in listings:
            try:
                outcome = _save_detail(
                    session, source, fetch_official_detail(client, listing), collected_now, intake_ai_complete
                )
                if outcome == "created":
                    created_jobs += 1
                elif outcome == "updated":
                    updated_jobs += 1
                else:
                    unchanged_jobs += 1
            except Exception:
                failed_jobs += 1
        if failed_jobs == len(listings):
            raise ValueError("来源列表可读，但全部详情解析失败")
        _record_source_success(source, collected_now)
        session.add(TaskRun(task_name=f"公开采集·{source.name}", status="完成", message=f"新增 {created_jobs} 条，无变化 {unchanged_jobs} 条，有更新 {updated_jobs} 条，单条失败 {failed_jobs} 条。"))
        session.commit()
        return RealCollectionResult(created_jobs, updated_jobs, failed_jobs, unchanged_jobs, source.status)
    except Exception as exc:
        _record_source_failure(source, exc)
        session.add(TaskRun(task_name=f"公开采集·{source.name}", status="失败", message=f"采集失败：{str(exc)[:300]}"))
        session.commit()
        raise


def collect_due_sources(
    session: Session, client, now: datetime | None = None, force: bool = False, intake_ai_complete=None,
) -> DailyCollectionResult:
    collected_now = now or datetime.now()
    ensure_official_source_catalog(session)
    enabled_sources = session.scalars(
        select(Source).where(Source.is_enabled.is_(True)).order_by(Source.id)
    ).all()
    sources = [source for source in enabled_sources if can_auto_collect(source)]
    attempted = successful = skipped = created = updated = unchanged = failed = 0
    for source in sources:
        plan = build_collection_plan(source.level, collected_now, source.last_success_at)
        if source.status == "暂停" or (not force and not plan.is_due):
            skipped += 1
            continue
        attempted += 1
        try:
            if source.adapter_key == "shanghai_sasac":
                result = collect_shanghai_sasac(
                    session, client, now=collected_now, intake_ai_complete=intake_ai_complete
                )
            elif source.adapter_key == "official_dated_list":
                result = collect_official_list_source(
                    session, client, source, now=collected_now, intake_ai_complete=intake_ai_complete
                )
            elif source.adapter_key == "spdb_shanghai_jobs":
                result = collect_spdb_shanghai_jobs(
                    session, client, now=collected_now, intake_ai_complete=intake_ai_complete
                )
            elif source.adapter_key == "boc_announcements":
                result = collect_boc_announcements(
                    session, client, now=collected_now, intake_ai_complete=intake_ai_complete
                )
            elif source.adapter_key == "ncss_shanghai_jobs":
                result = collect_ncss_shanghai_jobs(
                    session, client, now=collected_now, intake_ai_complete=intake_ai_complete
                )
            elif source.adapter_key == "sjtu_internship_json":
                result = collect_sjtu_internship_jobs(
                    session, client, now=collected_now, intake_ai_complete=intake_ai_complete
                )
            elif source.adapter_key == "sufe_job_json":
                result = collect_sufe_job_announcements(
                    session, client, now=collected_now, intake_ai_complete=intake_ai_complete
                )
            elif source.adapter_key == "sbs_jobs":
                result = collect_sbs_jobs(session, client, now=collected_now, intake_ai_complete=intake_ai_complete)
            elif source.adapter_key == "sspu_news":
                result = collect_sspu_jobs(session, client, now=collected_now, intake_ai_complete=intake_ai_complete)
            else:
                skipped += 1
                continue
            successful += 1
            created += result.created_jobs
            updated += result.updated_jobs
            unchanged += result.unchanged_jobs
            failed += result.failed_jobs
        except Exception:
            failed += 1
    task_status = "无到期任务" if not attempted else "失败" if not successful else "部分完成" if failed or successful < attempted else "完成"
    session.add(TaskRun(task_name="每日多来源采集", status=task_status, message=f"尝试 {attempted} 个来源，成功 {successful} 个，跳过 {skipped} 个；新增 {created} 条，无变化 {unchanged} 条，有更新 {updated} 条，失败 {failed} 项。"))
    session.commit()
    return DailyCollectionResult(attempted, successful, skipped, created, updated, unchanged, failed)


def collect_boc_announcements(
    session: Session, client, limit: int = 12, now: datetime | None = None, intake_ai_complete=None,
) -> RealCollectionResult:
    """Collect public Bank of China announcements as unverified national notices."""
    collected_now = now or datetime.now()
    ensure_official_source_catalog(session)
    source = session.scalar(select(Source).where(Source.name == BOC_SOURCE_NAME))
    if source is None:
        raise ValueError("中国银行官方招聘来源未配置")
    source.last_checked_at = collected_now
    created_jobs = updated_jobs = failed_jobs = unchanged_jobs = 0
    try:
        listings = fetch_boc_listings(client, limit=limit)
        if not listings:
            raise ValueError("中国银行招聘公告列表未发现带日期的招聘信息，未将其视为没有新招聘")
        for listing in listings:
            try:
                outcome = _save_detail(
                    session, source, fetch_boc_detail(client, listing), collected_now, intake_ai_complete
                )
                if outcome == "created":
                    created_jobs += 1
                elif outcome == "updated":
                    updated_jobs += 1
                else:
                    unchanged_jobs += 1
            except Exception:
                failed_jobs += 1
        if failed_jobs == len(listings):
            raise ValueError("来源列表可读，但全部详情解析失败")
        _record_source_success(source, collected_now)
        session.add(TaskRun(task_name=f"公开采集·{source.name}", status="完成", message=f"新增 {created_jobs} 条，无变化 {unchanged_jobs} 条，有更新 {updated_jobs} 条，单条失败 {failed_jobs} 条。"))
        session.commit()
        return RealCollectionResult(created_jobs, updated_jobs, failed_jobs, unchanged_jobs, source.status)
    except Exception as exc:
        _record_source_failure(source, exc)
        session.add(TaskRun(task_name=f"公开采集·{source.name}", status="失败", message=f"采集失败：{str(exc)[:300]}"))
        session.commit()
        raise


def collect_ncss_shanghai_jobs(
    session: Session, client, limit_pages: int = 3, now: datetime | None = None, intake_ai_complete=None,
) -> RealCollectionResult:
    """Collect publicly listed Shanghai NCSS jobs into the unverified review queue."""
    collected_now = now or datetime.now()
    ensure_official_source_catalog(session)
    source = session.scalar(select(Source).where(Source.name == NCSS_SOURCE_NAME))
    if source is None:
        raise ValueError("国家大学生就业服务平台上海岗位来源未配置")
    source.last_checked_at = collected_now
    created_jobs = updated_jobs = failed_jobs = unchanged_jobs = 0
    successful_details = 0
    try:
        listings = fetch_ncss_shanghai_listings(client, pages=limit_pages)
        if not listings:
            raise ValueError("国家大学生就业服务平台上海列表为空，未将其视为没有新招聘")
        for listing in listings:
            try:
                detail = fetch_ncss_detail(client, listing)
                successful_details += 1
                decision = evaluate_candidate(
                    candidate_from_detail(detail, source_listing_url=source.url),
                    now=collected_now,
                )
                if decision.verdict != "qualified":
                    failed_jobs += 1
                    continue
                outcome = _save_detail(session, source, detail, collected_now, intake_ai_complete)
                if outcome == "created":
                    created_jobs += 1
                elif outcome == "updated":
                    updated_jobs += 1
                else:
                    unchanged_jobs += 1
            except Exception:
                failed_jobs += 1
        if successful_details == 0:
            raise ValueError("来源列表可读，但全部详情解析失败")
        _record_source_success(source, collected_now)
        source.last_monitor_summary = (
            f"国家大学生就业服务平台上海采集：新增 {created_jobs} 条，无变化 {unchanged_jobs} 条，"
            f"有更新 {updated_jobs} 条，单条跳过或失败 {failed_jobs} 条。"
        )
        session.add(
            TaskRun(
                task_name="公开采集·国家大学生就业服务平台上海岗位",
                status="完成",
                message=f"新增 {created_jobs} 条，无变化 {unchanged_jobs} 条，有更新 {updated_jobs} 条，单条跳过或失败 {failed_jobs} 条。",
            )
        )
        session.commit()
        return RealCollectionResult(created_jobs, updated_jobs, failed_jobs, unchanged_jobs, source.status)
    except Exception as exc:
        _record_source_failure(source, exc)
        session.add(
            TaskRun(
                task_name="公开采集·国家大学生就业服务平台上海岗位",
                status="失败",
                message=f"采集失败：{str(exc)[:300]}",
            )
        )
        session.commit()
        raise


def _collect_campus_json_source(
    session: Session,
    client,
    *,
    source_name: str,
    campus_source,
    now: datetime | None = None,
    intake_ai_complete=None,
) -> RealCollectionResult:
    """Collect explicitly Shanghai campus positions into the existing review queue."""
    collected_now = now or datetime.now()
    ensure_official_source_catalog(session)
    source = session.scalar(select(Source).where(Source.name == source_name))
    if source is None:
        raise ValueError(f"校园就业来源未配置：{source_name}")
    source.last_checked_at = collected_now
    created_jobs = updated_jobs = failed_jobs = unchanged_jobs = non_shanghai_jobs = 0
    successful_details = 0
    try:
        announcements = fetch_campus_announcements(client, campus_source, pages=3)
        if not announcements:
            raise ValueError("校园就业网公开列表为空，未将其视为没有新招聘")
        for announcement in announcements:
            try:
                details = fetch_campus_details(client, campus_source, announcement)
                if not details:
                    raise ValueError("校园就业网详情未返回岗位")
                successful_details += 1
                for detail in details:
                    decision = evaluate_candidate(
                        candidate_from_detail(detail, source_listing_url=source.url),
                        now=collected_now,
                    )
                    if "LOCATION_NOT_SHANGHAI" in decision.reason_codes:
                        non_shanghai_jobs += 1
                        continue
                    if decision.verdict != "qualified":
                        failed_jobs += 1
                        continue
                    outcome = _save_detail(session, source, detail, collected_now, intake_ai_complete)
                    if outcome == "created":
                        created_jobs += 1
                    elif outcome == "updated":
                        updated_jobs += 1
                    else:
                        unchanged_jobs += 1
            except Exception:
                failed_jobs += 1
        if successful_details == 0:
            raise ValueError("校园就业网列表可读，但全部详情解析失败")
        _record_source_success(source, collected_now)
        source.last_monitor_summary = (
            f"{campus_source.name}试采：新增 {created_jobs} 条，无变化 {unchanged_jobs} 条，"
            f"有更新 {updated_jobs} 条，非上海 {non_shanghai_jobs} 条，详情失败 {failed_jobs} 条。"
        )
        session.add(
            TaskRun(
                task_name=f"公开采集·{source.name}",
                status="完成",
                message=source.last_monitor_summary,
            )
        )
        session.commit()
        return RealCollectionResult(created_jobs, updated_jobs, failed_jobs, unchanged_jobs, source.status)
    except Exception as exc:
        _record_source_failure(source, exc)
        session.add(TaskRun(task_name=f"公开采集·{source.name}", status="失败", message=f"采集失败：{str(exc)[:300]}"))
        session.commit()
        raise


def collect_sjtu_internship_jobs(
    session: Session, client, now: datetime | None = None, intake_ai_complete=None,
) -> RealCollectionResult:
    return _collect_campus_json_source(
        session,
        client,
        source_name=SJTU_SOURCE_NAME,
        campus_source=SJTU_INTERNSHIP_SOURCE,
        now=now,
        intake_ai_complete=intake_ai_complete,
    )


def collect_sufe_job_announcements(
    session: Session, client, now: datetime | None = None, intake_ai_complete=None,
) -> RealCollectionResult:
    return _collect_campus_json_source(
        session,
        client,
        source_name=SUFE_SOURCE_NAME,
        campus_source=SUFE_JOB_SOURCE,
        now=now,
        intake_ai_complete=intake_ai_complete,
    )


def collect_sbs_jobs(session: Session, client, pages: int = 3, now: datetime | None = None, intake_ai_complete=None) -> RealCollectionResult:
    """Collect SBS positions, retaining audience-only uncertainty for manual review."""
    collected_now = now or datetime.now()
    ensure_official_source_catalog(session)
    source = session.scalar(select(Source).where(Source.name == SBS_SOURCE_NAME))
    if source is None:
        raise ValueError("上海商学院就业网来源未配置")
    source.last_checked_at = collected_now
    created_jobs = updated_jobs = failed_jobs = unchanged_jobs = review_jobs = skipped_jobs = 0
    reasons: Counter = Counter()
    errors: Counter = Counter()
    successful_details = 0
    controller = RequestBudgetController(total_seconds=180, request_timeout_seconds=12, interval_seconds=1)
    try:
        listings = fetch_sbs_listings(client, pages=pages, controller=controller)
        if not listings:
            raise ValueError("上海商学院就业网公开列表为空，未将其视为没有新招聘")
        for listing in listings:
            try:
                detail = fetch_sbs_detail(client, listing, controller=controller)
                successful_details += 1
                decision = evaluate_review_intake(candidate_from_detail(detail, source_listing_url=source.url), now=collected_now)
                if decision.verdict not in {"qualified", "review_only"}:
                    skipped_jobs += 1
                    reasons.update(decision.reason_codes)
                    continue
                review_note = REVIEW_NOTE if decision.verdict == "review_only" else ""
                outcome = _save_detail(session, source, detail, collected_now, intake_ai_complete, review_note=review_note)
                if outcome == "created": created_jobs += 1
                elif outcome == "updated": updated_jobs += 1
                elif outcome == "unchanged": unchanged_jobs += 1
                else:
                    skipped_jobs += 1
                    reasons.update(["APPLICATION_EXPIRED"])
                if review_note and outcome in {"created", "updated"}:
                    review_jobs += 1
            except Exception as exc:
                failed_jobs += 1
                errors.update([type(exc).__name__])
        if successful_details == 0:
            raise ValueError("上海商学院就业网列表可读，但全部详情解析失败")
        _record_source_success(source, collected_now)
        summary = f"上海商学院就业网：列表 {len(listings)} 条，详情成功 {successful_details} 条；新增 {created_jobs} 条，无变化 {unchanged_jobs} 条，有更新 {updated_jobs} 条，其中受众待确认 {review_jobs} 条；规则跳过 {skipped_jobs} 条，请求或处理失败 {failed_jobs} 条。"
        if reasons:
            summary += " 过滤原因：" + ", ".join(f"{key}={value}" for key, value in reasons.items())
        if errors:
            summary += " 错误类型：" + ", ".join(f"{key}={value}" for key, value in errors.items())
            source.last_error_summary = f"部分详情失败 {failed_jobs}/{len(listings)}：" + ", ".join(errors)
        source.last_monitor_summary = summary
        session.add(TaskRun(task_name=f"公开采集·{source.name}", status="部分完成" if failed_jobs else "完成", message=summary))
        session.commit()
        return RealCollectionResult(created_jobs, updated_jobs, failed_jobs, unchanged_jobs, source.status)
    except Exception as exc:
        _record_source_failure(source, exc)
        session.add(TaskRun(task_name=f"公开采集·{source.name}", status="失败", message=f"采集失败：{str(exc)[:300]}"))
        session.commit()
        raise


def _sspu_exclusion(detail) -> str:
    """Extra source-local safeguards for the undergraduate, cross-school audience."""
    text = f"{detail.title}\n{detail.evidence_text}"
    if re.search(r"(?:仅限|只限|仅面向|只面向|只接受|仅接受|只招|仅招|(?<!不)限)\s*(?:上海第二工业大学|二工大|本校)", text):
        return "SCHOOL_EXCLUSIVE"
    degree_requirement = r"(?:任职要求|学历要求|学历|教育背景|学历背景)\s*[：:]?\s*(?:全日制)?(?:硕士|博士)(?:研究生)?(?:及以上|以上|学历|学位|\s*(?=[，,；;。\n]|$))"
    degree_metadata = r"[/／]\s*(?:全日制)?(?:硕士|博士)(?:研究生)?(?:及以上|以上)?\s*(?=\n|$)"
    if re.search(degree_requirement, text) or re.search(degree_metadata, text):
        return "POSTGRADUATE_ONLY"
    return ""


def _sspu_student_evidence(detail) -> bool:
    """Experience with interns or HR campus duties do not prove eligibility."""
    if "实习" in detail.title:
        return True
    in_requirements = False
    for line in detail.evidence_text.splitlines():
        if re.search(r"岗位职责|工作职责|工作内容|职位描述", line):
            in_requirements = False
            continue
        if re.search(r"岗位要求|任职要求|任职条件|任职资格|教育背景|学历背景|招聘对象|申请条件", line):
            in_requirements = True
        if re.search(r"^实习(?:待遇|补贴)|(?:入职|参加|接受|开始)实习", line):
            return True
        if in_requirements and re.search(r"应届|在校生|在校学生|毕业两年内|20\d{2}\s*届|校招任职要求|(?:招收|招聘|面向|接受)\s*实习生", line):
            return True
    return False


def _save_sspu_detail(session, source, detail, collected_now, *, review_note="") -> str:
    context = {key: getattr(detail, key, "") for key in (
        "title", "announcement_title", "employer_name", "location_category", "location_detail",
        "published_at", "detail_url", "official_url", "recruitment_type",
    )}
    context["review_note"] = review_note
    return _save_detail(session, source, detail, collected_now, review_note=review_note,
                        fingerprint_context=json.dumps(context, ensure_ascii=False, sort_keys=True))


def _refresh_sspu_fields(job: Job, detail) -> None:
    """Keep displayed facts in sync with the newly extracted per-role evidence."""
    candidate = candidate_from_detail(detail)
    job.announcement_title = detail.announcement_title
    job.job_title = detail.title
    job.source_url = detail.detail_url
    job.location_category = detail.location_category
    job.location_detail = detail.location_detail
    job.recruitment_type = detail.recruitment_type
    job.deadline = candidate.deadline or "原文待人工确认"
    job.application_method = "email" if candidate.application_kind == "email" else "official_page"
    job.application_contact = candidate.application_value if candidate.application_kind == "email" else ""


def collect_sspu_jobs(
    session: Session, client, pages: int = 3, now: datetime | None = None, intake_ai_complete=None,
) -> RealCollectionResult:
    """Collect the explicitly approved A source without promoting individual jobs."""
    collected_now = now or datetime.now()
    ensure_official_source_catalog(session)
    source = session.scalar(select(Source).where(Source.source_key == "sspu-news"))
    if source is None or not can_auto_collect(source) or source.status == "暂停":
        raise ValueError("二工大来源未启用自动采集或已暂停")
    source.last_checked_at = collected_now
    counts: Counter = Counter()
    reasons: Counter = Counter()
    errors: Counter = Counter()
    controller = RequestBudgetController(total_seconds=180, request_timeout_seconds=12, interval_seconds=1)
    try:
        announcements = fetch_sspu_announcements(client, pages=pages, controller=controller)
        if not announcements:
            raise ValueError("二工大公开列表为空，未将其视为没有新招聘")
        for announcement in announcements:
            try:
                details = fetch_sspu_details(client, announcement, controller=controller)
                if not details:
                    raise ValueError("二工大详情未提取到可核验岗位")
                counts["successful_details"] += 1
                counts["parsed_jobs"] += len(details)
                for detail in details:
                    candidate = candidate_from_detail(detail, source_listing_url=source.url)
                    decision = evaluate_review_intake(candidate, now=collected_now)
                    if decision.verdict == "qualified" and not _sspu_student_evidence(detail):
                        decision = CandidateDecision("review_only", ("TARGET_AUDIENCE_UNCLEAR",))
                    local_exclusion = _sspu_exclusion(detail)
                    fingerprint = f"{source.name}|{detail.identity_key}"
                    if local_exclusion or decision.verdict not in {"qualified", "review_only"}:
                        counts["skipped"] += 1
                        reason_codes = (local_exclusion,) if local_exclusion else decision.reason_codes
                        reasons.update(reason_codes)
                        # A previously accepted role can become ineligible. Preserve its
                        # history, but do not leave an old approval active after a change.
                        existing = session.scalar(select(Job).where(Job.fingerprint == fingerprint))
                        if existing is not None:
                            outcome = _save_sspu_detail(session, source, detail, collected_now)
                            if outcome == "expired":
                                existing.verification_checks = "{}"
                                existing.verification_version = 0
                                counts["expired"] += 1
                            else:
                                _refresh_sspu_fields(existing, detail)
                                if existing.intake_grade != "D" or outcome == "updated":
                                    if outcome != "updated":
                                        existing.version += 1
                                    existing.status = "待核验"
                                    existing.intake_grade = "D"
                                    existing.intake_route = "过滤留档"
                                    existing.intake_reason = "来源复查不再符合入库条件：" + ", ".join(reason_codes)
                                    existing.verification_checks = "{}"
                                    existing.verification_version = 0
                                    existing.risk_flags = REAL_RISK_FLAG
                                    counts["updated"] += 1
                        continue
                    review_note = REVIEW_NOTE if decision.verdict == "review_only" else ""
                    outcome = _save_sspu_detail(session, source, detail, collected_now, review_note=review_note)
                    counts[outcome] += 1
                    if outcome in {"created", "updated"}:
                        job = session.scalar(select(Job).where(Job.fingerprint == fingerprint))
                        _refresh_sspu_fields(job, detail)
                        if not review_note:
                            intake = screen_intake(detail.title, detail.evidence_text)
                            job.intake_grade, job.intake_route = intake.grade, intake.route
                            job.intake_reason, job.intake_evidence = intake.reason, intake.evidence
                            job.intake_confidence = intake.confidence
                        if review_note:
                            counts["review"] += 1
            except UnsupportedSspuAnnouncement as exc:
                counts["unsupported"] += 1
                reasons.update([getattr(exc, "reason_code", "EXTERNAL_LINK_UNSUPPORTED")])
            except Exception as exc:
                counts["failed"] += 1
                errors.update([type(exc).__name__])
        if not counts["successful_details"]:
            raise ValueError("二工大列表可读，但全部详情未完成岗位解析；" + ", ".join(errors or reasons))
        _record_source_success(source, collected_now)
        summary = (
            f"二工大：公告 {len(announcements)} 篇，正文成功 {counts['successful_details']} 篇，解析岗位 {counts['parsed_jobs']} 条；"
            f"新增 {counts['created']} 条，无变化 {counts['unchanged']} 条，有更新 {counts['updated']} 条，"
            f"其中受众待确认 {counts['review']} 条；规则跳过 {counts['skipped']} 条，未适配公告跳过 {counts['unsupported']} 篇，"
            f"已入库转截止 {counts['expired']} 条，请求或处理失败 {counts['failed']} 条。"
        )
        if reasons:
            summary += " 过滤原因：" + ", ".join(f"{key}={value}" for key, value in reasons.items())
        if errors:
            source.last_error_summary = "部分详情失败：" + ", ".join(f"{key}={value}" for key, value in errors.items())
            summary += " " + source.last_error_summary
        source.last_monitor_summary = summary
        session.add(SourceDiagnostic(
            source_id=source.id, checked_at=collected_now, depth="detail",
            connection_status="ok", content_status="detail_verified", adapter_status="available",
            http_status=200, final_url=source.url, sample_count=len(announcements),
            detail_success_count=counts["successful_details"], message=summary,
        ))
        session.add(TaskRun(task_name=f"公开采集·{source.name}", status="部分完成" if counts["failed"] else "完成", message=summary))
        session.commit()
        return RealCollectionResult(counts["created"], counts["updated"], counts["failed"], counts["unchanged"], source.status)
    except Exception as exc:
        _record_source_failure(source, exc)
        session.add(TaskRun(task_name=f"公开采集·{source.name}", status="失败", message=f"采集失败：{str(exc)[:300]}"))
        session.commit()
        raise


def collect_spdb_shanghai_jobs(
    session: Session, client, limit: int = 20, now: datetime | None = None, intake_ai_complete=None,
) -> RealCollectionResult:
    collected_now = now or datetime.now()
    ensure_official_source_catalog(session)
    source = session.scalar(select(Source).where(Source.name == SPDB_SOURCE_NAME))
    if source is None:
        raise ValueError("浦发官方招聘来源未配置")
    source.last_checked_at = collected_now
    created_jobs = updated_jobs = failed_jobs = unchanged_jobs = 0
    try:
        fetched = fetch_spdb_shanghai_job_details(client, limit=limit, today=collected_now.date())
        for detail in fetched.details:
            try:
                outcome = _save_detail(session, source, detail, collected_now, intake_ai_complete)
                if outcome == "created":
                    created_jobs += 1
                elif outcome == "updated":
                    updated_jobs += 1
                else:
                    unchanged_jobs += 1
            except Exception:
                failed_jobs += 1
        _record_source_success(source, collected_now)
        source.last_monitor_summary = (
            f"浦发上海官方采集：新增 {created_jobs} 条，无变化 {unchanged_jobs} 条，"
            f"有更新 {updated_jobs} 条，学生适配预筛过滤 {fetched.filtered_count} 条。"
        )
        session.add(TaskRun(task_name="公开采集·上海浦东发展银行", status="完成", message=f"新增 {created_jobs} 条，无变化 {unchanged_jobs} 条，有更新 {updated_jobs} 条，学生适配预筛过滤 {fetched.filtered_count} 条，单条失败 {failed_jobs} 条。"))
        session.commit()
        return RealCollectionResult(created_jobs, updated_jobs, failed_jobs, unchanged_jobs, source.status)
    except Exception as exc:
        _record_source_failure(source, exc)
        session.add(TaskRun(task_name="公开采集·上海浦东发展银行", status="失败", message=f"采集失败：{str(exc)[:300]}"))
        session.commit()
        raise


def collect_shanghai_sasac(
    session: Session, client, limit: int = 12, now: datetime | None = None, intake_ai_complete=None,
) -> RealCollectionResult:
    collected_now = now or datetime.now()
    source = _ensure_source(session, collected_now)
    created_jobs = 0
    unchanged_jobs = 0
    updated_jobs = 0
    failed_jobs = 0
    failure_samples: list[str] = []
    try:
        listings = fetch_shanghai_sasac_listings(client, limit=limit)
        if not listings:
            raise ValueError("来源列表为空，未将其视为没有新招聘")
        for listing in listings:
            try:
                detail = fetch_shanghai_sasac_detail(client, listing)
                fingerprint = f"上海市国资委|{detail.title}|{detail.published_at}|上海|公告"
                job = session.scalar(select(Job).where(Job.fingerprint == fingerprint))
                content_fingerprint = _content_fingerprint(detail.evidence_text)
                extracted_deadline = extract_application_deadline(detail.evidence_text)
                if extracted_deadline is not None and extracted_deadline < collected_now.date():
                    if job is not None:
                        mark_job_expired(job, extracted_deadline)
                    unchanged_jobs += 1
                    continue
                if job is None:
                    intake = (
                        screen_intake_with_ai(detail.title, detail.evidence_text, intake_ai_complete)
                        if intake_ai_complete is not None
                        else screen_intake(detail.title, detail.evidence_text)
                    )
                    session.add(
                        Job(
                            fingerprint=fingerprint,
                            employer_name="待人工核验（上海国资招聘公告）",
                            announcement_title=detail.title,
                            job_title=detail.title,
                            job_family="待分类",
                            recruitment_type="待核验",
                            location_category="原文未明确",
                            location_detail="",
                            target_audience="待人工判断",
                            direction_tags="待人工分类",
                            deadline=extracted_deadline.isoformat() if extracted_deadline else "原文待人工确认",
                            official_url="",
                            source_url=detail.detail_url,
                            evidence_text=detail.evidence_text,
                            evidence_status="正文已提取",
                            evidence_note="",
                            quality_score=0,
                            risk_flags=REAL_RISK_FLAG,
                            is_demo=False,
                            published_at=_parse_published_at(detail.published_at),
                            collected_at=collected_now,
                            content_fingerprint=content_fingerprint,
                            last_verified_at=collected_now,
                            lifecycle_status="正常",
                            last_change_summary="",
                            status="待核验",
                            intake_grade=intake.grade,
                            intake_route=intake.route,
                            intake_reason=intake.reason,
                            intake_evidence=intake.evidence,
                            intake_confidence=intake.confidence,
                        )
                    )
                    created_jobs += 1
                elif job.content_fingerprint == content_fingerprint:
                    job.last_verified_at = collected_now
                    unchanged_jobs += 1
                else:
                    job.evidence_text = detail.evidence_text
                    job.announcement_title = detail.title or job.announcement_title
                    job.evidence_status = "正文已提取"
                    job.evidence_note = ""
                    job.last_verified_at = collected_now
                    job.published_at = _parse_published_at(detail.published_at) or job.published_at
                    job.content_fingerprint = content_fingerprint
                    job.lifecycle_status = "有更新"
                    job.last_change_summary = "原文内容发生变化，待人工确认"
                    job.status = "待核验"
                    job.risk_flags = REAL_RISK_FLAG
                    job.verification_checks = "{}"
                    job.verification_version = 0
                    job.version += 1
                    updated_jobs += 1
            except Exception as exc:
                failed_jobs += 1
                if len(failure_samples) < 3:
                    failure_samples.append(f"{getattr(listing, 'detail_url', '')} {type(exc).__name__}: {str(exc)[:120]}")
        if failed_jobs == len(listings):
            raise ValueError("来源列表可读，但全部详情解析失败；" + "；".join(failure_samples))
        _record_source_success(source, collected_now)
        message = (
            f"上海市国资委真实公开来源：新增 {created_jobs} 条，无变化 {unchanged_jobs} 条，"
            f"有更新 {updated_jobs} 条，单条失败 {failed_jobs} 条。"
        )
        if failed_jobs:
            source.last_error_summary = f"部分详情失败 {failed_jobs}/{len(listings)}；" + "；".join(failure_samples)
            message += " 部分详情未完成：" + "；".join(failure_samples)
        source.last_monitor_summary = message
        session.add(TaskRun(task_name="上海市国资委公开采集", status="部分完成" if failed_jobs else "完成", message=message))
        session.commit()
        return RealCollectionResult(
            created_jobs=created_jobs,
            updated_jobs=updated_jobs,
            failed_jobs=failed_jobs,
            unchanged_jobs=unchanged_jobs,
            source_status=source.status,
        )
    except Exception as exc:
        _record_source_failure(source, exc)
        session.add(
            TaskRun(
                task_name="上海市国资委公开采集",
                status="失败",
                message=f"采集失败：{str(exc)[:300]}",
            )
        )
        session.commit()
        raise
