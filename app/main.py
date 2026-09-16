from pathlib import Path
import json
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy import case, func, or_, select, text
from sqlalchemy.orm import Session

from app.database import create_database, create_session_factory
from app.config import Settings, load_settings
from app.auth.service import AuthenticationError, authenticate, change_password, create_user, csrf_valid, issue_session, reset_password, resolve_session, revoke_user_sessions
from app.auth.permissions import PermissionDenied, require_permission
from app.models import (
    DistributionItem, Job, ReviewLog, Source, SourceDiagnostic, SourceTrialRun,
    SourceTrialSample, TaskRun, WorkItem,
)
from app.services.ai_settings import (
    AiSettingsService,
    CredentialNotConfiguredError,
    OPENAI_COMPATIBLE_PROVIDER,
    OpenAICompatibleClient,
    TextProviderNotReadyError,
    WindowsCredentialStore,
)
from app.services.credentials import SecretProvider
from app.services.outbound_policy import validate_public_http_url
from app.services.health import cloud_schema_is_ready
from app.services.concurrency import EditConflict
from app.services.audit import record_event
from app.services.work_queue import enqueue
from app.services.collection_strategy import build_collection_plan
from app.services.source_library import monitoring_message, summarize_source_library
from app.services.source_admission import (
    AdmissionError, approve_source_admission, enqueue_source_trial,
    evaluate_source_admission,
)
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
from app.services.tasks import mark_interrupted_task_runs, run_demo_collection
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


def create_app(database_url: str | None = None, *, settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    database_url = database_url or (settings.database_url if settings.is_cloud else DEFAULT_DATABASE_URL)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    app = FastAPI(
        title="招聘内容运营后台",
        docs_url=None,
        redoc_url=None,
        openapi_url=None if settings.is_cloud else "/openapi.json",
    )
    if settings.is_cloud and settings.allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    is_cloud_runtime = settings.is_cloud and database_url == settings.database_url
    session_factory = create_session_factory(database_url) if is_cloud_runtime else create_database(database_url)
    if not is_cloud_runtime:
        with session_factory() as session:
            seed_demo_data(session)
            ensure_official_source_catalog(session)
            backfill_unscreened_intake_jobs(session)
            mark_interrupted_task_runs(session)
            expire_known_deadline_jobs(session)
            return_unsafe_publishable_jobs(session)
    app.state.session_factory = session_factory
    app.state.settings = settings
    if settings.is_cloud or settings.secret_backend == "file":
        secret_provider = SecretProvider(
            {
                "bailian": Path(settings.bailian_secret_file) if settings.bailian_secret_file else None,
                "openai_compatible": Path(settings.openai_compatible_secret_file)
                if settings.openai_compatible_secret_file else None,
            }
        )
        app.state.ai_settings_service = AiSettingsService(
            credential_store_factory=secret_provider.for_provider,
            openai_client=OpenAICompatibleClient(validate_outbound=validate_public_http_url),
        )
    else:
        app.state.ai_settings_service = AiSettingsService(WindowsCredentialStore())
    templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

    def get_session() -> Session:
        return app.state.session_factory()

    def get_ai_settings_service() -> AiSettingsService:
        return app.state.ai_settings_service

    def safe_report_link(value: str) -> str:
        value = (value or "").strip()
        try:
            parsed = urlsplit(value)
        except ValueError:
            return ""
        return value if parsed.scheme.lower() in {"http", "https"} and parsed.netloc else ""

    def required_action_for_request(request: Request) -> str:
        path = request.url.path
        if path.startswith("/users"):
            return "accounts.manage"
        if path.startswith("/settings/"):
            return "settings.manage"
        if path.startswith("/sources/") and path.endswith("/admission"):
            return "sources.approve"
        if path.startswith("/sources/") and path.endswith("/trials") and request.method == "POST":
            return "task.submit"
        if path.startswith("/distribution/") and path.endswith("/wechat"):
            return "distribution.preview"
        if path.startswith("/jobs/") and path.endswith("/review"):
            return "review.decide"
        if path.startswith("/tasks/"):
            return "business.read" if request.method == "GET" else "task.submit"
        if path.endswith("/suggest-batch"):
            return "task.submit"
        if request.method == "GET":
            return "business.read"
        return "business.edit"

    @app.middleware("http")
    async def require_cloud_session(request: Request, call_next):
        if not settings.is_cloud:
            return await call_next(request)
        path = request.url.path
        public_paths = {"/login", "/health/live", "/health/ready"}
        if path in public_paths or path.startswith("/static/"):
            return await call_next(request)
        raw_token = request.cookies.get("recruiting_session", "")
        try:
            with get_session() as session:
                request.state.current_user = resolve_session(session, raw_token)
        except AuthenticationError:
            if request.headers.get("accept", "").startswith("application/json"):
                return JSONResponse({"detail": "需要登录"}, status_code=401)
            return RedirectResponse(url="/login", status_code=303)
        request.state.request_id = secrets.token_urlsafe(12)
        if request.state.current_user.must_change_password and path != "/account/password":
            return RedirectResponse(url="/account/password", status_code=303)
        try:
            require_permission(request.state.current_user, required_action_for_request(request))
        except PermissionDenied:
            if request.headers.get("accept", "").startswith("application/json"):
                return JSONResponse({"detail": "没有操作权限"}, status_code=403)
            return JSONResponse({"detail": "没有操作权限"}, status_code=403)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            csrf_token = request.headers.get("X-CSRF-Token", "")
            if not csrf_token:
                await request.body()
                form = await request.form()
                csrf_token = str(form.get("csrf_token", ""))
            with get_session() as session:
                is_valid_csrf = csrf_valid(session, raw_token, csrf_token)
            if not is_valid_csrf:
                return JSONResponse({"detail": "CSRF 验证失败"}, status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def add_cloud_security_headers(request: Request, call_next):
        response = await call_next(request)
        if settings.is_cloud:
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "same-origin"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'self'; form-action 'self'"
            )
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    @app.get("/login")
    def login_page(request: Request):
        if not settings.is_cloud:
            return RedirectResponse(url="/", status_code=303)
        csrf_token = request.cookies.get("recruiting_login_csrf") or secrets.token_urlsafe(32)
        response = templates.TemplateResponse(request, "login.html", {"csrf_token": csrf_token})
        response.set_cookie("recruiting_login_csrf", csrf_token, secure=True, samesite="lax", max_age=600)
        return response

    @app.get("/health/live")
    def health_live():
        return {"status": "live"}

    @app.get("/health/ready")
    def health_ready():
        try:
            with get_session() as session:
                session.execute(text("SELECT 1"))
                if is_cloud_runtime and not cloud_schema_is_ready(session):
                    return JSONResponse({"status": "not_ready", "reason": "schema_not_migrated"}, status_code=503)
        except Exception:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return {"status": "ready", "version": settings.app_version}

    @app.get("/users")
    def users(request: Request, feedback: str = ""):
        with get_session() as session:
            from app.models import User
            accounts = session.scalars(select(User).order_by(User.id)).all()
            return templates.TemplateResponse(
                request,
                "users.html",
                page_context(session, "users", accounts=accounts, feedback=feedback),
            )

    @app.post("/users")
    def create_invited_user(
        request: Request,
        username: str = Form(),
        display_name: str = Form(),
        temporary_password: str = Form(),
        can_review: str | None = Form(None),
    ):
        with get_session() as session:
            try:
                account = create_user(
                    session,
                    username,
                    temporary_password,
                    display_name,
                    can_review=can_review == "on",
                    must_change_password=True,
                )
                _record_account_audit(session, request, "account.created", account, {
                    "role": account.role, "can_review": account.can_review, "must_change_password": True,
                })
            except ValueError as exc:
                return RedirectResponse(url=f"/users?{urlencode({'feedback': str(exc)})}", status_code=303)
        return RedirectResponse(url=f"/users?{urlencode({'feedback': f'已创建账号：{account.username}；请通过团队私密渠道交付临时密码。'})}", status_code=303)

    @app.post("/users/{user_id}/disable")
    def disable_user(request: Request, user_id: int):
        with get_session() as session:
            from app.models import User

            account = session.get(User, user_id)
            if account is None:
                raise HTTPException(404, "账号不存在")
            if account.role == "admin" and account.is_active:
                active_admins = session.scalar(
                    select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True))
                ) or 0
                if active_admins <= 1:
                    return RedirectResponse(
                        url=f"/users?{urlencode({'feedback': '至少保留一名启用中的管理员'})}", status_code=303
                    )
            account.is_active = False
            revoke_user_sessions(session, account.id)
            _record_account_audit(session, request, "account.disabled", account, {"is_active": False})
        return RedirectResponse(
            url=f"/users?{urlencode({'feedback': f'已停用账号：{account.username}'})}", status_code=303
        )

    @app.post("/users/{user_id}/enable")
    def enable_user(request: Request, user_id: int):
        with get_session() as session:
            from app.models import User

            account = session.get(User, user_id)
            if account is None:
                raise HTTPException(404, "账号不存在")
            account.is_active = True
            session.commit()
            _record_account_audit(session, request, "account.enabled", account, {"is_active": True})
        return RedirectResponse(
            url=f"/users?{urlencode({'feedback': f'已重新启用账号：{account.username}'})}", status_code=303
        )

    @app.post("/users/{user_id}/reset-password")
    def reset_user_password(request: Request, user_id: int, temporary_password: str = Form()):
        with get_session() as session:
            from app.models import User

            account = session.get(User, user_id)
            if account is None:
                raise HTTPException(404, "账号不存在")
            try:
                reset_password(session, account, temporary_password)
                _record_account_audit(
                    session, request, "account.password_reset", account, {"must_change_password": True}
                )
            except ValueError as exc:
                return RedirectResponse(url=f"/users?{urlencode({'feedback': str(exc)})}", status_code=303)
        return RedirectResponse(
            url=f"/users?{urlencode({'feedback': f'已重置临时密码：{account.username}；请通过私密渠道交付。'})}", status_code=303
        )

    def _record_account_audit(
        session: Session, request: Request, action: str, account, changes: dict[str, object]
    ) -> None:
        actor = getattr(request.state, "current_user", None)
        record_event(
            session,
            actor_user_id=actor.id if actor else None,
            actor_label=actor.display_name if actor else "本地管理员",
            action=action,
            entity_type="user",
            entity_id=str(account.id),
            request_id=getattr(request.state, "request_id", ""),
            changes=changes,
        )
        session.commit()

    @app.get("/account/password")
    def password_change_page(request: Request):
        return templates.TemplateResponse(request, "change_password.html", {})

    @app.post("/account/password")
    def change_own_password(request: Request, new_password: str = Form(), confirm_password: str = Form()):
        if new_password != confirm_password:
            return RedirectResponse(url=f"/account/password?{urlencode({'feedback': '两次输入的密码不一致'})}", status_code=303)
        with get_session() as session:
            current_user = getattr(request.state, "current_user")
            user = session.get(type(current_user), current_user.id)
            try:
                change_password(session, user, new_password)
            except ValueError as exc:
                return RedirectResponse(url=f"/account/password?{urlencode({'feedback': str(exc)})}", status_code=303)
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie("recruiting_session")
        response.delete_cookie("recruiting_csrf")
        return response

    @app.post("/login")
    def login(request: Request, username: str = Form(...), password: str = Form(...), csrf_token: str = Form(...)):
        if not settings.is_cloud:
            return RedirectResponse(url="/", status_code=303)
        if not secrets.compare_digest(csrf_token, request.cookies.get("recruiting_login_csrf", "")):
            return JSONResponse({"detail": "CSRF 验证失败"}, status_code=403)
        with get_session() as session:
            try:
                user = authenticate(session, username, password)
            except AuthenticationError:
                return templates.TemplateResponse(request, "login.html", {"error": "账号或密码错误"}, status_code=401)
            raw_token, session_csrf_token = issue_session(session, user)
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            "recruiting_session",
            raw_token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=8 * 60 * 60,
        )
        response.set_cookie("recruiting_csrf", session_csrf_token, secure=True, samesite="lax", max_age=8 * 60 * 60)
        response.delete_cookie("recruiting_login_csrf")
        return response

    @app.post("/logout")
    def logout(request: Request):
        if settings.is_cloud:
            with get_session() as session:
                current_user = getattr(request.state, "current_user")
                revoke_user_sessions(session, current_user.id)
        response = RedirectResponse(url="/login" if settings.is_cloud else "/", status_code=303)
        response.delete_cookie("recruiting_session")
        response.delete_cookie("recruiting_csrf")
        return response

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
            "cloud_runtime": settings.is_cloud,
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

    @app.get("/tasks/{task_id}")
    def task_detail(request: Request, task_id: int):
        with get_session() as session:
            task = session.get(TaskRun, task_id)
            if task is None:
                raise HTTPException(404, "任务不存在")
            items = session.scalars(
                select(WorkItem).where(WorkItem.batch_id == task.id).order_by(WorkItem.id)
            ).all()
            status_counts = {
                status: sum(1 for item in items if item.status == status)
                for status in ("queued", "running", "succeeded", "needs_review")
            }
            return templates.TemplateResponse(
                request,
                "task_detail.html",
                page_context(session, "dashboard", task=task, items=items, status_counts=status_counts),
            )

    @app.get("/sources")
    def sources(
        request: Request, health_feedback: str = "", health_feedback_kind: str = ""
    ):
        with get_session() as session:
            all_sources = session.scalars(select(Source)).all()
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
            source_library_summary = summarize_source_library(
                all_sources, OFFICIAL_SOURCE_CATALOG
            )
            source_messages = {
                source.id: monitoring_message(source) for source in source_records
            }
            diagnostic_records = session.scalars(
                select(SourceDiagnostic)
                .where(SourceDiagnostic.source_id.in_([source.id for source in source_records]))
                .order_by(SourceDiagnostic.checked_at.desc(), SourceDiagnostic.id.desc())
            ).all()
            latest_diagnostics = {}
            for diagnostic in diagnostic_records:
                latest_diagnostics.setdefault(diagnostic.source_id, diagnostic)
            diagnostic_labels = {
                "recruitment_list": "已找到招聘列表",
                "detail_verified": "已验证公告详情",
                "wrong_entry": "入口不是招聘列表",
                "dynamic_or_unverified": "动态内容待验证",
                "blocked": "官网访问受限",
                "no_openings": "未发现开放岗位",
                "not_checked": "未验证招聘内容",
            }
            trial_records = session.scalars(
                select(SourceTrialRun)
                .where(SourceTrialRun.source_id.in_([source.id for source in source_records]))
                .order_by(SourceTrialRun.finished_at.desc(), SourceTrialRun.id.desc())
            ).all()
            latest_trials = {}
            for trial in trial_records:
                latest_trials.setdefault(trial.source_id, trial)
            admission_results = {
                source.id: evaluate_source_admission(session, source.id)
                for source in source_records if source.library_tier == "B"
            }
            trial_exclusions = {}
            for source_id, trial in latest_trials.items():
                try:
                    trial_exclusions[source_id] = json.loads(trial.exclusion_summary or "{}")
                except (TypeError, json.JSONDecodeError):
                    trial_exclusions[source_id] = {}
            return templates.TemplateResponse(
                request,
                "sources.html",
                page_context(
                    session,
                    "sources",
                    sources=source_records,
                    source_plans=source_plans,
                    tier_summaries=tier_summaries,
                    source_library_summary=source_library_summary,
                    source_messages=source_messages,
                    latest_diagnostics=latest_diagnostics,
                    diagnostic_labels=diagnostic_labels,
                    latest_trials=latest_trials,
                    admission_results=admission_results,
                    trial_exclusions=trial_exclusions,
                    current_user=getattr(request.state, "current_user", None),
                    health_feedback=health_feedback,
                    health_feedback_kind=health_feedback_kind,
                ),
            )

    @app.get("/sources/{source_id}/trials")
    def source_trial_report(request: Request, source_id: int):
        with get_session() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise HTTPException(404, "来源不存在")
            runs = session.scalars(
                select(SourceTrialRun)
                .where(SourceTrialRun.source_id == source_id)
                .order_by(SourceTrialRun.started_at.desc(), SourceTrialRun.id.desc())
            ).all()
            run_ids = [run.run_id for run in runs]
            samples = (
                session.scalars(
                    select(SourceTrialSample)
                    .where(SourceTrialSample.run_id.in_(run_ids))
                    .order_by(SourceTrialSample.id.desc())
                ).all()
                if run_ids else []
            )
            sample_reason_codes = {}
            sample_source_links = {}
            sample_application_links = {}
            for sample in samples:
                try:
                    sample_reason_codes[sample.id] = json.loads(sample.reason_codes or "[]")
                except (TypeError, json.JSONDecodeError):
                    sample_reason_codes[sample.id] = ["INVALID_REASON_DATA"]
                sample_source_links[sample.id] = safe_report_link(sample.source_url)
                sample_application_links[sample.id] = safe_report_link(
                    sample.application_value
                )
            return templates.TemplateResponse(
                request,
                "source_trials.html",
                page_context(
                    session, "sources", source=source, runs=runs, samples=samples,
                    sample_reason_codes=sample_reason_codes,
                    sample_source_links=sample_source_links,
                    sample_application_links=sample_application_links,
                    admission=evaluate_source_admission(session, source_id),
                    current_user=getattr(request.state, "current_user", None),
                ),
            )

    @app.post("/sources/{source_id}/trials")
    def start_source_trial(request: Request, source_id: int):
        with get_session() as session:
            source = session.get(Source, source_id)
            if source is None:
                raise HTTPException(404, "来源不存在")
            actor = getattr(request.state, "current_user", None)
            try:
                item = enqueue_source_trial(session, source, actor.id if actor else None)
                session.commit()
            except AdmissionError as exc:
                return RedirectResponse(
                    f"/sources?{urlencode({'health_feedback': str(exc), 'health_feedback_kind': 'error'})}",
                    status_code=303,
                )
        return RedirectResponse(f"/tasks/{item.batch_id}", status_code=303)

    @app.post("/sources/{source_id}/admission")
    def approve_source(
        request: Request,
        source_id: int,
        expected_version: str = Form(...),
        expected_rule_version: str = Form(...),
    ):
        with get_session() as session:
            actor = getattr(request.state, "current_user", None)
            try:
                source = approve_source_admission(
                    session, source_id, actor_id=actor.id if actor else None,
                    expected_version=expected_version,
                    expected_rule_version=expected_rule_version,
                    request_id=getattr(request.state, "request_id", ""),
                )
            except AdmissionError as exc:
                return RedirectResponse(
                    f"/sources?{urlencode({'health_feedback': str(exc), 'health_feedback_kind': 'error'})}",
                    status_code=303,
                )
        return RedirectResponse(
            f"/sources?{urlencode({'health_feedback': f'{source.name} 已通过验收并启用', 'health_feedback_kind': 'success'})}",
            status_code=303,
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
        from app.services.source_diagnostics import diagnose_source, save_diagnostic

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
                checked_at = __import__("datetime").datetime.now()
                result = diagnose_source(source, client, checked_at)
                save_diagnostic(session, source, result, checked_at)
            session.commit()
        return sources(request, result.message, "success" if result.connection_status == "ok" else "error")

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
                    cloud_managed_secrets=settings.is_cloud,
                ),
            )

    @app.post("/settings/ai/key")
    def save_ai_key(api_key: str = Form()):
        if settings.is_cloud:
            raise HTTPException(status_code=403, detail="云端模型密钥由部署端受控 Secret 文件管理")
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
        if settings.is_cloud:
            raise HTTPException(status_code=403, detail="云端模型密钥由部署端受控 Secret 文件管理")
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
        score_status: str = "",
        query: str = "",
        collected_from: str = "",
        collected_to: str = "",
        classification_feedback: str = "",
        scoring_feedback: str = "",
    ):
        with get_session() as session:
            if data_type not in {"real", "demo", "all"}:
                raise HTTPException(400, "数据属性筛选无效")
            statement = select(Job)
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
            if score_status:
                if score_status not in {"待建议", "处理中", "AI建议", "规则建议", "不适用", "失败"}:
                    raise HTTPException(400, "建议状态筛选无效")
                statement = statement.where(Job.ai_score_status == score_status)
            try:
                collected_from_at = (
                    datetime.strptime(collected_from, "%Y-%m-%d") if collected_from else None
                )
                collected_to_at = (
                    datetime.strptime(collected_to, "%Y-%m-%d") + timedelta(days=1)
                    if collected_to
                    else None
                )
            except ValueError as exc:
                raise HTTPException(400, "拉取日期必须为 YYYY-MM-DD") from exc
            if collected_from_at:
                statement = statement.where(Job.collected_at >= collected_from_at)
            if collected_to_at:
                statement = statement.where(Job.collected_at < collected_to_at)
            normalized_query = query.strip()
            if normalized_query:
                statement = statement.where(
                    or_(
                        Job.employer_name.contains(normalized_query),
                        Job.job_title.contains(normalized_query),
                    )
                )
            statement = statement.order_by(
                Job.status != "待核验",
                case((Job.intake_grade == "A", 0), (Job.intake_grade == "B", 1), (Job.intake_grade == "C", 2), else_=3),
                Job.ai_suggested_score.desc(),
                Job.collected_at.desc(),
                Job.id.desc(),
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
            score_queue_counts = {
                value: session.scalar(select(func.count()).select_from(Job).where(
                    Job.is_demo.is_(False), Job.status == "待核验", Job.ai_score_status == value
                )) or 0
                for value in ("待建议", "处理中", "AI建议", "规则建议", "不适用", "失败")
            }
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
                    selected_score_status=score_status,
                    collected_from=collected_from,
                    collected_to=collected_to,
                    score_queue_counts=score_queue_counts,
                    suggested_new_recruitment_count=suggested_new_recruitment_count,
                    suggested_score_candidate_count=suggested_score_candidate_count,
                    classification_feedback=classification_feedback,
                    scoring_feedback=scoring_feedback,
                ),
            )

    @app.post("/jobs/scoring/suggest-batch")
    def suggest_scores_batch(request: Request):
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
            task_run = TaskRun(
                task_name="AI建议分批次",
                status="处理中",
                message=f"已锁定 {len(candidates)} 条待处理记录，正在逐条生成建议分。",
            )
            session.add(task_run)
            session.commit()
            if settings.is_cloud:
                actor = getattr(request.state, "current_user")
                for job in candidates:
                    job.ai_score_status = "处理中"
                    job.ai_score_reason = f"批次 #{task_run.id} 已入队，等待后台 worker 处理。"
                    enqueue(
                        session,
                        kind="score_job",
                        target_type="job",
                        target_id=job.id,
                        expected_row_version=job.row_version,
                        expected_fact_version=job.version,
                        config_revision=settings.secret_revision,
                        template_version="",
                        dedupe_key=f"score_job:{job.id}:{job.row_version}:{job.version}:{settings.secret_revision}",
                        requested_by=actor.id,
                        batch_id=task_run.id,
                    )
                task_run.status = "已入队"
                task_run.message = f"已将 {len(candidates)} 条记录加入后台建议分队列。"
                session.commit()
                return RedirectResponse(f"/tasks/{task_run.id}", status_code=303)
            complete = configured_intake_ai_complete(session)
            ai_count = 0
            rules_count = 0
            inapplicable_count = 0
            failed_count = 0
            for job in candidates:
                job.ai_score_status = "处理中"
                job.ai_score_reason = f"批次 #{task_run.id} 正在处理。"
                session.commit()
                try:
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
                    elif result.status == "不适用":
                        inapplicable_count += 1
                    session.add(
                        ReviewLog(
                            job_id=job.id,
                            action="AI建议分生成",
                            note=(
                                f"批次 #{task_run.id}；建议分：{result.score}；"
                                f"状态：{result.status}；理由：{result.reason}"
                            ),
                            operator_name="本地管理员",
                        )
                    )
                except Exception as exc:
                    failed_count += 1
                    job.ai_score_status = "失败"
                    job.ai_score_reason = "建议分生成失败，请检查模型配置、网络或该公告原文后重试。"
                    job.ai_score_breakdown = "{}"
                    job.ai_score_confidence = "低"
                    job.ai_scored_at = datetime.now()
                    session.add(
                        ReviewLog(
                            job_id=job.id,
                            action="AI建议分失败",
                            note=f"批次 #{task_run.id}：{str(exc)[:200]}",
                            operator_name="本地管理员",
                        )
                    )
                session.commit()
            task_run.status = "完成" if failed_count == 0 else "部分完成"
            task_run.message = (
                f"本批处理 {len(candidates)} 条：AI 建议 {ai_count} 条，规则建议 {rules_count} 条，"
                f"不适用 {inapplicable_count} 条，失败 {failed_count} 条。"
            )
            session.commit()
        message = f"批次 #{task_run.id}：{task_run.message}"
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
        employer_name: str = Form(""),
        announcement_title: str = Form(""),
        job_title: str = Form(""),
        job_family: str = Form(""),
        recruitment_type: str = Form(""),
        location_category: str = Form(""),
        location_detail: str = Form(""),
        target_audience: str = Form(""),
        direction_tags: str = Form(""),
        deadline: str = Form(""),
        official_url: str = Form(""),
        posting_scope: str = Form("single_role"),
        attachment_status: str = Form("not_required"),
        application_method: str = Form("official_page"),
        application_contact: str = Form(""),
        quality_score: str = Form(""),
        note: str = Form(""),
        student_fit_level: str = Form("待人工判断"),
        distribution_recommendation: str = Form("仅保留资料库"),
        ai_rationale: str = Form(""),
        ai_confidence: str = Form("低"),
        source_checked: str | None = Form(None),
        scope_checked: str | None = Form(None),
        audience_checked: str | None = Form(None),
        location_checked: str | None = Form(None),
        application_checked: str | None = Form(None),
        timeliness_checked: str | None = Form(None),
        expected_row_version: int = Form(0),
    ):
        try:
            normalized_quality_score = int(quality_score) if quality_score.strip() else -1
        except ValueError:
            normalized_quality_score = -1
        structuring_input = StructuringInput(
            employer_name=employer_name,
            announcement_title=announcement_title,
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
            quality_score=normalized_quality_score,
            note=note,
            student_fit_level=student_fit_level,
            distribution_recommendation=distribution_recommendation,
            ai_rationale=ai_rationale,
            ai_confidence=ai_confidence,
            verification_checks={
                "source_checked": source_checked == "on", "scope_checked": scope_checked == "on",
                "audience_checked": audience_checked == "on", "location_checked": location_checked == "on",
                "application_checked": application_checked == "on", "timeliness_checked": timeliness_checked == "on",
            },
        )
        with get_session() as session:
            try:
                structure_job(
                    session,
                    job_id,
                    structuring_input,
                    getattr(request.state, "current_user", None).display_name
                    if settings.is_cloud else "本地管理员",
                    expected_row_version=expected_row_version if settings.is_cloud else None,
                )
            except EditConflict as exc:
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
                        field_errors={},
                        submission_feedback="error",
                        submission_message=str(exc),
                    ),
                    status_code=409,
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
                actor = getattr(request.state, "current_user", None)
                job = review_job(
                    session,
                    job_id,
                    action,
                    final_note,
                    actor.display_name if actor else "本地管理员",
                    actor_user_id=actor.id if actor else None,
                )
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
            if item.job_version != job.version:
                return templates.TemplateResponse(
                    request,
                    "operation_error.html",
                    page_context(
                        session,
                        "queues",
                        title="公众号草稿需要重新生成",
                        message="岗位事实已更新，旧草稿已失效。请先回到岗位详情完成最新审核，再重新生成内容。",
                        back_url=f"/jobs/{job.id}",
                    ),
                    status_code=409,
                )
            errors = validate_publishable(job)
            if errors:
                return templates.TemplateResponse(
                    request,
                    "operation_error.html",
                    page_context(
                        session,
                        "queues",
                        title="公众号草稿暂不可使用",
                        message="；".join(errors),
                        back_url=f"/jobs/{job.id}",
                    ),
                    status_code=409,
                )
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
            if item.job_version != job.version:
                return RedirectResponse(
                    url=f"/distribution/{item_id}/wechat?{urlencode({'ai_feedback': 'error', 'ai_message': '岗位事实已更新，旧草稿不能继续提炼；请重新审核并生成草稿。'})}",
                    status_code=303,
                )
            validation_errors = validate_publishable(job)
            if validation_errors:
                return RedirectResponse(
                    url=f"/distribution/{item_id}/wechat?{urlencode({'ai_feedback': 'error', 'ai_message': '；'.join(validation_errors)})}",
                    status_code=303,
                )
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
