"""Screenshot work area: routes own HTTP feedback; services own evidence and review."""

from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.auth.permissions import PermissionDenied, require_permission
from app.models import DistributionItem, Job, ScreenshotImport, ScreenshotAsset, ScreenshotPiece
from app.services.screenshot_contracts import ScreenshotError
from app.services.screenshot_storage import UploadedImage, cleanup_assets, resolve_storage_path, validate_and_store_images


FIELD_LABELS = {
    "employer_name":"招聘单位", "announcement_title":"公告标题", "job_title":"岗位名称",
    "recruitment_type":"招聘类型", "location_detail":"工作地点", "target_audience":"适合人群",
    "deadline":"报名截止日期", "official_url":"官方公告/报名链接", "application_contact":"投递邮箱/入口",
    "role_summary":"岗位职责", "eligibility":"申请条件", "salary":"薪资（原文明确时填写）",
    "job_family":"岗位方向", "direction_tags":"专业方向", "location_category":"地点分类",
    "student_fit_level":"学生适配", "distribution_recommendation":"分发建议", "application_method":"投递方式",
    "posting_scope":"公告范围", "attachment_status":"附件状态",
    "notice_type":"公告类型", "evidence":"提取事实依据", "confidence":"提取可信度",
    "rationale":"识别判断说明", "uncertainties":"识别不确定项",
}


def register_screenshot_routes(app, templates, page_context):
    factory = app.state.session_factory
    root = Path(app.state.settings.screenshot_upload_root).resolve()

    def actor(request):
        user = getattr(request.state, "current_user", None)
        if app.state.settings.is_cloud and user is None:
            raise HTTPException(401, "请先登录")
        return user

    def batch_by_id(session, public_id):
        batch = session.scalar(select(ScreenshotImport).where(ScreenshotImport.public_id == public_id))
        if batch is None:
            raise HTTPException(404, "截图任务不存在")
        return batch

    def facts_for(batch):
        from app.services.screenshot_extraction import facts_from_dict
        try:
            return facts_from_dict(json.loads(batch.fields_json))
        except (TypeError, ValueError, KeyError):
            return None

    def review_versions(batch, job):
        return {"expected_row_version": batch.row_version, "content_revision": batch.content_revision,
                "expected_job_row_version": job.row_version if job else None,
                "expected_job_version": job.version if job else None}

    def readiness(session, batch):
        from app.services.screenshot_workflow import guided_review_readiness
        issues, _, ocr_complete, deadline_unknown_allowed = guided_review_readiness(session, batch)
        job = session.get(Job, batch.job_id) if batch.job_id else None
        return {"issues": [issue.to_dict() for issue in issues], "versions": review_versions(batch, job),
                "ready": not any(issue.severity == "error" for issue in issues),
                "ocr_complete": ocr_complete, "deadline_unknown_allowed": deadline_unknown_allowed}

    def detail(request, session, batch, *, error="", feedback="", status_code=200, form_values=None):
        from app.services.distribution import build_wechat_draft, build_group_message, distribution_blockers
        from app.services.jobs import REQUIRED_VERIFICATION_CHECKS
        facts = facts_for(batch)
        preview_facts = facts
        if facts:
            from app.services.screenshot_workflow import guided_selected_import_facts
            try:
                preview_facts = guided_selected_import_facts(batch)
            except ValueError:
                pass
        values = {key: field.value for key, field in facts.fields.items()} if facts else {}
        values.update(form_values or {})
        summary_values = dict(values)
        if preview_facts and preview_facts.roles:
            titles = [role["job_title"].value.strip() for role in preview_facts.roles if "job_title" in role]
            summary_values["job_title"] = "、".join(dict.fromkeys(value for value in titles if value))
            for key in ("target_audience", "location_detail"):
                field = preview_facts.fields.get(key)
                summary_values[key] = field.value if field else ""
        assets = session.scalars(select(ScreenshotAsset).where(ScreenshotAsset.import_id == batch.id).order_by(ScreenshotAsset.position)).all()
        pieces = session.scalars(select(ScreenshotPiece).join(ScreenshotAsset).where(ScreenshotAsset.import_id == batch.id)).all()
        job = session.get(Job, batch.job_id) if batch.job_id else None
        display_values = {**values, **{key: summary_values.get(key, "")
            for key in ("job_title", "target_audience", "location_detail")}}
        preview_values = {key: display_values.get(key, "") for key in (
            "employer_name", "announcement_title", "job_title", "job_family", "recruitment_type", "location_category",
            "location_detail", "target_audience", "direction_tags", "deadline", "official_url", "application_contact")}
        preview = Job(**preview_values, fingerprint="internal-preview", evidence_text=batch.edited_text or batch.ocr_text,
                      source_url=batch.source_url, application_method=values.get("application_method") or "unknown",
                      posting_scope=values.get("posting_scope") or "single_role", quality_score=0, version=1)
        preview.confirmed_content_json = json.dumps({"schema_version":1,"import_id":batch.id,"content_revision":batch.content_revision,
            "fields":{**display_values,"source_url":batch.source_url,"source_name":batch.source_name},
            "roles":[{k:v.value for k,v in role.items()} for role in preview_facts.roles] if preview_facts else [],
            "common_requirements":[value.value for value in facts.common_requirements] if facts else [],"evidence_refs":[],
            "student_roles_excerpt":bool(facts and preview_facts and len(preview_facts.roles)<len(facts.roles))},ensure_ascii=False)
        preview.confirmed_content_version = 1
        try:
            draft = build_wechat_draft(preview, mode="internal")
            group_message = build_group_message(preview, mode="internal")
        except (ValueError, TypeError, KeyError):
            draft = None
            group_message = "完成识别或补充信息后显示群消息预览。"
        item = session.scalar(select(DistributionItem).where(DistributionItem.job_id == job.id, DistributionItem.channel == "公众号")) if job else None
        formal_ready = bool(job and item and not distribution_blockers(job,item))
        current = actor(request)
        can_review = current is None or current.role == "admin" or current.can_review
        current_models = {}
        for kind in ("ocr", "text"):
            snapshot = getattr(app.state.ai_settings_service, kind + "_config_snapshot", None)
            try:
                configuration = snapshot(session) if callable(snapshot) else {}
                model = configuration.get("model") if isinstance(configuration, dict) else None
                current_models[kind] = model if isinstance(model, str) and 0 < len(model) <= 200 else "暂不可用"
            except Exception:
                current_models[kind] = "暂不可用"
        prefill_summary = {"filled": 0, "unresolved": 0, "role_unresolved": 0, "common_unresolved": 0}
        if facts:
            from app.services.screenshot_prefill import has_common_recovery_blockers
            from app.services.screenshot_review import DIAGNOSTIC_FIELDS
            factual_fields = [field for key, field in facts.fields.items() if key not in DIAGNOSTIC_FIELDS]
            prefill_summary.update(filled=sum(bool(field.value) for field in factual_fields),
                unresolved=sum(field.state in {"unclear", "conflict"} for field in factual_fields),
                role_unresolved=sum(field.state in {"unclear", "conflict"} for role in facts.roles for key, field in role.items() if key not in DIAGNOSTIC_FIELDS),
                common_unresolved=sum(field.state != "explicit" or not field.value for field in facts.common_requirements))
            if has_common_recovery_blockers(facts):
                prefill_summary["common_unresolved"] = max(1, prefill_summary["common_unresolved"])
        review_state = readiness(session, batch)
        return templates.TemplateResponse(request, "screenshot_review.html", page_context(session,"screenshots",
            batch=batch, assets=assets, pieces=pieces, facts=facts, values=values, summary_values=summary_values, field_labels=FIELD_LABELS,
            job=job, item=item, draft=draft, group_message=group_message, error=error, feedback=feedback,
            verification_checks=REQUIRED_VERIFICATION_CHECKS, can_review=can_review,
            current_models=current_models, prefill_summary=prefill_summary, guided_review=True,
            review_issues=review_state["issues"], readiness_versions=review_state["versions"],
            ocr_complete=review_state["ocr_complete"], deadline_unknown_allowed=review_state["deadline_unknown_allowed"],
            formal_ready=formal_ready), status_code=status_code)

    @app.get("/imports/screenshots")
    def upload_page(request: Request, source_url: str = ""):
        with factory() as session:
            recent = session.scalars(select(ScreenshotImport).order_by(ScreenshotImport.id.desc()).limit(20)).all()
            return templates.TemplateResponse(request,"screenshot_import.html",page_context(session,"screenshots",
                recent=recent, submission_key=uuid4().hex, source_url=source_url,
                notice="微信要求完成验证，原文链接已保留。请上传截图或补充正文继续整理。" if request.query_params.get("reason")=="wechat_verification" else ""))

    @app.post("/imports/screenshots")
    async def upload(request: Request):
        current = actor(request)
        if current:
            require_permission(current, "business.edit")
            require_permission(current, "task.submit")
        form = await request.form()
        key = str(form.get("submission_key") or uuid4().hex)
        if len(key) > 80:
            return JSONResponse({"error":"提交标识无效，请刷新后重试"},status_code=400)
        with factory() as session:
            existing = session.scalar(select(ScreenshotImport).where(ScreenshotImport.submission_key == key))
            if existing:
                return RedirectResponse(f"/imports/screenshots/{existing.public_id}",status_code=303)
        uploads = form.getlist("images")
        prepared = []
        committed = False
        try:
            prepared = validate_and_store_images([UploadedImage(file.filename, file.file) for file in uploads if getattr(file,"filename",None)], root=root)
            ai = app.state.ai_settings_service
            with factory() as settings_session:
                ocr_ready = ai.is_ocr_provider_ready(settings_session)
            with factory() as session:
                supplemental = str(form.get("supplemental_text") or "")
                source_name = str(form.get("source_name") or "")
                source_url = str(form.get("source_url") or "")
                if len(supplemental)>24000 or len(source_name)>160 or len(source_url)>500:
                    raise ScreenshotError("invalid_input","补充正文或来源信息过长，请缩短后重试")
                if source_url:
                    from urllib.parse import urlsplit
                    parsed = urlsplit(source_url)
                    if parsed.scheme not in {"http","https"} or not parsed.netloc:
                        raise ScreenshotError("invalid_source_url","原文链接需要是有效的 HTTP(S) 地址")
                batch = ScreenshotImport(submission_key=key, source_name=source_name, source_url=source_url,
                                         supplemental_text=supplemental, created_by_user_id=current.id if current else None)
                session.add(batch)
                session.flush()
                for image in prepared:
                    data = asdict(image)
                    parts = data.pop("pieces")
                    data["position"] = data.pop("order")
                    asset = ScreenshotAsset(import_id=batch.id, **data)
                    session.add(asset)
                    session.flush()
                    for piece in parts:
                        piece.pop("byte_size",None)
                        piece.pop("base64_size",None)
                        session.add(ScreenshotPiece(asset_id=asset.id, **piece))
                session.flush()
                if ocr_ready:
                    from app.services.screenshot_tasks import enqueue_screenshot_import
                    try:
                        with session.begin_nested():
                            enqueue_screenshot_import(session,batch.id,ai_service=ai,requested_by=current.id if current else None,
                                                      queue_limit=app.state.settings.task_queue_limit)
                    except ScreenshotError as exc:
                        batch.status = "uploaded"
                        batch.error_code, batch.error_message = exc.code, str(exc)
                else:
                    batch.error_code = "ocr_not_configured"
                    batch.error_message = "图片已保存。请在 AI 模型配置中启用并配置图片识别，再点击重试。"
                session.commit()
                committed = True
                url = f"/imports/screenshots/{batch.public_id}"
        except IntegrityError:
            cleanup_assets(prepared,root=root)
            with factory() as session:
                existing = session.scalar(select(ScreenshotImport).where(ScreenshotImport.submission_key == key))
                if existing:
                    return RedirectResponse(f"/imports/screenshots/{existing.public_id}",status_code=303)
            return JSONResponse({"error":"重复图片或提交冲突，请刷新后重试"},status_code=409)
        except (ScreenshotError, ValueError) as exc:
            cleanup_assets(prepared,root=root)
            return JSONResponse({"error":str(exc)},status_code=400)
        except Exception:
            # Compensate only this upload. A commit with an uncertain outcome is
            # resolved by submission key before deleting any possibly bound file.
            safe_to_clean = not committed
            try:
                with factory() as session:
                    recovered = session.scalar(select(ScreenshotImport).where(ScreenshotImport.submission_key==key))
                    if recovered is not None:
                        return RedirectResponse(f"/imports/screenshots/{recovered.public_id}",status_code=303)
            except Exception:
                safe_to_clean = False
            if safe_to_clean:
                cleanup_assets(prepared,root=root)
            return JSONResponse({"error":"上传暂未完成，请保留原图后重试；如已建立任务，可在最近任务中继续。"},status_code=503)
        return RedirectResponse(url,status_code=303)

    @app.get("/imports/screenshots/{public_id}")
    def review_page(request: Request, public_id: str):
        with factory() as session:
            return detail(request,session,batch_by_id(session,public_id))

    @app.get("/imports/screenshots/{public_id}/status")
    def status(request: Request, public_id: str):
        with factory() as session:
            batch = batch_by_id(session,public_id)
            pieces = session.scalars(select(ScreenshotPiece).join(ScreenshotAsset).where(ScreenshotAsset.import_id==batch.id)).all()
            message = batch.error_message
            if batch.status=="queued" and (datetime.now()-batch.updated_at).total_seconds()>60:
                message = "识别任务尚未开始，请检查后台执行器是否运行。"
            return {"status":batch.status,"completed":sum(p.status=="succeeded" for p in pieces),"total":len(pieces),
                    "error":message,"row_version":batch.row_version,"content_revision":batch.content_revision}

    @app.get("/imports/screenshots/{public_id}/readiness")
    def review_readiness(request: Request, public_id: str):
        actor(request)
        with factory() as session:
            return JSONResponse(readiness(session, batch_by_id(session, public_id)), headers={"Cache-Control": "no-store"})

    @app.get("/imports/screenshots/{public_id}/assets/{asset_id}")
    def image(public_id: str, asset_id: int):
        with factory() as session:
            batch = batch_by_id(session,public_id)
            asset = session.get(ScreenshotAsset,asset_id)
            if asset is None or asset.import_id != batch.id:
                raise HTTPException(404,"图片不存在")
            path = resolve_storage_path(root,asset.storage_key)
            if not path.is_file():
                raise HTTPException(404,"图片文件不可用，请检查上传目录")
            return FileResponse(path,media_type=asset.mime_type,headers={"Cache-Control":"private, no-store"})

    def versions(form):
        def integer(name):
            value = form.get(name)
            return int(value) if value not in {None,""} else None
        expected = integer("expected_row_version")
        if expected is None:
            raise ValueError("页面版本缺失，请刷新后重试")
        return {"expected_row_version":expected,"expected_job_row_version":integer("expected_job_row_version"),
                "expected_job_version":integer("expected_job_version")}

    def edits(form):
        data = {key:str(form.get(key) or "") for key in ("source_name","source_url","title","edited_text","supplemental_text") if key in form}
        data["fields"] = {key:str(form.get("field_"+key) or "") for key in FIELD_LABELS if "field_"+key in form}
        data["field_evidence"] = {key:str(form.get("field_evidence_"+key) or "") for key in FIELD_LABELS if form.get("field_evidence_"+key)}
        data["material_complete"] = form.get("material_complete")=="on"
        if form.get("review_mode") == "guided":
            # Diagnostics are display-only in the guided page, even for crafted posts.
            for key in ("evidence", "confidence", "rationale", "uncertainties"):
                data["fields"].pop(key, None)
                data["field_evidence"].pop(key, None)
            if "material_complete" not in form:
                data.pop("material_complete")
        data["resolved_missing_fields"] = list(form.getlist("resolved_missing_fields"))
        if "selected_roles" in form:
            data["selected_roles"] = [int(value) for value in form.getlist("selected_roles")]
        for prefix, target in (("role_", "role_fields"), ("role_evidence_", "role_evidence")):
            result = {}
            for name, value in form.multi_items():
                if not name.startswith(prefix):
                    continue
                rest = name[len(prefix):]
                index, separator, field = rest.partition("_")
                if separator and index.isdigit() and field in {"job_title","eligibility","role_summary","salary","target_audience","location_detail"}:
                    if prefix == "role_evidence_" and not str(value).strip():
                        continue
                    result.setdefault(index,{})[field] = str(value)
            if result:
                data[target] = result
        for prefix, target in (("common_", "common_fields"), ("common_evidence_", "common_evidence")):
            result={}
            for name,value in form.multi_items():
                if name.startswith(prefix) and name[len(prefix):].isdigit() and (target!="common_evidence" or str(value).strip()):
                    result[name[len(prefix):]]=str(value)
            if result: data[target]=result
        return data

    @app.post("/imports/screenshots/{public_id}/{action}")
    async def update(request: Request, public_id: str, action: str):
        if action not in {"save","retry","intake","confirm","derive"}:
            raise HTTPException(404,"操作不存在")
        from app.services.screenshot_workflow import save_import_edits, create_pending_job_from_import, confirm_screenshot_import
        current = actor(request)
        form = await request.form()
        guided = form.get("review_mode") == "guided"
        with factory() as session:
            batch = batch_by_id(session,public_id)
            own_saved_versions = None

            def guided_response(*, error="", feedback="", status=200, draft_saved=False, own_versions=None):
                latest = session.get(ScreenshotImport, batch.id)
                payload = readiness(session, latest)
                if own_versions is not None:
                    # Retained form edits may only adopt identifiers from their own commit.
                    payload["versions"] = dict(own_versions)
                payload.update(error=error, feedback=feedback, draft_saved=draft_saved,
                               review_url=f"/imports/screenshots/{public_id}")
                return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})

            try:
                expected = versions(form)
                if action=="retry":
                    if current: require_permission(current,"task.submit")
                    batch=save_import_edits(session,batch.id,{**edits(form), "reextract": True},actor=current,commit=False,**expected)
                    from app.services.screenshot_tasks import enqueue_screenshot_import, enqueue_screenshot_extraction
                    options={"ai_service":app.state.ai_settings_service,"requested_by":current.id if current else None,
                             "queue_limit":app.state.settings.task_queue_limit}
                    try:
                        with session.begin_nested():
                            if batch.material_complete and (batch.edited_text.strip() or batch.supplemental_text.strip()):
                                enqueue_screenshot_extraction(session,batch.id,**options)
                            else:
                                enqueue_screenshot_import(session,batch.id,retry_failed_only=True,**options)
                    except ScreenshotError as exc:
                        # A queue/provider issue must not discard the operator's edits.
                        job = session.get(Job,batch.job_id) if batch.job_id else None
                        own_saved_versions = review_versions(batch,job)
                        session.commit()
                        if guided:
                            return guided_response(error=f"草稿已保存。{exc}", status=409, draft_saved=True,
                                                   own_versions=own_saved_versions)
                        return detail(request,session,batch,error=f"草稿已保存。{exc}",status_code=409)
                    session.commit()
                    return RedirectResponse(f"/imports/screenshots/{public_id}",status_code=303)
                if action=="save":
                    if guided:
                        batch = save_import_edits(session,batch.id,edits(form),actor=current,commit=False,**expected)
                        job = session.get(Job,batch.job_id) if batch.job_id else None
                        own_saved_versions = review_versions(batch,job)
                        session.commit()
                    else:
                        save_import_edits(session,batch.id,edits(form),actor=current,**expected)
                    feedback="草稿已保存。"
                elif action=="intake":
                    batch=save_import_edits(session,batch.id,edits(form),actor=current,**expected)
                    job=session.get(Job,batch.job_id) if batch.job_id else None
                    create_pending_job_from_import(session,batch.id,actor=current,expected_row_version=batch.row_version,
                        expected_job_row_version=job.row_version if job else None,expected_job_version=job.version if job else None)
                    feedback="已保存为待核验线索。"
                elif action=="derive":
                    from app.services.screenshot_workflow import create_derived_import
                    selected=[int(value) for value in form.getlist("selected_roles")]
                    parent_edits=edits(form)
                    parent_edits.pop("selected_roles",None)
                    batch=save_import_edits(session,batch.id,parent_edits,actor=current,commit=False,**expected)
                    job=session.get(Job,batch.job_id) if batch.job_id else None
                    new_batch=create_derived_import(session,batch.id,selected,actor=current,
                        expected_row_version=batch.row_version,expected_job_row_version=job.row_version if job else None,
                        expected_job_version=job.version if job else None)
                    return RedirectResponse(f"/imports/screenshots/{new_batch.public_id}",status_code=303)
                else:
                    from app.services.jobs import REQUIRED_VERIFICATION_CHECKS
                    data={"edits":edits(form),"notice_type":str(form.get("notice_type") or "新招聘"),
                        "quality_score":int(form.get("quality_score") or 0),
                        "verification_checks":{key:form.get(key)=="on" for key in REQUIRED_VERIFICATION_CHECKS},
                        "role_details_checked":form.get("role_details_checked")=="on",
                        "deadline_unspecified_confirmed":form.get("deadline_unspecified_confirmed")=="on",
                        "audience_confirmation":{"evidence_url":str(form.get("audience_url") or ""),
                                                 "evidence_excerpt":str(form.get("audience_excerpt") or ""),
                                                 "note":str(form.get("audience_note") or "")}}
                    if not any(value.strip() for value in data["audience_confirmation"].values()):
                        data.pop("audience_confirmation")
                    if form.get("scope_single_employer")=="on" or form.get("scope_role_boundaries")=="on":
                        data["scope_confirmation"]={"single_employer":form.get("scope_single_employer")=="on",
                            "role_boundaries":form.get("scope_role_boundaries")=="on",
                            "employer_name":str(form.get("scope_employer_name") or ""),
                            "evidence_excerpt":str(form.get("scope_excerpt") or ""),"note":str(form.get("scope_note") or "")}
                    if guided:
                        from app.services.screenshot_workflow import confirm_guided_screenshot_import
                        clicked = str(form.get("acknowledge") or "")
                        confirm_action = clicked if clicked in {"submit", "approve_and_generate"} else "submit"
                        data["acknowledge"] = clicked in {"submit", "approve_and_generate"}
                        result = confirm_guided_screenshot_import(session,batch.id,data,action=confirm_action,actor=current,**expected)
                    else:
                        result=confirm_screenshot_import(session,batch.id,data,action=str(form.get("confirm_action") or "submit"),actor=current,**expected)
                    feedback={"publishable":"审核通过，两种格式已生成，可以复制。","pending_review":"已提交审核。",
                              "pending_verification":"已保存，仍需审核员确认学生受众或补充信息。"}[result.state]
                if guided:
                    return guided_response(feedback=feedback, draft_saved=True, own_versions=own_saved_versions)
                return detail(request,session,session.get(ScreenshotImport,batch.id),feedback=feedback)
            except PermissionDenied as exc:
                session.rollback()
                if guided:
                    return guided_response(error=str(exc), status=403)
                return detail(request,session,batch,error=str(exc),status_code=403)
            except (ValueError, ScreenshotError) as exc:
                session.rollback()
                if guided:
                    draft_saved = getattr(exc, "draft_saved", False) is True
                    response = guided_response(error=str(exc), status=409,
                        draft_saved=draft_saved,
                        own_versions=getattr(exc, "own_saved_versions", {}) if draft_saved else None)
                    if hasattr(exc, "issues"):
                        payload = json.loads(response.body)
                        payload["issues"] = [issue.to_dict() for issue in exc.issues]
                        return JSONResponse(payload, status_code=409, headers={"Cache-Control": "no-store"})
                    return response
                return detail(request,session,session.get(ScreenshotImport,batch.id),error=str(exc),status_code=409,
                              form_values=edits(form).get("fields",{}))

    @app.get("/distribution/{item_id}/copy-payload")
    def copy_payload(request: Request,item_id: int,expected_job_version: int,expected_item_row_version: int):
        from app.services.distribution import distribution_blockers, build_wechat_draft, build_group_message
        from app.services.ai_content_draft import parse_content_draft
        current=actor(request)
        if current:
            try: require_permission(current,"distribution.publish")
            except PermissionDenied as exc: return JSONResponse({"error":str(exc)},status_code=403)
        with factory() as session:
            item=session.get(DistributionItem,item_id)
            if item is None: raise HTTPException(404,"草稿不存在")
            job=session.get(Job,item.job_id)
            if job is None: raise HTTPException(404,"岗位不存在")
            errors=distribution_blockers(job,item)
            if job.version!=expected_job_version or item.row_version!=expected_item_row_version:
                errors.append("内容已变化，请刷新并重新核对")
            if errors: return JSONResponse({"error":"；".join(errors)},status_code=409)
            content=parse_content_draft(item.ai_content_json,job) if item.ai_content_json else None
            draft=build_wechat_draft(job,content)
            return JSONResponse({"title":draft.title,"digest":draft.digest,"html":draft.html,"plain_text":draft.plain_text,
                                 "group_message":build_group_message(job),"job_version":job.version},headers={"Cache-Control":"no-store"})
