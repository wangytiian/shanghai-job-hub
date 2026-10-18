import importlib
import importlib.util
import json

import pytest

from app.models import DistributionItem, Job, ScreenshotAsset, ScreenshotImport
from app.services.concurrency import EditConflict
from app.services.screenshot_extraction import facts_to_dict, parse_extraction_chunk


def workflow():
    assert importlib.util.find_spec("app.services.screenshot_workflow") is not None, "截图工作区服务尚未实现"
    return importlib.import_module("app.services.screenshot_workflow")


def make_import(session, *, text=None, fields=None, roles=None, common=None, **changes):
    source = text or "星辰公司招聘财务实习生。岗位职责：报表整理。任职要求：本科，应届生可投。工作地点：上海。报名截止：2099年12月31日。官方投递邮箱：jobs@example.com。"
    values = {
        "employer_name": "星辰公司", "job_title": "财务实习生", "notice_type": "新招聘",
        "job_family": "财会审计", "recruitment_type": "实习", "location_category": "明确上海", "location_detail": "上海",
        "target_audience": "应届生", "direction_tags": "财会", "deadline": "2099-12-31", "official_url": "https://example.com/jobs",
        "application_method": "email", "application_contact": "jobs@example.com", "posting_scope": "single_role",
        "attachment_status": "not_required", "student_fit_level": "核心适配", "distribution_recommendation": "进入学生分发审核",
    }
    values.update(fields or {})
    model_fields = {name: {"value": value, "state": "explicit", "quotes": [source]} for name, value in values.items() if value}
    # Add the official URL to the material so the parser can prove it.
    if "https://example.com/jobs" not in source:
        source += "\n官方链接：https://example.com/jobs"
    model_fields = {name: {**value, "quotes": [source]} for name, value in model_fields.items()}
    facts = parse_extraction_chunk(json.dumps({"fields": model_fields, "roles": roles or [], "common_requirements": common or []}, ensure_ascii=False), source)
    record = ScreenshotImport(submission_key="test-import-" + str(session.query(ScreenshotImport).count()),
        source_name="客户提供材料", source_url="https://example.com/source", title="星辰公司招聘", ocr_text=source,
        fields_json=json.dumps(facts_to_dict(facts), ensure_ascii=False), material_complete=True, status="ready")
    for key, value in changes.items():
        setattr(record, key, value)
    session.add(record)
    session.commit()
    return record


def call_save(session, record, edits, **kwargs):
    job = session.get(Job, record.job_id) if record.job_id else None
    expected = {"expected_row_version": record.row_version, "expected_job_row_version": job.row_version if job else None,
                "expected_job_version": job.version if job else None, "actor": None}
    expected.update(kwargs)
    return workflow().save_import_edits(session, record.id, edits, **expected)


def create_job(session, record, **kwargs):
    return workflow().create_pending_job_from_import(session, record.id, expected_row_version=record.row_version, actor=None, **kwargs)


def valid_data(**changes):
    data = {"notice_type": "新招聘", "quality_score": 85, "verification_checks": {
        key: True for key in ("source_checked", "scope_checked", "audience_checked", "location_checked", "application_checked", "timeliness_checked")}}
    data.update(changes)
    return data


def test_save_preserves_ocr_and_uses_revision_check(session):
    record = make_import(session)
    original = record.ocr_text
    old_version = record.row_version
    call_save(session, record, {"edited_text": original + "\n人工补充：报名材料需成绩单。"})
    assert record.ocr_text == original
    assert record.content_revision == 2
    assert record.edited_text.endswith("成绩单。")
    with pytest.raises(EditConflict):
        call_save(session, record, {"edited_text": "过时表单"}, expected_row_version=old_version)
    session.refresh(record)
    assert record.edited_text.endswith("成绩单。")


def test_intake_does_not_fake_quality_or_verification_and_is_idempotent(session):
    record = make_import(session)
    job = create_job(session, record)
    assert job.status == "待核验" and job.intake_grade == "A"
    assert job.quality_score == 0 and job.verification_version == 0
    assert job.verification_checks == "{}"
    assert "客户提供材料" in job.evidence_note
    again = workflow().create_pending_job_from_import(session, record.id, expected_row_version=record.row_version,
        expected_job_row_version=job.row_version, expected_job_version=job.version, actor=None)
    assert again.id == job.id and session.query(Job).count() == 1


def test_bound_edit_checks_both_job_versions_and_invalidates_old_copy(session):
    record = make_import(session)
    job = create_job(session, record)
    job.status = "可发布"
    job.confirmed_content_json = '{"schema_version":1}'
    job.confirmed_content_version = job.version
    job.verification_version = job.version
    job.verification_checks = '{"source_checked":true}'
    item = DistributionItem(job_id=job.id, channel="微信群", audience_group="学生", content="旧稿", job_version=job.version)
    session.add(item)
    session.commit()
    before = job.version
    call_save(session, record, {"fields": {"deadline": "2099-11-30"}})
    assert job.version == before + 1 and job.status == "待核验"
    assert job.verification_version == 0 and job.confirmed_content_version == 0
    assert item.job_version != job.version
    from app.services.distribution import distribution_blockers
    assert distribution_blockers(job, item)


def test_other_job_page_edit_cannot_be_overwritten_from_screenshot_page(session):
    record = make_import(session)
    job = create_job(session, record)
    previous_job_row, previous_fact = job.row_version, job.version
    job.row_version += 1
    job.version += 1
    job.deadline = "2099-09-30"
    session.commit()
    with pytest.raises(EditConflict):
        call_save(session, record, {"fields": {"deadline": "2099-11-30"}},
            expected_job_row_version=previous_job_row, expected_job_version=previous_fact)
    session.refresh(job)
    assert job.deadline == "2099-09-30"


def test_bound_save_cannot_omit_job_versions(session):
    record = make_import(session)
    create_job(session, record)
    with pytest.raises(EditConflict):
        workflow().save_import_edits(session, record.id, {"title": "旧页"}, expected_row_version=record.row_version, actor=None)


def test_missing_facts_can_be_saved_without_claiming_complete(session):
    record = make_import(session, fields={"employer_name": "", "official_url": "", "location_detail": ""}, source_url="", material_complete=False)
    job = create_job(session, record)
    assert job.official_url == "" and job.quality_score == 0
    assert job.status == "待核验"


def test_asset_reordering_requires_exact_current_asset_ids(session):
    record = make_import(session)
    asset = ScreenshotAsset(import_id=record.id, storage_key="random.png", pixel_sha256="pixel1", position=0)
    session.add(asset)
    session.commit()
    with pytest.raises(ValueError):
        call_save(session, record, {"asset_order": [asset.id, 999]})
    session.refresh(asset)
    assert asset.position == 0


def test_manual_field_with_no_source_quote_remains_unclear(session):
    record = make_import(session)
    call_save(session, record, {"fields": {"salary": "8000元"}})
    facts = workflow().read_import_facts(record)
    assert facts.fields["salary"].state == "unclear"
    assert not facts.fields["salary"].evidence_refs


def test_derive_d_import_keeps_original_and_remaps_own_asset_evidence(session):
    from app.models import ScreenshotPiece
    from app.services.screenshot_contracts import EvidenceRef, ExtractedField, ScreenshotFacts
    record = make_import(session)
    service = workflow()
    asset = ScreenshotAsset(import_id=record.id, storage_key="private.png", pixel_sha256="original-pixel")
    session.add(asset)
    session.flush()
    piece = ScreenshotPiece(asset_id=asset.id, piece_index=0, storage_key="private-piece.png", text=record.ocr_text, status="succeeded")
    session.add(piece)
    session.flush()
    facts = service.read_import_facts(record)
    title_ref = EvidenceRef(asset.id, piece.id, 0, len(record.ocr_text), record.ocr_text, "ocr")
    roles = ({"job_title": ExtractedField("财务实习生", "explicit", (title_ref,))},)
    record.fields_json = json.dumps(facts_to_dict(ScreenshotFacts(facts.fields, roles, (), 1)), ensure_ascii=False)
    session.commit()
    job = create_job(session, record)
    job.intake_grade = "D"
    job.intake_reason = "原公告另有高级岗，原记录保留"
    session.commit()
    derived = service.create_derived_import(session, record.id, [0], expected_row_version=record.row_version,
        expected_job_row_version=job.row_version, expected_job_version=job.version, actor=None)
    assert derived.parent_import_id == record.id and derived.job_id is None
    own_asset = session.query(ScreenshotAsset).filter_by(import_id=derived.id).one()
    own_piece = session.query(ScreenshotPiece).filter_by(asset_id=own_asset.id).one()
    ref = service.read_import_facts(derived).roles[0]["job_title"].evidence_refs[0]
    assert (ref.asset_id, ref.piece_id) == (own_asset.id, own_piece.id)
    assert own_asset.storage_key == asset.storage_key
    assert job.intake_grade == "D" and session.get(ScreenshotImport, record.id).job_id == job.id


def test_selected_role_malformed_nested_input_is_value_error(session):
    record = make_import(session, selected_roles_json='[[0]]')
    with pytest.raises(ValueError):
        create_job(session, record)


def test_private_original_piece_text_survives_asset_reorder(session):
    from app.models import ScreenshotPiece
    record = make_import(session)
    assets = [ScreenshotAsset(import_id=record.id, storage_key=f"{index}.png", pixel_sha256=f"pixel{index}", position=index) for index in range(2)]
    session.add_all(assets)
    session.flush()
    pieces = [ScreenshotPiece(asset_id=asset.id, piece_index=0, storage_key=f"piece{index}.png", text=f"第{index}图", status="succeeded") for index, asset in enumerate(assets)]
    session.add_all(pieces)
    session.commit()
    call_save(session, record, {"asset_order": [assets[1].id, assets[0].id]})
    assert record.ocr_text == "第1图\n第0图"
    assert [piece.text for piece in pieces] == ["第0图", "第1图"]


def role_import(session):
    from app.services.screenshot_contracts import EvidenceRef, ExtractedField, ScreenshotFacts
    text = "星辰公司招聘财务实习生和研究实习生。\n财务实习生：本科，资格更正以官方补充为准。\n研究实习生：硕士。\n工作地点：上海。报名截止：2099年12月31日。官方投递邮箱：jobs@example.com。"
    record = make_import(session, text=text)
    facts = workflow().read_import_facts(record)
    def field(value, quote):
        start = record.ocr_text.index(quote)
        return ExtractedField(value, "explicit", (EvidenceRef(None, None, start, start + len(quote), quote, "manual"),))
    roles = (
        {"job_title": field("财务实习生", "财务实习生：本科，资格更正以官方补充为准。"), "eligibility": field("本科", "财务实习生：本科，资格更正以官方补充为准。")},
        {"job_title": field("研究实习生", "研究实习生：硕士。"), "eligibility": field("硕士", "研究实习生：硕士。")},
    )
    record.fields_json = json.dumps(facts_to_dict(ScreenshotFacts(facts.fields, roles, (), 1)), ensure_ascii=False)
    session.commit()
    return record


def test_correcting_finance_role_does_not_change_research_role(session):
    record = role_import(session)
    call_save(session, record, {"supplemental_text": "官方更正：财务实习生要求大专及以上。", "role_fields": {"0": {"eligibility": "大专及以上"}},
        "role_evidence": {"0": {"eligibility": "官方更正：财务实习生要求大专及以上。"}}})
    facts = workflow().read_import_facts(record)
    assert facts.roles[0]["eligibility"].value == "大专及以上" and facts.roles[0]["eligibility"].state == "explicit"
    assert facts.roles[1]["eligibility"].value == "硕士"


def test_role_edit_out_of_range_or_cross_role_quote_is_rejected(session):
    record = role_import(session)
    with pytest.raises(ValueError, match="岗位"):
        call_save(session, record, {"role_fields": {"20": {"eligibility": "大专"}}})
    with pytest.raises(ValueError, match="岗位"):
        call_save(session, record, {"role_fields": {"0": {"eligibility": "硕士"}}, "role_evidence": {"0": {"eligibility": "研究实习生：硕士。"}}})
    assert workflow().read_import_facts(record).roles[0]["eligibility"].value == "本科"


def test_unproved_role_correction_stays_unclear(session):
    record = role_import(session)
    call_save(session, record, {"role_fields": {"0": {"salary": "10000元"}}})
    assert workflow().read_import_facts(record).roles[0]["salary"].state == "unclear"


def test_reorder_rebases_field_quote_locations_to_the_new_merged_text(session):
    from app.models import ScreenshotPiece
    record = make_import(session)
    assets = [ScreenshotAsset(import_id=record.id, storage_key=f"ref-{index}.png", pixel_sha256=f"refpixel{index}", position=index) for index in range(2)]
    session.add_all(assets)
    session.flush()
    session.add_all([ScreenshotPiece(asset_id=assets[0].id, piece_index=0, storage_key="ref-piece0.png", text=record.ocr_text, status="succeeded"),
        ScreenshotPiece(asset_id=assets[1].id, piece_index=0, storage_key="ref-piece1.png", text="前置补充文字", status="succeeded")])
    session.commit()
    call_save(session, record, {"asset_order": [assets[1].id, assets[0].id]})
    ref = workflow().read_import_facts(record).fields["employer_name"].evidence_refs[0]
    assert ref.text_start == len("前置补充文字\n")
    assert record.ocr_text[ref.text_start:ref.text_end] == ref.quote
