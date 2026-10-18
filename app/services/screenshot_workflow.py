"""Transactional screenshot workspace, intake and human confirmation services."""

from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
import re
from urllib.parse import urlsplit
from uuid import uuid4
from types import SimpleNamespace

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth.permissions import PermissionDenied, require_permission
from app.models import DistributionItem, Job, ReviewLog, ScreenshotAsset, ScreenshotImport, ScreenshotPiece, User
from app.services.audit import record_event
from app.services.concurrency import EditConflict
from app.services.deadline_policy import is_application_expired, job_application_deadline, parse_known_deadline
from app.services.distribution import build_confirmed_content_snapshot, create_distribution_items
from app.services.intake_screening import IntakeScreeningResult, REVIEW_NOTE, _FILTER_TERMS, screen_intake
from app.services.jobs import REQUIRED_VERIFICATION_CHECKS, UNSPECIFIED_DEADLINE
from app.services.reviews import review_job
from app.services.screenshot_contracts import ConfirmationResult, EvidenceRef, ExtractedField, ScreenshotFacts
from app.services.screenshot_extraction import ENUMS, FIELD_NAMES, MANUAL_CLASSIFICATION_FIELDS, MAX_TEXT_LENGTH, facts_from_dict, facts_to_dict, merge_piece_texts
from app.services.screenshot_prefill import COMMON_RECOVERY_WARNING, has_common_recovery_blockers
from app.services.structuring import StructuringInput, structure_job
from app.services.screenshot_review import ReviewIssue, collect_review_issues, known_deadline, publication_facts
from app.services.screenshot_review_policy import REVIEW_POLICY_VERSION, build_review_confirmation


EDIT_KEYS = {"source_name", "source_url", "title", "supplemental_text", "edited_text", "fields", "field_evidence", "role_fields", "role_evidence", "common_fields", "common_evidence", "selected_roles", "material_complete", "asset_order", "resolved_missing_fields", "reextract"}
JOB_FIELDS = ("employer_name", "announcement_title", "job_title", "job_family", "recruitment_type", "location_category", "location_detail", "target_audience", "direction_tags", "deadline", "official_url", "posting_scope", "attachment_status", "application_method", "application_contact", "student_fit_level", "distribution_recommendation")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _string(value, name, maximum=MAX_TEXT_LENGTH):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"{name}须为文字且不得超过{maximum}字符")
    return value.strip() if name not in {"正文", "补充正文"} else value


def _form_eol_value(value, canonical):
    """Keep existing bytes when a browser changed only CRLF/LF encoding."""
    if isinstance(value, str) and isinstance(canonical, str) and value.replace("\r\n", "\n") == canonical.replace("\r\n", "\n"):
        return canonical
    return value


def _form_field_values(old, value, evidence):
    if old:
        value = _form_eol_value(value, old.value)
        if isinstance(evidence, str):
            for ref in old.evidence_refs:
                evidence = _form_eol_value(evidence, ref.quote)
    return value, evidence


def _actor(session, actor, permission):
    if actor is None:
        if os.getenv("APP_ENV", "local").lower() == "cloud":
            raise PermissionDenied("云端操作必须登录有效账号")
        return None
    actor_id = actor if type(actor) is int else getattr(actor, "id", None)
    user = session.scalar(select(User).where(User.id == actor_id).execution_options(populate_existing=True))
    if user is None:
        raise PermissionDenied("账号不存在或会话已失效")
    require_permission(user, permission)
    return user


def _label(actor):
    return actor.display_name if actor else "本地管理员"


def _reserve(session, import_id, expected_row_version, expected_job_row_version=None, expected_job_version=None):
    record = session.scalar(select(ScreenshotImport).where(ScreenshotImport.id == import_id).execution_options(populate_existing=True))
    if record is None:
        raise ValueError("截图导入不存在")
    if type(expected_row_version) is not int:
        raise EditConflict("缺少导入版本，请刷新后重试")
    job = session.scalar(select(Job).where(Job.id == record.job_id).execution_options(populate_existing=True)) if record.job_id else None
    if record.job_id and job is None:
        raise ValueError("关联岗位不存在")
    if job is not None:
        if type(expected_job_row_version) is not int or type(expected_job_version) is not int:
            raise EditConflict("关联岗位版本缺失，请刷新后比较并重新提交")
        changed = session.execute(update(Job).where(Job.id == job.id, Job.row_version == expected_job_row_version,
            Job.version == expected_job_version).values(row_version=Job.row_version + 1), execution_options={"synchronize_session": False})
        if changed.rowcount != 1:
            raise EditConflict("岗位已被其他页面更新，请刷新后比较并重新提交")
        session.refresh(job)
    changed = session.execute(update(ScreenshotImport).where(ScreenshotImport.id == import_id,
        ScreenshotImport.row_version == expected_row_version).values(row_version=ScreenshotImport.row_version + 1),
        execution_options={"synchronize_session": False})
    if changed.rowcount != 1:
        raise EditConflict("导入版本已变化，请刷新后比较并重新提交")
    session.refresh(record)
    return record, job


def effective_import_text(record):
    base = record.edited_text or record.ocr_text
    text = "\n".join(part for part in (base, record.supplemental_text) if part)
    return _string(text, "正文")


def read_import_facts(record) -> ScreenshotFacts:
    try:
        data = json.loads(record.fields_json or "{}")
    except (ValueError, TypeError) as exc:
        raise ValueError("已存字段格式无效，请重新整理或手工补充") from exc
    if not data:
        return ScreenshotFacts({}, (), (), record.content_revision)
    return facts_from_dict(data)


def _selected(record, facts):
    try:
        indices = json.loads(record.selected_roles_json or "[]")
    except ValueError as exc:
        raise ValueError("岗位选择格式无效") from exc
    if not isinstance(indices, list) or any(type(index) is not int or not 0 <= index < len(facts.roles) for index in indices) or len(indices) != len(set(indices)):
        raise ValueError("所选岗位不存在，请刷新并重新选择")
    return tuple(facts.roles[index] for index in indices) if indices else facts.roles


def selected_import_facts(record):
    facts = read_import_facts(record)
    return replace(facts, roles=_selected(record, facts), content_revision=record.content_revision)


def guided_selected_import_facts(record):
    """Project chosen-role audience/location consistently, retaining literal proof."""
    facts = selected_import_facts(record)
    fields = dict(facts.fields)
    for name in ("location_detail", "target_audience"):
        if not facts.roles:
            continue
        contributors = []
        for role in facts.roles:
            field = role.get(name)
            if not field or not field.value.strip():
                field = fields.get(name)
            if field and field.value.strip() and field not in contributors:
                contributors.append(field)
        if contributors:
            values = tuple(dict.fromkeys(field.value.strip() for field in contributors))
            refs = tuple(dict.fromkeys(ref for field in contributors for ref in field.evidence_refs))
            state = "conflict" if any(field.state == "conflict" for field in contributors) else "unclear" if any(field.state != "explicit" for field in contributors) else "explicit"
            fields[name] = ExtractedField("、".join(values), state, refs)
    return replace(facts, fields=fields)


def _field_value(facts, name):
    field = facts.fields.get(name)
    return field.value if field else ""


def _screen(record, facts):
    fee = _fee_result("\n".join((record.ocr_text or "", effective_import_text(record))))
    if fee:
        return fee
    common = "\n".join(ref.quote for field in facts.common_requirements for ref in field.evidence_refs)
    original_material = "\n".join((record.ocr_text or "", effective_import_text(record)))
    global_clauses = [clause for clause in re.split(r"[。；;\n]", original_material)
        if re.search(r"(?:所有|全部|各)(?:招聘)?岗位(?:要求|条件|均|须|应|统一)|(?:所有|全体)应聘(?:者|人员)|公共条件|通用条件|统一要求", clause)]
    common = "\n".join((common, *dict.fromkeys(global_clauses)))
    if facts.roles:
        # Do not reuse full-announcement quotes: those may include an unrelated senior role.
        application = "\n".join(_field_value(facts, name) for name in ("application_contact", "application_method", "official_url"))
        results = []
        for role in facts.roles:
            evidence = "\n".join(dict.fromkeys(ref.quote for field in role.values() for ref in field.evidence_refs))
            evidence = "\n".join((evidence, common, application))
            results.append(_screen_with_fee(role["job_title"].value, evidence))
        grade = max(results, key=lambda item: {"A": 0, "B": 1, "C": 2, "D": 3}[item.grade])
    else:
        grade = _screen_with_fee(_field_value(facts, "job_title") or record.title,
            "\n".join(dict.fromkeys((record.ocr_text or "", effective_import_text(record)))))
    deadline = _field_value(facts, "deadline")
    if is_application_expired(effective_import_text(record)) or (parse_known_deadline(deadline) and parse_known_deadline(deadline) < datetime.now().date()):
        return IntakeScreeningResult("D", "过滤留档", "原文明确报名已截止", "已截止", "高")
    return grade


def _screen_with_fee(title, text):
    return _fee_result(text) or screen_intake(title, text)


def _fee_result(text):
    for term in _FILTER_TERMS:
        if term in text:
            return IntakeScreeningResult("D", "过滤留档", f"原文出现“{term}”，不适合学生招聘入库。", term, "高")
    for sentence in re.split(r"[。；;\n]|但是|但|然而|另需", text):
        if any(term in sentence for term in ("不收费", "不得收费", "无需缴费", "不收取", "免费报名", "不需缴费", "无需支付")):
            continue
        if re.search(r"(?:报名|招聘|应聘|求职).{0,15}(?:缴纳|缴费|收费|支付).{0,10}(?:费|元)|(?:需|须|应).{0,8}缴纳.{0,8}(?:报名费|培训费)", sentence):
            return IntakeScreeningResult("D", "过滤留档", "原文明确招聘或报名需缴费，不进入学生分发", sentence[:160], "高")
    return None


def _job_values(record, facts):
    values = {name: _field_value(facts, name) for name in JOB_FIELDS}
    values.update(posting_scope=values["posting_scope"] or "insufficient_information",
                  attachment_status=values["attachment_status"] or "not_required",
                  application_method=values["application_method"] or "unknown",
                  student_fit_level=values["student_fit_level"] or "待人工判断",
                  distribution_recommendation=values["distribution_recommendation"] or "仅保留资料库")
    if facts.roles:
        title = "、".join(role["job_title"].value for role in facts.roles)
        if len(title) > 160:
            raise ValueError("所选岗位名称合计超过160字符，请明确公告范围")
        values["job_title"] = title
    values.update(source_url=record.source_url, evidence_text=effective_import_text(record),
                  notice_type=_field_value(facts, "notice_type") or "待判断",
                  content_fingerprint=hashlib.sha256(effective_import_text(record).encode("utf-8")).hexdigest())
    limits = {"employer_name": 160, "announcement_title": 200, "job_title": 160, "job_family": 80,
              "recruitment_type": 30, "location_category": 30, "location_detail": 160,
              "target_audience": 60, "direction_tags": 200, "deadline": 40, "official_url": 500,
              "source_url": 500, "application_contact": 500}
    for name, maximum in limits.items():
        if len(values[name]) > maximum:
            raise ValueError(f"{name}超过{maximum}字符，请核对岗位范围")
    return values


def _audit(session, record, actor, action, changes):
    record_event(session, actor.id if actor else None, _label(actor), action, "screenshot_import", str(record.id), "", changes)


def _invalidate_job(session, record, job, facts, actor):
    if job.status in {"已截止", "已撤销", "淘汰"} or job.lifecycle_status != "正常":
        raise ValueError("岗位已截止、撤销或淘汰，不能覆盖原记录")
    previous_grade, previous_reason = job.intake_grade, job.intake_reason
    for name, value in _job_values(record, facts).items():
        setattr(job, name, value)
    screening = _screen(record, facts)
    if previous_grade == "D":
        screening = IntakeScreeningResult("D", "过滤留档", previous_reason, job.intake_evidence, job.intake_confidence)
    elif previous_grade in {"B", "C"} and screening.grade != "D":
        screening = IntakeScreeningResult(previous_grade, "普通待核验" if previous_grade == "B" else "人工复核",
            REVIEW_NOTE if previous_grade == "B" else previous_reason, job.intake_evidence, job.intake_confidence)
    job.intake_grade, job.intake_route, job.intake_reason = screening.grade, screening.route, screening.reason
    job.intake_evidence, job.intake_confidence = screening.evidence, screening.confidence
    job.status, job.quality_score = "待核验", 0
    job.version += 1
    job.verification_checks, job.verification_version = "{}", 0
    job.confirmed_content_version = 0
    job.role_details_status, job.role_details_evidence = "unknown", ""
    job.last_verified_at = None
    job.risk_flags = "截图事实已修改，尚未人工核验"
    job.updated_by_user_id = actor.id if actor else None
    job.last_change_summary = "截图页修改事实，旧核验和分发稿已失效"
    for item in session.scalars(select(DistributionItem).where(DistributionItem.job_id == job.id)):
        item.status = "已失效"
        item.row_version += 1
    session.add(ReviewLog(job_id=job.id, action="截图事实更新", note=job.last_change_summary,
                          operator_name=_label(actor), actor_user_id=actor.id if actor else None))


def _edit_roles(facts, role_fields, role_evidence, text, ledger, actor):
    if not isinstance(role_fields, dict) or not isinstance(role_evidence, dict) or set(role_evidence) - set(role_fields):
        raise ValueError("岗位编辑格式无效")
    roles = [dict(role) for role in facts.roles]
    for index_key, values in role_fields.items():
        if not isinstance(index_key, str) or not index_key.isdecimal() or not 0 <= int(index_key) < len(roles):
            raise ValueError("编辑的岗位索引不存在")
        index = int(index_key)
        evidence = role_evidence.get(index_key, {})
        if not isinstance(values, dict) or set(values) - set(FIELD_NAMES) or not isinstance(evidence, dict) or set(evidence) - set(values):
            raise ValueError("岗位字段或证据格式无效")
        role = roles[index]
        own_title = role["job_title"].value
        title_refs = role["job_title"].evidence_refs
        anchor = title_refs[0].text_start if title_refs else text.find(own_title)
        others = [(other["job_title"].value, other["job_title"].evidence_refs) for position, other in enumerate(facts.roles) if position != index]
        next_anchors = [refs[0].text_start for _, refs in others if refs and refs[0].text_start > anchor]
        end = min(next_anchors) if next_anchors else len(text)
        for name, value in values.items():
            value = _string(value, f"岗位{name}", 6000)
            if name in {"role_details_status", "role_details_evidence"}:
                raise ValueError("岗位明细核验须使用人工清单")
            old = role.get(name)
            value, submitted_quote = _form_field_values(old, value, evidence.get(name))
            if name in evidence:
                evidence[name] = submitted_quote
            if old and old.value == value and (name not in evidence or any(ref.quote == submitted_quote for ref in old.evidence_refs)):
                continue
            if value and name in MANUAL_CLASSIFICATION_FIELDS:
                if name in ENUMS and value not in ENUMS[name]:
                    raise ValueError("岗位人工分类枚举无效")
                role[name] = ExtractedField(value, "explicit", ())
                ledger.append({"kind": "human_classification", "role_index": index, "field": name, "value": value, "actor_id": actor.id if actor else None})
                continue
            quote = _string(evidence[name], "岗位证据", MAX_TEXT_LENGTH) if name in evidence else value
            position = text.find(quote, max(0, anchor), end) if quote else -1
            if position < 0 and name in evidence:
                # Later official corrections must name their own role explicitly.
                position = text.find(quote)
                if position < 0 or (own_title not in quote and not (name == "job_title" and value in quote)) or any(title in quote for title, _ in others):
                    raise ValueError("岗位证据不属于当前岗位，不能串用其他岗位条件")
            if name in evidence and position < 0:
                raise ValueError("岗位证据必须是连续原文摘录")
            refs = (EvidenceRef(None, None, position, position + len(quote), quote, "manual"),) if quote and position >= 0 else ()
            role[name] = ExtractedField(value, "explicit" if value and refs else "unclear" if value else "missing", refs)
    return replace(facts, roles=tuple(roles))


def _rebase_facts(facts, text):
    def rebase(field):
        refs = []
        for ref in field.evidence_refs:
            start = text.find(ref.quote)
            if start < 0:
                return replace(field, state="unclear")
            refs.append(replace(ref, text_start=start, text_end=start + len(ref.quote)))
        return replace(field, evidence_refs=tuple(refs))
    return replace(facts, fields={name: rebase(field) for name, field in facts.fields.items()},
        roles=tuple({name: rebase(field) for name, field in role.items()} for role in facts.roles),
        common_requirements=tuple(rebase(field) for field in facts.common_requirements))


def _edit_common(facts, values, evidence, text, ledger, actor):
    if not isinstance(values, dict) or not isinstance(evidence, dict) or set(evidence) - set(values):
        raise ValueError("公共条件编辑格式无效")
    common = list(facts.common_requirements)
    for index_key, value in values.items():
        if not isinstance(index_key, str) or not index_key.isdecimal() or not 0 <= int(index_key) < len(common):
            raise ValueError("公共条件索引不存在")
        index = int(index_key)
        value = _string(value, "公共条件", 6000)
        value, submitted_quote = _form_field_values(common[index], value, evidence.get(index_key))
        if index_key in evidence:
            evidence[index_key] = submitted_quote
        if common[index].value == value and (index_key not in evidence or any(ref.quote == submitted_quote for ref in common[index].evidence_refs)):
            continue
        if not value:
            raise ValueError("既有公共条件不得删除，请对照原文纠正")
        quote = _string(evidence.get(index_key, value), "公共条件证据", MAX_TEXT_LENGTH)
        start = text.find(quote) if quote else -1
        if start < 0 or not any(term in quote for term in ("通用", "公共", "所有", "全体", "全部岗位", "各岗位", "统一", "各岗")):
            raise ValueError("公共条件须引用完整材料中明确适用于所有岗位的连续原文，不能串用单岗条件")
        common[index] = ExtractedField(value, "explicit", (EvidenceRef(None, None, start, start + len(quote), quote, "manual"),))
        ledger.append({"kind": "human_common_correction", "common_index": index, "value": value, "quote": quote, "actor_id": actor.id if actor else None})
    return replace(facts, common_requirements=tuple(common))


def _save(session, record, job, edits, actor):
    if not isinstance(edits, dict) or set(edits) - EDIT_KEYS:
        raise ValueError("保存字段格式无效")
    if "reextract" in edits and type(edits["reextract"]) is not bool:
        raise ValueError("重新提取确认须为布尔值")
    facts = read_import_facts(record)
    previous_fields = facts.fields
    previous_roles = facts.roles
    previous_common = facts.common_requirements
    previous_warnings = facts.warnings
    before = {name: getattr(record, name) for name in ("source_name", "source_url", "title", "supplemental_text", "edited_text", "fields_json", "selected_roles_json", "material_complete")}
    for name, maximum in (("source_name", 160), ("source_url", 500), ("title", 200), ("supplemental_text", MAX_TEXT_LENGTH), ("edited_text", MAX_TEXT_LENGTH)):
        if name in edits:
            value = _string(edits[name], "正文" if name == "edited_text" else "补充正文" if name == "supplemental_text" else name, maximum)
            canonical = getattr(record, name)
            if name == "edited_text":
                # Recover a prior browser-only CRLF draft using immutable OCR
                # bytes; genuine body edits continue to need current evidence.
                canonical = _form_eol_value(record.edited_text or record.ocr_text, record.ocr_text)
            setattr(record, name, _form_eol_value(value, canonical))
    text = effective_import_text(record)
    if "material_complete" in edits:
        if type(edits["material_complete"]) is not bool:
            raise ValueError("材料完整确认须为布尔值")
        record.material_complete = edits["material_complete"]
    new_fields, evidence = edits.get("fields", {}), edits.get("field_evidence", {})
    if not isinstance(new_fields, dict) or set(new_fields) - set(FIELD_NAMES) or not isinstance(evidence, dict) or set(evidence) - set(new_fields):
        raise ValueError("人工字段或引用格式无效")
    fields = dict(facts.fields)
    try:
        manual_ledger = json.loads(record.evidence_json or "[]")
    except ValueError:
        manual_ledger = []
    if not isinstance(manual_ledger, list):
        manual_ledger = []
    for name, value in new_fields.items():
        value = _string(value, name, 6000)
        if name in {"role_details_status", "role_details_evidence"}:
            raise ValueError("岗位明细核验请使用人工确认清单")
        old = fields.get(name)
        value, submitted_quote = _form_field_values(old, value, evidence.get(name))
        if name in evidence:
            evidence[name] = submitted_quote
        if old and old.value == value and (name not in evidence or (not value and evidence[name] == "") or any(ref.quote == submitted_quote for ref in old.evidence_refs)):
            continue
        if value and name in MANUAL_CLASSIFICATION_FIELDS:
            if name in ENUMS and value not in ENUMS[name] and not (name == "attachment_status" and value == "checked"):
                raise ValueError(f"{name}人工分类选择无效")
            fields[name] = ExtractedField(value, "explicit", ())
            manual_ledger.append({"kind": "human_classification", "field": name, "value": value, "actor_id": actor.id if actor else None})
            continue
        if value and name == "official_url" and name not in evidence:
            if not _http_url(value):
                raise ValueError("人工提供的官方链接须为有效HTTP(S)地址")
            fields[name] = ExtractedField(value, "explicit", ())
            manual_ledger.append({"kind": "human_provided_url", "field": name, "value": value, "actor_id": actor.id if actor else None})
            continue
        quote = _string(evidence[name], "字段证据", MAX_TEXT_LENGTH) if name in evidence else value
        location = text.find(quote) if quote else -1
        refs = (EvidenceRef(None, None, location, location + len(quote), quote, "manual"),) if quote and location >= 0 else ()
        if name in evidence and not refs:
            raise ValueError("人工字段证据必须为正文或补充材料中的连续原文")
        state = "explicit" if value and refs else "unclear" if value or (old and old.state == "unclear") else "missing"
        fields[name] = ExtractedField(value, state, refs if state != "missing" else ())
    resolved_missing = edits.get("resolved_missing_fields", [])
    if not isinstance(resolved_missing, list) or any(not isinstance(name, str) or name not in FIELD_NAMES for name in resolved_missing) or len(resolved_missing) != len(set(resolved_missing)):
        raise ValueError("原文未提供项确认格式无效")
    for name in resolved_missing:
        field = fields.get(name)
        if field is None or field.value or field.state != "unclear":
            raise ValueError("仅可明确确认空白待核对字段为原文未提供，非空字段须核对依据")
        fields[name] = ExtractedField("", "missing", ())
        manual_ledger.append({"kind": "human_confirmed_missing", "field": name, "content_revision": record.content_revision + 1,
                              "actor_id": actor.id if actor else None})
    facts = replace(facts, fields=fields)
    facts = _edit_roles(facts, edits.get("role_fields", {}), edits.get("role_evidence", {}), text, manual_ledger, actor)
    common_ledger_start = len(manual_ledger)
    facts = _edit_common(facts, edits.get("common_fields", {}), edits.get("common_evidence", {}), text, manual_ledger, actor)
    common_corrected = any(item.get("kind") == "human_common_correction" for item in manual_ledger[common_ledger_start:])
    if common_corrected and facts.common_requirements and all(
        field.state == "explicit" and field.value and field.evidence_refs and all(
            ref.quote and 0 <= ref.text_start < ref.text_end <= len(text) and text[ref.text_start:ref.text_end] == ref.quote
            for ref in field.evidence_refs
        ) for field in facts.common_requirements
    ):
        facts = replace(facts, warnings=tuple(warning for warning in facts.warnings if not warning.startswith(COMMON_RECOVERY_WARNING)))
    if "selected_roles" in edits:
        selected = edits["selected_roles"]
        if not isinstance(selected, list) or any(type(index) is not int for index in selected) or len(selected) != len(set(selected)):
            raise ValueError("岗位选择格式无效")
        record.selected_roles_json = _json(selected)
        _selected(record, facts)
        if job and job.intake_grade == "D" and record.selected_roles_json != before["selected_roles_json"]:
            raise ValueError("D级原岗位不可强行升档，请创建派生导入选择其他岗位")
    if "asset_order" in edits:
        assets = session.scalars(select(ScreenshotAsset).where(ScreenshotAsset.import_id == record.id)).all()
        ids = edits["asset_order"]
        if not isinstance(ids, list) or any(type(value) is not int for value in ids) or set(ids) != {asset.id for asset in assets} or len(ids) != len(assets):
            raise ValueError("图片顺序必须完整且仅包含当前批次图片")
        for asset in assets:
            asset.position = ids.index(asset.id)
        session.flush()
        pairs = session.execute(select(ScreenshotPiece, ScreenshotAsset.position).join(ScreenshotAsset, ScreenshotPiece.asset_id == ScreenshotAsset.id).where(ScreenshotAsset.import_id == record.id)).all()
        if pairs:
            record.ocr_text, refs = merge_piece_texts([{"id": piece.id, "asset_id": piece.asset_id, "asset_order": position,
                "piece_index": piece.piece_index, "text": piece.text} for piece, position in pairs])
            facts = _rebase_facts(facts, effective_import_text(record))
            fields = facts.fields
            manual_ledger = [item for item in manual_ledger if not (isinstance(item, dict) and item.get("kind") == "ocr_source")]
            manual_ledger.extend({"kind": "ocr_source", **ref.to_dict()} for ref in refs)
    changed = any(getattr(record, name) != value for name, value in before.items() if name != "fields_json") or fields != previous_fields or facts.roles != previous_roles or facts.common_requirements != previous_common
    if "asset_order" in edits or edits.get("reextract") is True or facts.warnings != previous_warnings:
        changed = True
    if changed:
        record.content_revision += 1
        facts = replace(facts, content_revision=record.content_revision)
        record.fields_json = _json(facts_to_dict(facts))
        record.evidence_json = _json(manual_ledger)
        record.conflicts_json = _json([name for name, field in fields.items() if field.state == "conflict"])
        record.status = "needs_attention" if any(field.state in {"conflict", "unclear"} for field in fields.values()) else "ready"
        record.updated_by_user_id = actor.id if actor else None
        if job:
            _invalidate_job(session, record, job, replace(facts, roles=_selected(record, facts)), actor)
        _audit(session, record, actor, "screenshot.edited", {"content_revision": record.content_revision, "job_id": record.job_id})
    return record


def save_import_edits(session: Session, import_id: int, edits: dict, *, expected_row_version: int,
                       expected_job_row_version=None, expected_job_version=None, actor=None, commit=True) -> ScreenshotImport:
    try:
        user = _actor(session, actor, "business.edit")
        record, job = _reserve(session, import_id, expected_row_version, expected_job_row_version, expected_job_version)
        _save(session, record, job, edits, user)
        session.commit() if commit else session.flush()
        return record
    except Exception:
        session.rollback()
        raise


def _create_job(session, record, actor):
    if record.job_id:
        return session.get(Job, record.job_id)
    facts = selected_import_facts(record)
    values = _job_values(record, facts)
    screening = _screen(record, facts)
    identity = hashlib.sha256(_json({"revision": record.content_revision, "selection": record.selected_roles_json, "facts": facts_to_dict(facts)}).encode("utf-8")).hexdigest()
    job = Job(**values, fingerprint=f"screenshot:{record.id}:{identity}", quality_score=0, is_demo=False,
        status="待核验", risk_flags="截图材料尚未人工核验", evidence_status="截图原文已识别",
        evidence_note=f"人工截图材料来源：{record.source_name or '未填写来源名称'}", intake_grade=screening.grade,
        intake_route=screening.route, intake_reason=screening.reason, intake_evidence=screening.evidence[:160],
        intake_confidence=screening.confidence, collected_at=datetime.now(), updated_by_user_id=actor.id if actor else None)
    session.add(job)
    session.flush()
    record.job_id = job.id
    record.updated_by_user_id = actor.id if actor else None
    session.add(ReviewLog(job_id=job.id, action="截图入库", note=f"初筛{screening.grade}：{screening.reason}", operator_name=_label(actor), actor_user_id=actor.id if actor else None))
    _audit(session, record, actor, "screenshot.intake_created", {"job_id": job.id, "grade": job.intake_grade, "fingerprint": identity})
    return job


def create_pending_job_from_import(session: Session, import_id: int, *, expected_row_version: int,
                                    expected_job_row_version=None, expected_job_version=None, actor=None, commit=True) -> Job:
    try:
        user = _actor(session, actor, "business.edit")
        record, _ = _reserve(session, import_id, expected_row_version, expected_job_row_version, expected_job_version)
        job = _create_job(session, record, user)
        session.commit() if commit else session.flush()
        return job
    except Exception:
        session.rollback()
        raise


def create_derived_import(session: Session, import_id: int, selected_roles: list[int], *, expected_row_version: int,
                           expected_job_row_version=None, expected_job_version=None, actor=None) -> ScreenshotImport:
    """Copy a workspace's read-only asset references, keeping its old D job intact."""
    try:
        user = _actor(session, actor, "business.edit")
        parent, job = _reserve(session, import_id, expected_row_version, expected_job_row_version, expected_job_version)
        if job is None or job.intake_grade != "D":
            raise ValueError("只有已保留D级岗位的批次需要创建派生导入")
        facts = read_import_facts(parent)
        if not isinstance(selected_roles, list) or not selected_roles or any(type(index) is not int or not 0 <= index < len(facts.roles) for index in selected_roles) or len(set(selected_roles)) != len(selected_roles):
            raise ValueError("请选择证据明确的其他岗位")
        child = ScreenshotImport(submission_key=uuid4().hex, parent_import_id=parent.id, source_name=parent.source_name,
            source_url=parent.source_url, title=parent.title, supplemental_text=parent.supplemental_text, ocr_text=parent.ocr_text,
            edited_text=parent.edited_text, selected_roles_json=_json(selected_roles), material_complete=parent.material_complete,
            status="ready", created_by_user_id=user.id if user else None, updated_by_user_id=user.id if user else None)
        session.add(child)
        session.flush()
        asset_ids, piece_ids = {}, {}
        for source in session.scalars(select(ScreenshotAsset).where(ScreenshotAsset.import_id == parent.id)).all():
            copied = ScreenshotAsset(**{column.name: getattr(source, column.name) for column in ScreenshotAsset.__table__.columns if column.name not in {"id", "import_id"}}, import_id=child.id)
            session.add(copied)
            session.flush()
            asset_ids[source.id] = copied.id
            for piece in session.scalars(select(ScreenshotPiece).where(ScreenshotPiece.asset_id == source.id)).all():
                duplicate = ScreenshotPiece(**{column.name: getattr(piece, column.name) for column in ScreenshotPiece.__table__.columns if column.name not in {"id", "asset_id"}}, asset_id=copied.id)
                session.add(duplicate)
                session.flush()
                piece_ids[piece.id] = duplicate.id
        def remap(field):
            return replace(field, evidence_refs=tuple(replace(ref, asset_id=asset_ids.get(ref.asset_id), piece_id=piece_ids.get(ref.piece_id)) if ref.origin == "ocr" else ref for ref in field.evidence_refs))
        own_facts = ScreenshotFacts({name: remap(field) for name, field in facts.fields.items()},
            tuple({name: remap(field) for name, field in role.items()} for role in facts.roles), facts.warnings, 1,
            tuple(remap(field) for field in facts.common_requirements))
        child.fields_json = _json(facts_to_dict(own_facts))
        child.evidence_json = _json([ref.to_dict() for field in own_facts.fields.values() for ref in field.evidence_refs])
        _audit(session, child, user, "screenshot.derived_created", {"parent_import_id": parent.id, "original_job_id": job.id, "selected_roles": selected_roles})
        session.commit()
        return child
    except Exception:
        session.rollback()
        raise


def _http_url(value):
    parsed = urlsplit(value)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname) and not any(char.isspace() for char in value)


def _confirm_audience(session, record, job, data, actor, *, validate_only=False):
    confirmation = data.get("audience_confirmation")
    if not confirmation:
        return False
    if actor:
        require_permission(actor, "review.decide")
    if job.intake_grade == "D":
        raise ValueError("D级记录不可通过受众确认升档，请创建派生导入")
    if job.intake_grade not in {"B", "C"}:
        return True
    if job.intake_grade == "C" and not any(term in job.intake_reason for term in ("信息不足", "不完整", "缺少", "适配不明", "受众", "信号")):
        raise ValueError("当前C级原因不属于信息不足或受众待确认")
    if not isinstance(confirmation, dict):
        raise ValueError("受众确认证据格式无效")
    url = _string(confirmation.get("evidence_url", ""), "官方证据链接", 500)
    excerpt = _string(confirmation.get("evidence_excerpt", ""), "官方摘录", 6000)
    note = _string(confirmation.get("note", ""), "判断说明", 2000)
    facts = selected_import_facts(record)
    if not _http_url(url) or url != job.official_url or not excerpt or excerpt not in effective_import_text(record) or not note:
        raise ValueError("请提供当前官方链接、连续原文受众摘录及人工判断说明")
    if not record.material_complete or not job.employer_name or not job.job_title or not job.official_url or not job.location_detail or not (job.application_contact or job.application_method in {"official_page", "official_platform", "on_site"}):
        raise ValueError("受众确认前请补齐完整岗位事实与官方投递依据")
    if not any(term in excerpt for term in ("应届", "实习", "在校", "校园招聘", "毕业两年", "校招")):
        raise ValueError("官方受众证据未明确学生或应届可投")
    if _screen(record, facts).grade == "D" or job.lifecycle_status != "正常":
        raise ValueError("明确高级资历、已截止或异常岗位不能升档")
    if validate_only:
        return True
    old = job.intake_grade
    evidence = {"old_grade": old, "new_grade": "B", "evidence_url": url, "evidence_excerpt": excerpt, "note": note,
                "content_fingerprint": job.content_fingerprint, "import_id": record.id, "content_revision": record.content_revision}
    job.intake_grade, job.intake_route = "B", "普通待核验"
    job.intake_reason = "审核员依据官方证据确认学生受众，仍须完整核验与审核"
    job.intake_evidence = excerpt[:160]
    job.evidence_note = f"人工截图材料来源：{record.source_name or '未填写来源名称'}；官方受众证据已确认"
    session.add(ReviewLog(job_id=job.id, action="截图受众确认", note=_json(evidence), operator_name=_label(actor), actor_user_id=actor.id if actor else None))
    _audit(session, record, actor, "screenshot.audience_confirmed", evidence)
    return True


def _confirmation_blockers(record, facts, data):
    if not record.material_complete:
        raise ValueError("材料尚不完整，请补齐失败图片或人工确认完整正文")
    if not effective_import_text(record).strip():
        raise ValueError("缺少完整原文")
    if has_common_recovery_blockers(facts):
        raise ValueError("AI公共条件待核对，请逐项补充当前原文依据后保存；公共条件结构缺失时请重新提取")
    if any(field.state in {"conflict", "unclear"} for field in facts.fields.values()) or any(field.state in {"conflict", "unclear"} for role in facts.roles for field in role.values()) or any(field.state in {"conflict", "unclear"} for field in facts.common_requirements):
        raise ValueError("存在字段冲突或不确定值，请核对证据后保存")
    checks = data.get("verification_checks", {})
    if not isinstance(checks, dict) or set(checks) - set(REQUIRED_VERIFICATION_CHECKS) or any(type(value) is not bool for value in checks.values()) or not all(checks.get(key) is True for key in REQUIRED_VERIFICATION_CHECKS):
        raise ValueError("请逐项确认六项人工核验清单")
    score = data.get("quality_score", 0)
    if type(score) is not int or not 0 <= score <= 100:
        raise ValueError("质量分必须为0到100整数")
    deadline = _field_value(facts, "deadline")
    if deadline and deadline != UNSPECIFIED_DEADLINE and parse_known_deadline(deadline) is None:
        raise ValueError("截止日期缺少年份或日期不明确，请依据原文补全后核验")
    if not deadline and data.get("deadline_unspecified_confirmed") is not True:
        raise ValueError("截止日期缺失，请核对完整公告并明确确认未统一规定截止时间")
    return _resolve_scope(record, facts, data)


def _resolve_scope(record, facts, data):
    text = effective_import_text(record)
    employers = tuple(dict.fromkeys(value.strip() for value in re.findall(r"(?:招聘单位|用人单位|公司名称|单位名称)\s*[：:]\s*([^。；;\n，,]{2,80})", text)))
    critical = [warning for warning in facts.warnings if any(term in warning for term in
        ("岗位边界", "归属", "多个公司", "多公司", "拆批", "多单位", "跨公司", "单位冲突", "无法区分"))]
    missing_roles = _field_value(facts, "posting_scope") == "multi_role_announcement" and not facts.roles
    if not critical and len(employers) < 2 and not missing_roles:
        return None
    confirmation = data.get("scope_confirmation")
    if not isinstance(confirmation, dict) or confirmation.get("single_employer") is not True or confirmation.get("role_boundaries") is not True:
        raise ValueError("单位或岗位范围/边界尚未明确，请拆批或提供人工范围确认证据")
    employer = _string(confirmation.get("employer_name", ""), "确认单位", 160)
    excerpt = _string(confirmation.get("evidence_excerpt", ""), "范围证据", MAX_TEXT_LENGTH)
    note = _string(confirmation.get("note", ""), "范围判断说明", 2000)
    titles = [role["job_title"].value for role in facts.roles] or [_field_value(facts, "job_title")]
    if employer != _field_value(facts, "employer_name") or not employer or not excerpt or excerpt not in text or not note or employer not in excerpt or any(not title or title not in excerpt for title in titles):
        raise ValueError("范围证据须为当前单位及所有选中岗位的连续原文，并说明人工选择范围")
    if any(other != employer and other in excerpt for other in employers):
        raise ValueError("范围证据仍包含多个单位，请仅选择一个单位的岗位或拆批")
    if missing_roles:
        raise ValueError("多岗位明细尚未明确，请补齐独立岗位证据或按单岗位范围重新保存")
    return {"kind": "human_scope_confirmation", "content_revision": record.content_revision,
        "employer_name": employer, "evidence_excerpt": excerpt, "note": note,
        "warnings": critical, "selected_employer_excerpt": len(employers) > 1}


def confirm_screenshot_import(session: Session, import_id: int, data: dict, *, action: str,
                               expected_row_version: int, expected_job_row_version=None,
                               expected_job_version=None, actor=None) -> ConfirmationResult:
    try:
        if action not in {"submit", "approve_and_generate"} or not isinstance(data, dict):
            raise ValueError("未知确认操作")
        user = _actor(session, actor, "business.edit")
        if action == "approve_and_generate":
            user = _actor(session, user, "review.decide")
            user = _actor(session, user, "distribution.publish")
        record, existing = _reserve(session, import_id, expected_row_version, expected_job_row_version, expected_job_version)
        edits = data.get("edits", {})
        if "fields" in data:
            edits = {**edits, "fields": data["fields"]}
        if edits:
            _save(session, record, existing, edits, user)
        job = _create_job(session, record, user)
        if job.intake_grade == "D":
            raise ValueError("D级记录不可进入学生分发，请创建派生导入处理其他岗位")
        if job.lifecycle_status != "正常" or job.status in {"已截止", "已撤销", "淘汰"}:
            raise ValueError("已截止、撤销或淘汰岗位不能确认")
        if data.get("notice_type") == "新招聘":
            job.notice_type = "新招聘"
        if job.notice_type != "新招聘":
            raise ValueError("请人工确认内容为新招聘")
        _confirm_audience(session, record, job, data, user)
        if job.intake_grade == "C" or "[TARGET_AUDIENCE_UNCLEAR]" in job.intake_reason:
            job.status, job.verification_version, job.verification_checks = "待核验", 0, "{}"
            session.commit()
            return ConfirmationResult(record.id, job.id, "pending_verification", ())
        facts = selected_import_facts(record)
        scope_resolution = _confirmation_blockers(record, facts, data)
        if not _http_url(record.source_url) or not _http_url(job.official_url):
            raise ValueError("正式审核须填写有效来源链接和官方链接")
        if job.distribution_recommendation != "进入学生分发审核":
            raise ValueError("仅保留资料库或不进入学生分发的内容不能生成正式稿")
        if job.status in {"待审核", "可发布"}:
            job.status = "待核验"
            job.verification_version = 0
            job.verification_checks = "{}"
        note = _string(data.get("note", ""), "核验备注", 2000) or "用户确认当前版本及核验清单"
        job_values = _job_values(record, facts)
        details_checked = data.get("role_details_checked") is True
        detail_refs = [ref.to_dict() for role in facts.roles for field in role.values() for ref in field.evidence_refs]
        struct = StructuringInput(**{name: job_values.get(name, "") for name in ("employer_name", "job_title", "job_family", "recruitment_type", "location_category", "location_detail", "target_audience", "direction_tags", "deadline", "official_url", "posting_scope", "attachment_status", "application_method", "application_contact", "student_fit_level", "distribution_recommendation", "announcement_title")},
            quality_score=data.get("quality_score", 0), note=note, verification_checks=data["verification_checks"],
            role_details_status="checked" if details_checked else "unknown", role_details_evidence=_json(detail_refs) if details_checked else "")
        structure_job(session, job.id, struct, _label(user), expected_row_version=job.row_version, commit=False)
        excerpt = (bool(facts.roles) and len(facts.roles) < len(read_import_facts(record).roles)) or bool(scope_resolution and scope_resolution["selected_employer_excerpt"])
        snapshot = build_confirmed_content_snapshot(facts, import_id=record.id, content_revision=record.content_revision, student_roles_excerpt=excerpt)
        snapshot["fields"].update({name: getattr(job, name) for name in JOB_FIELDS})
        snapshot["fields"]["source_name"] = record.source_name
        snapshot["fields"]["source_url"] = record.source_url
        job.confirmed_content_json = _json(snapshot)
        job.confirmed_content_version = job.version
        if scope_resolution:
            ledger = json.loads(record.evidence_json or "[]")
            record.evidence_json = _json([*ledger, {**scope_resolution, "actor_id": user.id if user else None}])
            _audit(session, record, user, "screenshot.scope_confirmed", scope_resolution)
        ids = ()
        if action == "approve_and_generate":
            review_job(session, job.id, "approve", note, _label(user), actor_user_id=user.id if user else None, commit=False)
            ids = tuple(item.id for item in create_distribution_items(session, job.id, commit=False))
        job.updated_by_user_id = user.id if user else None
        _audit(session, record, user, "screenshot.confirmed", {"job_id": job.id, "action": action, "job_version": job.version, "content_revision": record.content_revision})
        session.commit()
        return ConfirmationResult(record.id, job.id, "publishable" if action == "approve_and_generate" else "pending_review", ids)
    except Exception:
        session.rollback()
        raise


class ScreenshotReviewValidationError(ValueError):
    """All current guided blockers; the separately saved draft remains intact."""

    def __init__(self, issues):
        self.issues = tuple(issues)
        super().__init__("草稿已保存，尚不能成稿：" + "；".join(issue.message for issue in self.issues))


def _guided_issue(path, code, message, action="请对照当前原文核对、补充并保存"):
    return ReviewIssue(path, code, message, "error", action)


def guided_review_readiness(session, record):
    """Read-only readiness, including actual OCR pieces and absent-date wording."""
    facts = guided_selected_import_facts(record)
    assets = session.scalars(select(ScreenshotAsset).where(ScreenshotAsset.import_id == record.id)).all()
    pieces = session.scalars(select(ScreenshotPiece).join(ScreenshotAsset,
        ScreenshotPiece.asset_id == ScreenshotAsset.id).where(ScreenshotAsset.import_id == record.id)).all()
    covered = {piece.asset_id for piece in pieces}
    ocr_complete = bool(assets) and covered == {asset.id for asset in assets} and all(
        piece.status == "succeeded" and piece.text.strip() for piece in pieces)
    failed_material = bool(assets) and not ocr_complete
    manual_complete = record.material_complete is True and (not failed_material or bool(record.edited_text.strip()))
    complete = manual_complete or ocr_complete
    text = effective_import_text(record)
    deadline = facts.fields.get("deadline")
    blank_deadline = not deadline or (not deadline.value.strip() and deadline.state != "conflict")
    # An absent extraction must never erase an explicit yearless date in the source.
    raw_unknown = any(re.search(r"(?<!\d)(?:\d{1,2}月\d{1,2}日|\d{1,2}[-/]\d{1,2})(?!\d)", match.group(0))
        and not re.search(r"20\d{2}\s*(?:年|[-/.])", match.group(0))
        for match in re.finditer(r"(?:报名截止|截止日期|截止时间|截至|截止)[^。；;\n]{0,80}", text))
    deadline_unknown_allowed = blank_deadline and not raw_unknown
    view_fields = dict(facts.fields)
    if deadline_unknown_allowed:
        view_fields["deadline"] = ExtractedField(UNSPECIFIED_DEADLINE, "missing", ())
    view = SimpleNamespace(source_url=record.source_url, material_complete=complete,
        ocr_text=record.ocr_text, edited_text=record.edited_text, supplemental_text=record.supplemental_text)
    issues = list(collect_review_issues(view, replace(facts, fields=view_fields)))
    parsed_deadline = known_deadline(deadline.value) if deadline else None
    if parsed_deadline is not None:
        quoted_dates = {known_deadline(match.group(0)) for ref in deadline.evidence_refs
            for match in re.finditer(r"20\d{2}\s*(?:年|[-/.])\s*\d{1,2}\s*(?:月|[-/.])\s*\d{1,2}\s*日?", ref.quote)}
        if parsed_deadline not in quoted_dates:
            issues.append(_guided_issue("fields.deadline", "deadline_evidence_missing",
                "完整截止日期缺少含相同年月日的当前原文依据，不能补造年份", "请提供官方完整日期的连续原文，并定位该日期证据"))
    if blank_deadline or (deadline and deadline.value == UNSPECIFIED_DEADLINE):
        # Body replacement cannot erase a date constraint retained in the OCR.
        # A supported current dated correction is handled by the effective-text
        # collector above; this guard specifically protects the absent-date path.
        raw_view = SimpleNamespace(source_url=record.source_url, material_complete=complete,
            ocr_text=record.ocr_text, edited_text="", supplemental_text=record.supplemental_text)
        raw_date_issues = [issue for issue in collect_review_issues(raw_view, replace(facts, fields=view_fields))
            if issue.path == "fields.deadline" and issue.severity == "error"]
        for issue in raw_date_issues:
            if not any(existing.path == issue.path and existing.code == issue.code for existing in issues):
                issues.append(issue)
        if raw_date_issues:
            deadline_unknown_allowed = False
    original_fee = _fee_result("\n".join((record.ocr_text or "", text)))
    if original_fee and not any(issue.code == "recruitment_fee" for issue in issues):
        issues.append(_guided_issue("common_requirements", "recruitment_fee", original_fee.reason,
            "原始材料收费限制不能通过改写正文删除，请保留资料库"))
    projected = publication_facts(facts)
    evidence_fields = [(f"fields.{name}", field) for name, field in projected.fields.items()]
    evidence_fields += [(f"roles.{index}.{name}", field) for index, role in enumerate(projected.roles) for name, field in role.items()]
    evidence_fields += [(f"common_requirements.{index}", field) for index, field in enumerate(projected.common_requirements)]
    for path, field in evidence_fields:
        if any(not ref.quote or text[ref.text_start:ref.text_end] != ref.quote for ref in field.evidence_refs):
            issues.append(_guided_issue(path, "evidence_mismatch", "事实引用与当前完整原文不一致", "请重新定位当前原文的连续依据后保存"))
    if raw_unknown and blank_deadline:
        issues.append(_guided_issue("fields.deadline", "deadline_year_unknown", "原文截止日期缺少年份，不能按未规定截止时间保留", "请依据官方原文补齐年份，不能猜测"))
    screening = _screen(record, facts)
    existing = session.get(Job, record.job_id) if record.job_id else None
    if screening.grade == "D" or (existing and existing.intake_grade == "D"):
        reason = existing.intake_reason if existing and existing.intake_grade == "D" else screening.reason
        issues.append(_guided_issue("fields.target_audience", "screening_rejected", f"D级内容不能成稿：{reason}",
            "请保留资料库，或按真实岗位边界创建派生导入"))
    elif (existing and (existing.intake_grade == "C" or "[TARGET_AUDIENCE_UNCLEAR]" in existing.intake_reason)) or (
            existing is None and (screening.grade == "C" or "[TARGET_AUDIENCE_UNCLEAR]" in screening.reason)):
        issues.append(_guided_issue("fields.target_audience", "audience_confirmation_required",
            "学生受众尚未明确，须补充当前官方受众确认证据", "请提供当前官方链接、连续受众摘录及人工判断说明"))
    selected_indices = json.loads(record.selected_roles_json or "[]")
    if selected_indices:
        for index, issue in enumerate(issues):
            match = re.match(r"roles\.(\d+)(\..*)?$", issue.path)
            if match and int(match.group(1)) < len(selected_indices):
                issues[index] = replace(issue, path=f"roles.{selected_indices[int(match.group(1))]}{match.group(2) or ''}")
    return tuple(issues), facts, bool(ocr_complete), deadline_unknown_allowed


def get_guided_review_issues(session, record):
    return guided_review_readiness(session, record)[0]


def confirm_guided_screenshot_import(session: Session, import_id: int, data: dict, *, action: str,
                                     expected_row_version: int, expected_job_row_version=None,
                                     expected_job_version=None, actor=None) -> ConfirmationResult:
    """Commit safe edits first; reserve their versions for atomic formal approval."""
    if action not in {"submit", "approve_and_generate"} or not isinstance(data, dict):
        raise ValueError("未知确认操作")
    user = _actor(session, actor, "business.edit")
    if action == "approve_and_generate":
        user = _actor(session, user, "review.decide")
        user = _actor(session, user, "distribution.publish")
    edits = data.get("edits", {})
    if "fields" in data:
        edits = {**edits, "fields": data["fields"]}
    record = save_import_edits(session, import_id, edits, expected_row_version=expected_row_version,
        expected_job_row_version=expected_job_row_version, expected_job_version=expected_job_version, actor=user, commit=False)
    saved_job = session.get(Job, record.job_id) if record.job_id else None
    own_saved_versions = {"expected_row_version": record.row_version, "content_revision": record.content_revision,
        "expected_job_row_version": saved_job.row_version if saved_job else None,
        "expected_job_version": saved_job.version if saved_job else None}
    session.commit()
    try:
        record, _ = _reserve(session, import_id, own_saved_versions["expected_row_version"],
            own_saved_versions["expected_job_row_version"], own_saved_versions["expected_job_version"])
        issues, facts, ocr_complete, unknown_date = guided_review_readiness(session, record)
        issues = list(issues)
        acknowledged = data.get("acknowledge") is True
        if not acknowledged:
            issues.append(_guided_issue("acknowledge", "acknowledgement_required", "请明确确认已核对本版关键内容", "核对摘要、预览和原文后点击最终确认按钮"))
        if acknowledged and ocr_complete:
            record.material_complete = True
        if any(issue.code == "audience_confirmation_required" for issue in issues) and data.get("audience_confirmation"):
            candidate = session.get(Job, record.job_id) if record.job_id else None
            if candidate is None:
                screening = _screen(record, facts)
                candidate = SimpleNamespace(**_job_values(record, facts), intake_grade=screening.grade,
                    intake_reason=screening.reason, lifecycle_status="正常")
            try:
                if _confirm_audience(session, record, candidate, data, user, validate_only=True):
                    issues = [issue for issue in issues if issue.code != "audience_confirmation_required"]
            except ValueError as exc:
                issues.append(_guided_issue("fields.target_audience", "audience_confirmation_invalid", str(exc)))
        scope_resolution = None
        try:
            scope_resolution = _resolve_scope(record, facts, data)
            if scope_resolution:
                issues = [issue for issue in issues if not (issue.path == "fields.posting_scope" and issue.code == "scope_unresolved")]
        except ValueError as exc:
            issues.append(_guided_issue("fields.posting_scope", "scope_confirmation_required", str(exc)))
        if issues:
            raise ScreenshotReviewValidationError(issues)
        job = _create_job(session, record, user)
        if job.lifecycle_status != "正常" or job.status in {"已截止", "已撤销", "淘汰"}:
            raise ScreenshotReviewValidationError((_guided_issue("fields", "job_inactive", "已截止、撤销或淘汰岗位不能确认"),))
        try:
            _confirm_audience(session, record, job, data, user)
        except ValueError as exc:
            raise ScreenshotReviewValidationError((_guided_issue("fields.target_audience", "audience_confirmation_invalid", str(exc)),)) from exc
        if job.intake_grade not in {"A", "B"} or "[TARGET_AUDIENCE_UNCLEAR]" in job.intake_reason:
            raise ScreenshotReviewValidationError((_guided_issue("fields.target_audience", "audience_confirmation_required", "学生受众尚未明确，草稿保留待人工确认", "请提供当前官方链接、连续受众摘录及人工判断说明"),))
        if job.notice_type != "新招聘":
            raise ScreenshotReviewValidationError((_guided_issue("fields.notice_type", "notice_unconfirmed", "请确认内容为新招聘"),))
        note = _string(data.get("note", ""), "核验备注", 2000) or "用户明确核对本版摘要、原文及关键内容，一次确认六项核验范围"
        published = publication_facts(facts)
        values = _job_values(record, published)
        if unknown_date:
            values["deadline"] = UNSPECIFIED_DEADLINE
        if job.status in {"待审核", "可发布"}:
            job.status, job.verification_version, job.verification_checks = "待核验", 0, "{}"
        refs = [ref.to_dict() for role in published.roles for field in role.values() for ref in field.evidence_refs]
        details_checked = bool(published.roles) and all(role["job_title"].evidence_refs for role in published.roles)
        checks = {name: True for name in REQUIRED_VERIFICATION_CHECKS}
        struct = StructuringInput(**{name: values.get(name, "") for name in (
            "employer_name", "job_title", "job_family", "recruitment_type", "location_category", "location_detail",
            "target_audience", "direction_tags", "deadline", "official_url", "posting_scope", "attachment_status",
            "application_method", "application_contact", "student_fit_level", "distribution_recommendation", "announcement_title")},
            quality_score=job.quality_score, note=note, verification_checks=checks,
            role_details_status="checked" if details_checked else "unknown", role_details_evidence=_json(refs) if details_checked else "",
            review_policy=REVIEW_POLICY_VERSION)
        structure_job(session, job.id, struct, _label(user), expected_row_version=job.row_version, commit=False)
        excerpt = (bool(facts.roles) and len(facts.roles) < len(read_import_facts(record).roles)) or bool(scope_resolution and scope_resolution["selected_employer_excerpt"])
        snapshot = build_confirmed_content_snapshot(published, import_id=record.id,
            content_revision=record.content_revision, student_roles_excerpt=excerpt)
        snapshot["fields"].update({name: getattr(job, name) for name in JOB_FIELDS})
        snapshot["fields"].update(source_name=record.source_name, source_url=record.source_url, notice_type=job.notice_type)
        snapshot["review_confirmation"] = build_review_confirmation(snapshot, job_id=job.id, job_version=job.version,
            actor_id=user.id if user else None, confirmed_at=datetime.now().isoformat())
        job.confirmed_content_json, job.confirmed_content_version = _json(snapshot), job.version
        if scope_resolution:
            ledger = json.loads(record.evidence_json or "[]")
            record.evidence_json = _json([*ledger, {**scope_resolution, "actor_id": user.id if user else None}])
            _audit(session, record, user, "screenshot.scope_confirmed", scope_resolution)
        ids = ()
        if action == "approve_and_generate":
            review_job(session, job.id, "approve", note, _label(user), actor_user_id=user.id if user else None, commit=False)
            ids = tuple(item.id for item in create_distribution_items(session, job.id, commit=False))
        job.updated_by_user_id = user.id if user else None
        _audit(session, record, user, "screenshot.guided_confirmed", {"job_id": job.id, "action": action,
            "job_version": job.version, "content_revision": record.content_revision, "acknowledge": True,
            "verification_scope": list(REQUIRED_VERIFICATION_CHECKS)})
        session.commit()
        return ConfirmationResult(record.id, job.id, "publishable" if action == "approve_and_generate" else "pending_review", ids)
    except Exception as exc:
        session.rollback()
        exc.draft_saved = True
        exc.own_saved_versions = own_saved_versions
        raise
