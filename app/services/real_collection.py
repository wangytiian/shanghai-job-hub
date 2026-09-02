from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, Source, TaskRun
from app.services.intake_screening import screen_intake, screen_intake_with_ai
from app.services.collection_strategy import build_collection_plan
from app.services.source_library import can_auto_collect
from app.sources.catalog import ensure_official_source_catalog
from app.sources.official_list import fetch_official_detail, fetch_official_listings
from app.sources.shanghai_sasac import (
    LISTING_URL,
    fetch_shanghai_sasac_detail,
    fetch_shanghai_sasac_listings,
)
from app.sources.spdb import fetch_spdb_shanghai_job_details


REAL_SOURCE_NAME = "上海市国资委国企招聘（真实公开来源）"
SPDB_SOURCE_NAME = "上海浦东发展银行官方招聘"
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


def _attachment_links(detail) -> str:
    return json.dumps(
        [{"name": item.name, "url": item.url} for item in getattr(detail, "attachments", ())],
        ensure_ascii=False,
        separators=(",", ":"),
    )


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
) -> str:
    identity_key = getattr(detail, "identity_key", detail.title)
    fingerprint = f"{source.name}|{identity_key}|{detail.published_at}|{source.scope_group}|公告"
    job = session.scalar(select(Job).where(Job.fingerprint == fingerprint))
    attachment_links = _attachment_links(detail)
    content_fingerprint = _content_fingerprint(f"{detail.evidence_text}\n{attachment_links}")
    if job is None:
        intake = (
            screen_intake_with_ai(detail.title, detail.evidence_text, intake_ai_complete)
            if intake_ai_complete is not None
            else screen_intake(detail.title, detail.evidence_text)
        )
        session.add(
            Job(
                fingerprint=fingerprint,
                employer_name=getattr(detail, "employer_name", f"待人工核验（{source.name}）"),
                job_title=detail.title,
                job_family="待分类",
                recruitment_type=getattr(detail, "recruitment_type", "待核验"),
                location_category=getattr(detail, "location_category", "地区待定"),
                location_detail=getattr(detail, "location_detail", "以公告原文为准"),
                target_audience="待人工判断",
                direction_tags="待人工分类",
                deadline=getattr(detail, "deadline", "原文待人工确认"),
                official_url=getattr(detail, "official_url", ""),
                source_url=detail.detail_url,
                evidence_text=detail.evidence_text,
                attachment_links=attachment_links,
                quality_score=0,
                risk_flags=REAL_RISK_FLAG,
                is_demo=False,
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
        return "created"
    if job.content_fingerprint == content_fingerprint:
        job.collected_at = collected_now
        job.last_verified_at = collected_now
        return "unchanged"
    job.evidence_text = detail.evidence_text
    job.employer_name = getattr(detail, "employer_name", job.employer_name)
    job.official_url = getattr(detail, "official_url", job.official_url)
    job.attachment_links = attachment_links
    job.collected_at = collected_now
    job.last_verified_at = collected_now
    job.content_fingerprint = content_fingerprint
    job.lifecycle_status = "有更新"
    job.last_change_summary = "原文内容发生变化，待人工确认"
    job.status = "待核验"
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
    session.add(TaskRun(task_name="每日多来源采集", status="完成" if successful else "失败", message=f"尝试 {attempted} 个来源，成功 {successful} 个，跳过 {skipped} 个；新增 {created} 条，无变化 {unchanged} 条，有更新 {updated} 条，失败 {failed} 项。"))
    session.commit()
    return DailyCollectionResult(attempted, successful, skipped, created, updated, unchanged, failed)


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
                            job_title=detail.title,
                            job_family="待分类",
                            recruitment_type="待核验",
                            location_category="明确上海",
                            location_detail="上海（公告来源）",
                            target_audience="待人工判断",
                            direction_tags="待人工分类",
                            deadline="原文待人工确认",
                            official_url="",
                            source_url=detail.detail_url,
                            evidence_text=detail.evidence_text,
                            quality_score=0,
                            risk_flags=REAL_RISK_FLAG,
                            is_demo=False,
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
                    job.collected_at = collected_now
                    job.last_verified_at = collected_now
                    unchanged_jobs += 1
                else:
                    job.evidence_text = detail.evidence_text
                    job.collected_at = collected_now
                    job.last_verified_at = collected_now
                    job.content_fingerprint = content_fingerprint
                    job.lifecycle_status = "有更新"
                    job.last_change_summary = "原文内容发生变化，待人工确认"
                    job.status = "待核验"
                    job.version += 1
                    updated_jobs += 1
            except Exception:
                failed_jobs += 1
        _record_source_success(source, collected_now)
        message = (
            f"上海市国资委真实公开来源：新增 {created_jobs} 条，无变化 {unchanged_jobs} 条，"
            f"有更新 {updated_jobs} 条，单条失败 {failed_jobs} 条。"
        )
        session.add(TaskRun(task_name="上海市国资委公开采集", status="完成", message=message))
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
