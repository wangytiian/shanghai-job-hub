from pathlib import Path
import json
from datetime import datetime
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.database import create_database
from app.models import DistributionItem, Job, ReviewLog, Source, TaskRun
from app.services.ai_settings import (
    AiSettingsService,
    CredentialNotConfiguredError,
    OPENAI_COMPATIBLE_PROVIDER,
    TextProviderNotReadyError,
    WindowsCredentialStore,
)
from app.services.collection_strategy import build_collection_plan
from app.services.source_library import monitoring_message
from app.seed import seed_demo_data
from app.services.distribution import build_wechat_draft, create_distribution_items
from app.services.real_collection import REAL_SOURCE_NAME, collect_due_sources, collect_shanghai_sasac
from app.sources.catalog import OFFICIAL_SOURCE_CATALOG, ensure_official_source_catalog
from app.services.reviews import review_job
from app.services.jobs import validate_publishable
from app.services.structuring import StructuringInput, StructuringValidationError, structure_job
from app.services.ai_structuring import build_structuring_prompt, parse_ai_draft
from app.services.ai_content_draft import build_content_prompt, parse_content_draft
from app.services.notice_classification import (
    classify_job,
    confirm_suggested_new_recruitments,
    suggest_notice_type,
    suggested_new_recruitment_jobs,
)
from app.services.tasks import run_demo_collection
from app.services.wechat_leads import import_public_wechat_article
from app.services.publication_safety import return_unsafe_publishable_jobs
from app.services.deadline_policy import expire_known_deadline_jobs
from app.services.attachment_parser import create_pending_child_jobs, parse_xlsx_role_candidates
from app.services.intake_backfill import backfill_unscreened_intake_jobs
from app.services.ai_scoring import suggest_job_score

APP_DIR = Path(__file__).parent
DATA_DIR = APP_DIR.parent / "data"
DEFAULT_DATABASE_PATH = DATA_DIR / "recruiting_local.db"
DEFAULT_DATABASE_URL = f"sqlite:///{DEFAULT_DATABASE_PATH.as_posix()}"


def create_app(database_url: str = DEFAULT_DATABASE_URL) -> FastAPI:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="招聘内容运营后台", docs_url=None, redoc_url=None)
    session_factory = create_database(database_url)
    with session_factory() as session:
        seed_demo_data(session)
        ensure_official_source_catalog(session)
        backfill_unscreened_intake_jobs(session)
        expire_known_deadline_jobs(session)
        return_unsafe_publishable_jobs(session)
    app.state.session_factory = session_factory
    app.state.ai_settings_service = AiSettingsService(WindowsCredentialStore())
    templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

    def get_session() -> Session:
        return app.state.session_factory()

    def get_ai_settings_service() -> AiSettingsService:
        return app.state.ai_settings_service

    def configured_intake_ai_complete(session: Session):
        """Return a callback for the currently selected, locally configured text AI."""
        service = get_ai_settings_service()
        if not service.is_active_text_provider_ready(session):
            return None

        def complete(prompt: str) -> str:
            return service.complete_text(session, prompt)

        return complete

    def build_global_status(session: Session) -> dict[str, object]:
        last_task = session.scalar(select(TaskRun).order_by(TaskRun.id.desc()).limit(1))
        pending = session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.is_demo.is_(False), Job.status.in_(("待核验", "待审核")))
        ) or 0
        unhealthy = session.scalar(
            select(func.count())
            .select_from(Source)
            .where(Source.status.in_(("异常", "暂停")))
        ) or 0
        return {
            "last_task_at": last_task.created_at if last_task else None,
            "pending_work_count": pending,
            "unhealthy_source_count": unhealthy,
            "operator_name": "本地管理员",
        }

    def page_context(session: Session, active_nav: str, **values) -> dict[str, object]:
        return {
            "active_nav": active_nav,
            "global_status": build_global_status(session),
            **values,
        }

    def render_job_detail(
        request: Request,
        session: Session,
        job: Job,
        review_feedback: str = "",
        review_feedback_kind: str = "",
    ):
        try:
            attachments = json.loads(job.attachment_links or "[]")
        except json.JSONDecodeError:
            attachments = []
        attachments = [
            {**attachment, "is_xlsx": str(attachment.get("url", "")).split("?", 1)[0].lower().endswith(".xlsx")}
            for attachment in attachments
            if isinstance(attachment, dict) and attachment.get("url") and attachment.get("name")
        ]
        child_jobs = session.scalars(
            select(Job).where(Job.parent_job_id == job.id).order_by(Job.created_at.desc(), Job.id.desc())
        ).all()
        child_status_counts = {
            status: sum(1 for child in child_jobs if child.status == status)
            for status in ("待核验", "待审核", "可发布")
        }
        parent_job = session.get(Job, job.parent_job_id) if job.parent_job_id else None
        return templates.TemplateResponse(
            request,
            "job_detail.html",
            page_context(
                session,
                "jobs",
                job=job,
                logs=session.scalars(
                    select(ReviewLog).where(ReviewLog.job_id == job.id).order_by(ReviewLog.id.desc())
                ).all(),
                publish_errors=validate_publishable(job),
                review_feedback=review_feedback,
                review_feedback_kind=review_feedback_kind,
                attachments=attachments,
                child_jobs=child_jobs,
                child_status_counts=child_status_counts,
                parent_job=parent_job,
            ),
        )

    @app.get("/")
    def dashboard(request: Request):
        with get_session() as session:
            workflow_counts = {
                status: session.scalar(
                    select(func.count())
                    .select_from(Job)
                    .where(Job.is_demo.is_(False), Job.status == status)
                ) or 0
                for status in ("待核验", "待审核", "可发布", "已发布")
            }
            source_health = {
                status: session.scalar(
                    select(func.count()).select_from(Source).where(Source.status == status)
                ) or 0
                for status in ("正常", "异常", "暂停")
            }
            return templates.TemplateResponse(
                request,
                "dashboard.html",
                page_context(
                    session,
                    "dashboard",
                    workflow_counts=workflow_counts,
                    source_health=source_health,
                    total_jobs=session.scalar(
                        select(func.count()).select_from(Job).where(Job.is_demo.is_(False))
                    ) or 0,
                    task_runs=session.scalars(
                        select(TaskRun).order_by(TaskRun.id.desc()).limit(5)
                    ).all(),
                    logs=session.scalars(
                        select(ReviewLog).order_by(ReviewLog.id.desc()).limit(5)
                    ).all(),
                ),
            )

    @app.get("/sources")
    def sources(
        request: Request, health_feedback: str = "", health_feedback_kind: str = ""
    ):
        with get_session() as session:
            source_records = session.scalars(
                select(Source)
                .where(Source.name.in_([definition.name for definition in OFFICIAL_SOURCE_CATALOG]))
                .order_by(Source.name.contains("真实公开来源").desc(), Source.id)
            ).all()
            source_plans = {
                source.id: build_collection_plan(
                    source.level, __import__("datetime").datetime.now(), source.last_success_at
                )
                for source in source_records
            }
            tier_summaries = {
                tier: sum(1 for source in source_records if source.library_tier == tier)
                for tier in ("A", "B", "C", "D")
            }
            source_messages = {
                source.id: monitoring_message(source) for source in source_records
            }
            return templates.TemplateResponse(
                request,
                "sources.html",
                page_context(
                    session,
                    "sources",
                    sources=source_records,
                    source_plans=source_plans,
                    tier_summaries=tier_summaries,
                    source_messages=source_messages,
                    health_feedback=health_feedback,
                    health_feedback_kind=health_feedback_kind,
                ),
            )

    @app.post("/sources/{source_id}/resume")
    def resume_source(source_id: int):
        with get_session() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise HTTPException(404, "来源不存在")
            source.status = "正常"
            source.consecutive_failure_count = 0
            source.pause_reason = ""
            source.last_error_summary = ""
            session.commit()
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/{source_id}/health-check")
    def source_health_check(request: Request, source_id: int):
        from app.services.source_health import check_source_connection

        with get_session() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise HTTPException(404, "来源不存在")
            if source.library_tier == "A":
                return sources(
                    request,
                    "A 类来源由每日采集任务维护健康状态，无需单独连接检查。",
                    "error",
                )
            with httpx.Client(follow_redirects=True, max_redirects=3) as client:
                result = check_source_connection(source, client, __import__("datetime").datetime.now())
            session.commit()
        return sources(request, result.message, result.kind)

    @app.get("/sources/wechat-leads/import")
    def wechat_lead_import_page(request: Request):
        with get_session() as session:
            source = session.scalar(
                select(Source).where(Source.adapter_key == "wechat_article_lead")
            )
            if source is None:
                raise HTTPException(404, "公众号线索来源尚未配置")
            return templates.TemplateResponse(
                request,
                "wechat_lead_import.html",
                page_context(session, "sources", source=source, error=""),
            )

    @app.post("/sources/wechat-leads/import")
    def import_wechat_lead(request: Request, article_url: str = Form()):
        with get_session() as session:
            source = session.scalar(
                select(Source).where(Source.adapter_key == "wechat_article_lead")
            )
            if source is None:
                raise HTTPException(404, "公众号线索来源尚未配置")
            try:
                with httpx.Client(follow_redirects=True) as client:
                    job = import_public_wechat_article(session, source, article_url, client)
            except (ValueError, httpx.HTTPError) as exc:
                return templates.TemplateResponse(
                    request,
                    "wechat_lead_import.html",
                    page_context(session, "sources", source=source, error=str(exc)[:300]),
                    status_code=400,
                )
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.get("/settings/ai")
    def ai_settings(
        request: Request,
        test_provider: str = "",
        test_result: str = "",
    ):
        with get_session() as session:
            bailian_setting, openai_setting = get_ai_settings_service().get_all_settings(session)
            return templates.TemplateResponse(
                request,
                "ai_settings.html",
                page_context(
                    session,
                    "ai_settings",
                    setting=bailian_setting,
                    bailian_setting=bailian_setting,
                    openai_setting=openai_setting,
                    test_provider=test_provider,
                    test_result=test_result,
                ),
            )

    @app.post("/settings/ai/key")
    def save_ai_key(api_key: str = Form()):
        with get_session() as session:
            try:
                get_ai_settings_service().save_api_key(session, api_key)
            except (ValueError, RuntimeError) as exc:
                setting = get_ai_settings_service().get_setting(session)
                setting.connection_status = "error"
                setting.last_error_summary = str(exc)[:300]
                session.commit()
        return RedirectResponse("/settings/ai", status_code=303)

    @app.post("/settings/ai/models")
    def save_ai_models(
        text_model: str = Form(),
        ocr_model: str = Form(),
        text_enabled: str | None = Form(None),
        ocr_enabled: str | None = Form(None),
    ):
        with get_session() as session:
            try:
                get_ai_settings_service().save_models(
                    session,
                    text_model,
                    ocr_model,
                    text_enabled == "on",
                    ocr_enabled == "on",
                )
            except ValueError as exc:
                setting = get_ai_settings_service().get_setting(session)
                setting.connection_status = "error"
                setting.last_error_summary = str(exc)[:300]
                session.commit()
        return RedirectResponse("/settings/ai", status_code=303)

    @app.post("/settings/ai/test")
    def test_ai_connection():
        with get_session() as session:
            try:
                setting = get_ai_settings_service().test_connection(session)
            except CredentialNotConfiguredError as exc:
                setting = get_ai_settings_service().get_setting(session)
                setting.connection_status = "not_configured"
                setting.last_error_summary = str(exc)[:300]
                session.commit()
        result = "success" if setting.connection_status == "ready" else "error"
        return RedirectResponse(
            f"/settings/ai?test_provider=bailian&test_result={result}", status_code=303
        )

    @app.post("/settings/ai/openai")
    def save_openai_ai_settings(
        api_key: str = Form(),
        base_url: str = Form(),
        text_model: str = Form(),
        api_mode: str = Form("chat_completions"),
        text_enabled: str | None = Form(None),
        make_active: str | None = Form(None),
    ):
        with get_session() as session:
            try:
                get_ai_settings_service().save_openai_settings(
                    session,
                    api_key=api_key,
                    base_url=base_url,
                    text_model=text_model,
                    api_mode=api_mode,
                    text_enabled=text_enabled == "on",
                    make_active=make_active == "on",
                )
            except (ValueError, RuntimeError) as exc:
                setting = get_ai_settings_service().get_setting(session, OPENAI_COMPATIBLE_PROVIDER)
                setting.connection_status = "error"
                setting.last_error_summary = str(exc)[:300]
                session.commit()
        return RedirectResponse("/settings/ai", status_code=303)

    @app.post("/settings/ai/openai/test")
    def test_openai_ai_connection():
        with get_session() as session:
            try:
                setting = get_ai_settings_service().test_connection(session, OPENAI_COMPATIBLE_PROVIDER)
            except (CredentialNotConfiguredError, TextProviderNotReadyError) as exc:
                setting = get_ai_settings_service().get_setting(session, OPENAI_COMPATIBLE_PROVIDER)
                setting.connection_status = "not_configured"
                setting.last_error_summary = str(exc)[:300]
                session.commit()
        result = "success" if setting.connection_status == "ready" else "error"
        return RedirectResponse(
            f"/settings/ai?test_provider={OPENAI_COMPATIBLE_PROVIDER}&test_result={result}",
            status_code=303,
        )

    @app.post("/settings/ai/active")
    def set_active_ai_provider(provider: str = Form()):
        with get_session() as session:
            try:
                get_ai_settings_service().set_active_text_provider(session, provider)
            except (ValueError, CredentialNotConfiguredError, TextProviderNotReadyError) as exc:
                setting = get_ai_settings_service().get_setting(session, provider if provider == OPENAI_COMPATIBLE_PROVIDER else "bailian")
                setting.connection_status = "error"
                setting.last_error_summary = str(exc)[:300]
                session.commit()
        return RedirectResponse("/settings/ai", status_code=303)

    @app.get("/jobs")
    def jobs(
        request: Request,
        status: str = "",
        data_type: str = "real",
        intake_grade: str = "",
        query: str = "",
        classification_feedback: str = "",
        scoring_feedback: str = "",
    ):
        with get_session() as session:
            if data_type not in {"real", "demo", "all"}:
                raise HTTPException(400, "数据属性筛选无效")
            statement = select(Job).order_by(Job.updated_at.desc(), Job.id.desc())
            if data_type == "real":
                statement = statement.where(Job.is_demo.is_(False))
            elif data_type == "demo":
                statement = statement.where(Job.is_demo.is_(True))
            if status:
                statement = statement.where(Job.status == status)
            elif data_type == "real":
                statement = statement.where(Job.status != "已截止")
            if intake_grade:
                if intake_grade not in {"A", "B", "C", "D"}:
                    raise HTTPException(400, "入库分级筛选无效")
                statement = statement.where(Job.intake_grade == intake_grade)
            elif data_type == "real":
                statement = statement.where(Job.intake_grade != "D")
            normalized_query = query.strip()
            if normalized_query:
                statement = statement.where(
                    or_(
                        Job.employer_name.contains(normalized_query),
                        Job.job_title.contains(normalized_query),
                    )
                )
            job_records = session.scalars(statement).all()
            suggested_new_recruitment_count = (
                len(suggested_new_recruitment_jobs(session))
                if data_type == "real" and status in {"", "待核验"}
                else 0
            )
            suggested_score_candidate_count = session.scalar(
                select(func.count())
                .select_from(Job)
                .where(
                    Job.is_demo.is_(False),
                    Job.status == "待核验",
                    Job.notice_type == "新招聘",
                    Job.intake_grade.in_(("A", "B", "C")),
                    Job.quality_score == 0,
                    Job.ai_score_status == "待建议",
                )
            ) or 0
            return templates.TemplateResponse(
                request,
                "jobs.html",
                page_context(
                    session,
                    "jobs",
                    jobs=job_records,
                    selected_status=status,
                    selected_intake_grade=intake_grade,
                    selected_data_type=data_type,
                    query=normalized_query,
                    suggested_new_recruitment_count=suggested_new_recruitment_count,
                    suggested_score_candidate_count=suggested_score_candidate_count,
                    classification_feedback=classification_feedback,
                    scoring_feedback=scoring_feedback,
                ),
            )

    @app.post("/jobs/scoring/suggest-batch")
    def suggest_scores_batch():
        with get_session() as session:
            candidates = session.scalars(
                select(Job)
                .where(
                    Job.is_demo.is_(False),
                    Job.status == "待核验",
                    Job.notice_type == "新招聘",
                    Job.intake_grade.in_(("A", "B", "C")),
                    Job.quality_score == 0,
                    Job.ai_score_status == "待建议",
                )
                .order_by(Job.intake_grade.asc(), Job.collected_at.desc(), Job.id.desc())
                .limit(5)
            ).all()
            complete = configured_intake_ai_complete(session)
            ai_count = 0
            rules_count = 0
            for job in candidates:
                result = suggest_job_score(job, complete=complete)
                job.ai_suggested_score = result.score
                job.ai_score_status = result.status
                job.ai_score_reason = result.reason
                job.ai_score_breakdown = json.dumps(result.breakdown, ensure_ascii=False)
                job.ai_score_confidence = result.confidence
                job.ai_scored_at = datetime.now()
                if result.status == "AI建议":
                    ai_count += 1
                elif result.status == "规则建议":
                    rules_count += 1
                session.add(
                    ReviewLog(
                        job_id=job.id,
                        action="AI建议分生成",
                        note=f"建议分：{result.score}；状态：{result.status}；理由：{result.reason}",
                        operator_name="本地管理员",
                    )
                )
            session.commit()
        message = f"本批处理 {len(candidates)} 条：AI 建议 {ai_count} 条，规则建议 {rules_count} 条。"
        return RedirectResponse(f"/jobs?{urlencode({'scoring_feedback': message})}", status_code=303)

    @app.get("/jobs/{job_id}")
    def job_detail(request: Request, job_id: int):
        with get_session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(404, "岗位不存在")
            return render_job_detail(request, session, job)

    @app.get("/jobs/{job_id}/structure")
    def job_structuring(
        request: Request,
        job_id: int,
        ai_feedback: str = "",
        ai_message: str = "",
    ):
        with get_session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(404, "岗位不存在")
            if job.status != "待核验" or job.notice_type != "新招聘":
                raise HTTPException(409, "只有待核验公告可以结构化")
            return templates.TemplateResponse(
                request,
                "job_structuring.html",
                page_context(
                    session,
                    "jobs",
                    job=job,
                    ai_feedback=ai_feedback,
                    ai_message=ai_message,
                ),
            )

    @app.post("/jobs/{job_id}/structure/ai-draft")
    def ai_structure_draft(request: Request, job_id: int):
        with get_session() as session:
            job = session.get(Job, job_id)
            if job is None or job.status != "待核验" or job.notice_type != "新招聘":
                raise HTTPException(409, "只有确认为新招聘的待核验公告可以使用 AI 预填")
            service = get_ai_settings_service()
            if not service.is_active_text_provider_ready(session):
                return RedirectResponse(
                    url=f"/jobs/{job_id}/structure?{urlencode({'ai_feedback': 'error', 'ai_message': '尚未完成 AI 服务配置，请前往 AI 模型配置页面保存密钥。'})}",
                    status_code=303,
                )
            try:
                content = service.complete_text(session, build_structuring_prompt(job.job_title, job.source_url, job.evidence_text))
                draft = parse_ai_draft(content, job.evidence_text)
            except Exception as exc:
                return RedirectResponse(
                    url=f"/jobs/{job_id}/structure?{urlencode({'ai_feedback': 'error', 'ai_message': 'AI 请求失败，请检查模型配置、账户额度和网络后重试；也可继续手工填写。'})}",
                    status_code=303,
                )
            return templates.TemplateResponse(
                request,
                "job_structuring.html",
                page_context(
                    session,
                    "jobs",
                    job=job,
                    ai_draft=draft,
                    ai_feedback="success",
                    ai_message="AI 已完成预填。请逐项核对带入字段后再提交。",
                ),
            )

    @app.post("/jobs/{job_id}/structure")
    def submit_job_structuring(
        request: Request,
        job_id: int,
        employer_name: str = Form(),
        job_title: str = Form(),
        job_family: str = Form(),
        recruitment_type: str = Form(),
        location_category: str = Form(),
        location_detail: str = Form(),
        target_audience: str = Form(),
        direction_tags: str = Form(),
        deadline: str = Form(""),
        official_url: str = Form(),
        posting_scope: str = Form("single_role"),
        attachment_status: str = Form("not_required"),
        application_method: str = Form("official_page"),
        application_contact: str = Form(""),
        quality_score: int = Form(0),
        note: str = Form(""),
        student_fit_level: str = Form("待人工判断"),
        distribution_recommendation: str = Form("仅保留资料库"),
        ai_rationale: str = Form(""),
        ai_confidence: str = Form("低"),
    ):
        structuring_input = StructuringInput(
            employer_name=employer_name,
            job_title=job_title,
            job_family=job_family,
            recruitment_type=recruitment_type,
            location_category=location_category,
            location_detail=location_detail,
            target_audience=target_audience,
            direction_tags=direction_tags,
            deadline=deadline,
            official_url=official_url,
            posting_scope=posting_scope,
            attachment_status=attachment_status,
            application_method=application_method,
            application_contact=application_contact,
            quality_score=quality_score,
            note=note,
            student_fit_level=student_fit_level,
            distribution_recommendation=distribution_recommendation,
            ai_rationale=ai_rationale,
            ai_confidence=ai_confidence,
        )
        with get_session() as session:
            try:
                structure_job(
                    session,
                    job_id,
                    structuring_input,
                    "本地管理员",
                )
            except StructuringValidationError as exc:
                job = session.get(Job, job_id)
                if job is None:
                    raise HTTPException(404, "岗位不存在")
                return templates.TemplateResponse(
                    request,
                    "job_structuring.html",
                    page_context(
                        session,
                        "jobs",
                        job=job,
                        form_values=structuring_input.__dict__,
                        field_errors=exc.field_errors,
                        submission_feedback="error",
                        submission_message=f"还需处理 {len(exc.field_errors)} 项后才能进入待审核。",
                    ),
                    status_code=200,
                )
            except ValueError as exc:
                job = session.get(Job, job_id)
                if job is None:
                    raise HTTPException(404, "岗位不存在")
                return templates.TemplateResponse(
                    request,
                    "job_structuring.html",
                    page_context(
                        session,
                        "jobs",
                        form_values=structuring_input.__dict__,
                        field_errors={},
                        submission_feedback="error",
                        submission_message=str(exc),
                    ),
                    status_code=200,
                )
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/classification")
    def classify_job_route(job_id: int, notice_type: str = Form()):
        with get_session() as session:
            try:
                classify_job(session, job_id, notice_type, "本地管理员")
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/classification/confirm-suggestions")
    def confirm_suggested_notice_classifications():
        with get_session() as session:
            count = confirm_suggested_new_recruitments(session, "本地管理员")
        feedback = (
            f"已批量确认 {count} 条系统建议的新招聘公告，可逐条进入公告结构化。"
            if count
            else "没有仍符合批量确认条件的公告。"
        )
        return RedirectResponse(
            f"/jobs?{urlencode({'data_type': 'real', 'status': '待核验', 'classification_feedback': feedback})}",
            status_code=303,
        )

    @app.post("/jobs/{job_id}/attachments/parse")
    def parse_attachment(
        request: Request,
        job_id: int,
        attachment_name: str = Form(),
        attachment_url: str = Form(),
    ):
        with get_session() as session:
            parent = session.get(Job, job_id)
            if parent is None:
                raise HTTPException(404, "岗位不存在")
            try:
                attachments = json.loads(parent.attachment_links or "[]")
            except json.JSONDecodeError:
                attachments = []
            selected = next(
                (
                    item for item in attachments
                    if isinstance(item, dict)
                    and item.get("name") == attachment_name
                    and item.get("url") == attachment_url
                ),
                None,
            )
            if selected is None:
                return render_job_detail(request, session, parent, "附件来源不匹配，不能解析。", "error")
            if not attachment_url.split("?", 1)[0].lower().endswith(".xlsx"):
                return render_job_detail(request, session, parent, "当前只支持解析已核验的 .xlsx 岗位说明附件。", "error")
            if parent.attachment_status != "checked":
                return render_job_detail(request, session, parent, "请先将附件核验状态设为“已核验”，再拆分岗位。", "error")
            try:
                response = httpx.get(attachment_url, follow_redirects=True, timeout=20.0)
                if response.status_code >= 400:
                    raise ValueError(f"附件下载失败（HTTP {response.status_code}）")
                if len(response.content) > 8 * 1024 * 1024:
                    raise ValueError("附件超过 8MB，暂不自动解析")
                candidates = parse_xlsx_role_candidates(response.content)
                if not candidates:
                    raise ValueError("附件未发现明确的“岗位名称/招聘岗位/岗位”列或有效岗位行")
                children = create_pending_child_jobs(
                    session, parent, attachment_name, attachment_url, candidates, "本地管理员"
                )
            except (ValueError, httpx.HTTPError) as exc:
                return render_job_detail(request, session, parent, f"未生成岗位：{exc}", "error")
            return render_job_detail(
                request,
                session,
                parent,
                f"已从官方附件生成 {len(children)} 条待核验岗位。请逐条打开补齐事实并完成最终审核。",
                "success",
            )

    @app.post("/tasks/demo-collection")
    def demo_collection():
        with get_session() as session:
            run_demo_collection(session)
        return RedirectResponse("/", status_code=303)

    @app.post("/tasks/shanghai-sasac-collection")
    def shanghai_sasac_collection():
        with get_session() as session:
            source = session.scalar(select(Source).where(Source.name == REAL_SOURCE_NAME))
            if source is not None and source.status == "暂停":
                raise HTTPException(409, "该来源已暂停，请先在来源监控中恢复来源后再运行。")
        with get_session() as session, httpx.Client(follow_redirects=True) as client:
            try:
                collect_shanghai_sasac(session, client)
            except Exception as exc:
                raise HTTPException(502, f"公开来源采集失败：{exc}") from exc
        return RedirectResponse("/jobs?data_type=real&status=待核验", status_code=303)

    @app.post("/tasks/daily-collection")
    def daily_collection():
        with get_session() as session, httpx.Client(follow_redirects=True) as client:
            result = collect_due_sources(
                session, client, force=True, intake_ai_complete=configured_intake_ai_complete(session)
            )
        if result.successful_sources == 0:
            raise HTTPException(502, "每日采集未成功完成，请到来源监控查看失败原因。")
        return RedirectResponse("/jobs?data_type=real&status=待核验", status_code=303)

    @app.post("/jobs/{job_id}/review")
    def review(
        request: Request,
        job_id: int,
        action: str = Form(),
        note: str = Form(""),
        extra_note: str = Form(""),
    ):
        with get_session() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise HTTPException(404, "岗位不存在")
            final_note = "；".join(part for part in (note.strip(), extra_note.strip()) if part)
            try:
                job = review_job(session, job_id, action, final_note, "本地管理员")
            except ValueError as exc:
                return render_job_detail(
                    request,
                    session,
                    job,
                    f"暂不能通过：{exc}。请补齐后再次提交。",
                    "error",
                )
            action_feedback = {
                "approve": "审核通过，已进入可发布状态。现在可以生成公众号和群消息草稿。",
                "return": "已退回待核验。请按选择的原因补齐信息后再提交审核。",
                "reject": "已淘汰该记录，不会进入内容队列。",
            }
            return render_job_detail(request, session, job, action_feedback[action], "success")

    @app.post("/jobs/{job_id}/distribution")
    def distribution(job_id: int):
        with get_session() as session:
            try:
                create_distribution_items(session, job_id)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        return RedirectResponse("/queues", status_code=303)

    @app.get("/queues")
    def queues(request: Request):
        with get_session() as session:
            items = session.scalars(select(DistributionItem).order_by(DistributionItem.id.desc())).all()
            return templates.TemplateResponse(
                request,
                "queues.html",
                page_context(session, "queues", items=items),
            )

    @app.get("/distribution/{item_id}/wechat")
    def wechat_draft(request: Request, item_id: int, ai_feedback: str = "", ai_message: str = ""):
        with get_session() as session:
            item = session.get(DistributionItem, item_id)
            if item is None or item.channel != "公众号":
                raise HTTPException(404, "公众号草稿不存在")
            job = session.get(Job, item.job_id)
            if job is None:
                raise HTTPException(404, "对应岗位不存在")
            group_item = session.scalar(
                select(DistributionItem).where(
                    DistributionItem.job_id == job.id,
                    DistributionItem.channel == "微信群",
                )
            )
            content_draft = parse_content_draft(item.ai_content_json, job) if item.ai_content_json else None
            return templates.TemplateResponse(
                request,
                "wechat_draft.html",
                page_context(
                    session,
                    "queues",
                    item=item,
                    job=job,
                    draft=build_wechat_draft(job, content_draft),
                    group_message=group_item.content if group_item else "微信群消息尚未生成",
                    ai_feedback=ai_feedback,
                    ai_message=ai_message,
                ),
            )

    @app.post("/distribution/{item_id}/wechat/ai-content")
    def refine_wechat_draft_with_ai(item_id: int):
        with get_session() as session:
            item = session.get(DistributionItem, item_id)
            if item is None or item.channel != "公众号":
                raise HTTPException(404, "公众号草稿不存在")
            job = session.get(Job, item.job_id)
            if job is None:
                raise HTTPException(404, "对应岗位不存在")
            try:
                content = get_ai_settings_service().complete_text(session, build_content_prompt(job))
                draft = parse_content_draft(content, job)
                if not any((draft.company_intro, draft.role_summary, draft.eligibility, draft.career_advice, draft.apply_tip)):
                    raise ValueError("AI 返回内容没有通过事实校验，已保留基础稿")
                item.ai_content_json = content
                item.ai_content_status = "AI 已提炼"
                item.ai_content_error = ""
                session.commit()
                feedback, message = "success", "AI 内容已提炼并套入固定公众号模板，请检查后复制。"
            except Exception:
                item.ai_content_status = "基础稿"
                item.ai_content_error = "AI 提炼未完成，已保留基础稿。请检查当前模型、额度或网络后再试。"
                session.commit()
                feedback, message = "error", item.ai_content_error
        return RedirectResponse(
            url=f"/distribution/{item_id}/wechat?{urlencode({'ai_feedback': feedback, 'ai_message': message})}",
            status_code=303,
        )

    return app


app = create_app()
