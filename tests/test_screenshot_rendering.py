"""Deterministic dual-channel rendering, no external models."""

import json

import pytest
from sqlalchemy.orm import Session

from app.models import DistributionItem, Job
from app.services.ai_content_draft import ContentDraft
from app.services import distribution


def snapshot(job, *, excerpt=False):
    fields = {name: getattr(job, name) for name in (
        "employer_name", "job_title", "announcement_title", "recruitment_type", "location_detail",
        "target_audience", "direction_tags", "deadline", "official_url", "source_url", "posting_scope",
        "application_method", "application_contact",
    )}
    fields.update(employer_name="事实快照单位", application_method="email",
                  application_contact="confirmed@example.cn", posting_scope="multi_role_announcement")
    return {
        "schema_version": 1, "import_id": 1, "content_revision": 2, "fields": fields,
        "roles": [
            {"job_title": "财务实习岗", "role_summary": "核对财务账目", "eligibility": "本科在读，每周三天", "salary": "180元/天"},
            {"job_title": "研究助理岗", "role_summary": "整理研究数据", "eligibility": "硕士在读，统计专业", "salary": "200元/天"},
        ],
        "common_requirements": ["所有岗位均须在官方平台完成资料登记"],
        "evidence_refs": [], "student_roles_excerpt": excerpt,
    }


def confirm_snapshot(job, data=None):
    job.confirmed_content_json = json.dumps(data or snapshot(job), ensure_ascii=False)
    job.confirmed_content_version = job.version


def test_snapshot_retains_each_roles_conditions_in_both_channels_after_reopen(session, pending_review_job):
    job = pending_review_job
    job.status = "可发布"
    confirm_snapshot(job, snapshot(job, excerpt=True))
    session.commit()
    engine, identifier = session.get_bind(), job.id
    session.close()
    with Session(engine) as reopened:
        reloaded = reopened.get(Job, identifier)
        article = distribution.build_wechat_draft(reloaded)
        group = distribution.build_group_message(reloaded)
        for value in ("事实快照单位", "财务实习岗", "本科在读，每周三天", "180元/天", "研究助理岗",
                      "硕士在读，统计专业", "200元/天", "核对财务账目", "整理研究数据",
                      "所有岗位均须在官方平台完成资料登记", "confirmed@example.cn", "学生岗位节选"):
            assert value in article.html and value in article.plain_text and value in group
        assert "示例金融集团" not in article.plain_text


def test_stale_snapshot_blocks_formal_rendering_and_does_not_fall_back(session, pending_review_job):
    confirm_snapshot(pending_review_job)
    pending_review_job.version += 1
    for renderer in (distribution.build_wechat_draft, distribution.build_group_message):
        with pytest.raises(ValueError, match="快照.*过期"):
            renderer(pending_review_job)


def test_internal_mode_marks_draft_and_missing_facts(session, pending_review_job):
    data = snapshot(pending_review_job)
    data["fields"]["deadline"] = ""
    data["fields"]["location_detail"] = ""
    confirm_snapshot(pending_review_job, data)
    pending_review_job.confirmed_content_version = 0
    draft = distribution.build_wechat_draft(pending_review_job, mode="internal")
    assert "内部草稿 · 未审核" in draft.html and "内部草稿 · 未审核" in draft.plain_text
    assert "待补充" in draft.html
    assert "已核验" not in draft.html and "已核验" not in draft.plain_text
    assert "内部草稿 · 未审核" in distribution.build_group_message(pending_review_job, mode="internal")


def test_html_escapes_ai_opening_and_plain_text_includes_ai_conditions(session, pending_review_job):
    draft = distribution.build_wechat_draft(pending_review_job,
        ContentDraft(company_intro='<script>alert("x")</script>', eligibility="必须提交学校成绩单"))
    assert "<script>" not in draft.html
    assert "&lt;script&gt;" in draft.html
    assert "必须提交学校成绩单" in draft.plain_text


@pytest.mark.parametrize("malicious", ["javascript:alert(1)", "https://official.example/\" onmouseover=\"alert(1)", "data:text/html,test", "//example.com"])
def test_only_valid_http_links_are_rendered(session, pending_review_job, malicious):
    pending_review_job.official_url = malicious
    pending_review_job.source_url = malicious
    draft = distribution.build_wechat_draft(pending_review_job)
    assert 'href="javascript:' not in draft.html and 'href="data:' not in draft.html
    assert ' onmouseover=' not in draft.html
    assert malicious not in draft.plain_text


def test_legacy_group_message_contains_email_and_cleaned_tags(session, pending_review_job):
    job = pending_review_job
    job.application_method = "email"
    job.application_contact = "apply@example.cn"
    job.direction_tags = "['会计', '审计']"
    group = distribution.build_group_message(job)
    assert "apply@example.cn" in group
    assert "会计、审计" in group and "['会计'" not in group
    assert distribution._group_message(job) == group


@pytest.mark.parametrize("attribute,value,reason", [
    ("status", "待审核", "可发布"),
    ("lifecycle_status", "已撤销", "生命周期"),
    ("distribution_recommendation", "仅保留资料库", "资料库"),
    ("verification_version", 0, "核验"),
])
def test_uniform_distribution_blockers(session, pending_review_job, attribute, value, reason):
    pending_review_job.status = "可发布"
    setattr(pending_review_job, attribute, value)
    assert any(reason in message for message in distribution.distribution_blockers(pending_review_job))


def test_queue_uses_status_lifecycle_and_recommendation_gate(session, pending_review_job):
    job = pending_review_job
    job.status = "可发布"
    session.commit()
    items = distribution.create_distribution_items(session, job.id)
    job.lifecycle_status = "已撤销"
    session.commit()
    assert distribution.filter_current_queue_items(session, items) == []


def test_manually_edited_content_is_not_overwritten_or_upgraded_silently(session, pending_review_job):
    job = pending_review_job
    job.status = "可发布"
    session.commit()
    item = distribution.create_distribution_items(session, job.id)[0]
    item.content, item.is_manually_edited = "人工精修原文", True
    session.commit()
    distribution.create_distribution_items(session, job.id)
    assert item.content == "人工精修原文"
    job.version += 1
    job.verification_version = job.version
    session.commit()
    distribution.create_distribution_items(session, job.id)
    assert item.content == "人工精修原文"
    assert item.job_version < job.version
    assert distribution.distribution_blockers(job, item)


def test_no_commit_generation_rolls_back_with_outer_transaction(session, pending_review_job):
    job = pending_review_job
    job.status = "可发布"
    session.commit()
    distribution.create_distribution_items(session, job.id, commit=False)
    assert session.query(DistributionItem).count() == 2
    session.rollback()
    assert session.query(DistributionItem).count() == 0


def test_unknown_salary_is_not_invented_and_missing_snapshot_field_does_not_reuse_job(session, pending_review_job):
    data = snapshot(pending_review_job)
    for role in data["roles"]:
        role["salary"] = ""
    data["fields"].pop("location_detail")
    confirm_snapshot(pending_review_job, data)
    article = distribution.build_wechat_draft(pending_review_job)
    assert "高薪" not in article.plain_text and "薪资" not in article.plain_text
    assert "工作地点" not in article.plain_text


def test_invalid_snapshot_never_uses_legacy_fallback(session, pending_review_job):
    pending_review_job.confirmed_content_json = '{"schema_version":1}'
    pending_review_job.confirmed_content_version = pending_review_job.version
    with pytest.raises(ValueError, match="快照"):
        distribution.build_wechat_draft(pending_review_job)


def test_top_level_shared_duties_are_preserved_alongside_role_details(session, pending_review_job):
    data = snapshot(pending_review_job)
    data["fields"]["role_summary"] = "所有岗位均需参加统一轮岗培训"
    confirm_snapshot(pending_review_job, data)
    article = distribution.build_wechat_draft(pending_review_job)
    group = distribution.build_group_message(pending_review_job)
    assert "所有岗位均需参加统一轮岗培训" in article.plain_text
    assert "所有岗位均需参加统一轮岗培训" in group


def test_snapshot_builder_keeps_dates_provenance_and_all_public_conditions():
    from app.services.screenshot_contracts import EvidenceRef, ExtractedField, ScreenshotFacts
    evidence = EvidenceRef(1, 2, 0, 7, "本科每周三天", "ocr")
    requirement = ExtractedField("本科每周三天", "explicit", (evidence,))
    date = ExtractedField("10月30日", "explicit", (EvidenceRef(None, None, 8, 14, "10月30日", "manual"),))
    facts = ScreenshotFacts({"deadline": date}, ({"job_title": requirement, "eligibility": requirement},), (), 2, (requirement,))
    result = distribution.build_confirmed_content_snapshot(facts, import_id=1, content_revision=2,
                                                           student_roles_excerpt=True)
    assert result["fields"]["deadline_state"] == "explicit"
    assert result["fields"]["deadline_original_text"] == "10月30日"
    assert result["roles"][0]["eligibility"] == "本科每周三天"
    assert result["common_requirements"] == ["本科每周三天"]
    assert len(result["evidence_refs"]) == 2
    with pytest.raises(ValueError, match="版本"):
        distribution.build_confirmed_content_snapshot(facts, import_id=1, content_revision=3)


def test_invalid_evidence_shape_in_confirmed_snapshot_is_blocked(session, pending_review_job):
    data = snapshot(pending_review_job)
    data["evidence_refs"] = [{}]
    confirm_snapshot(pending_review_job, data)
    with pytest.raises(ValueError, match="证据"):
        distribution.build_wechat_draft(pending_review_job)


def test_confirmed_snapshot_deadline_itself_is_checked_for_expiry(session, pending_review_job):
    pending_review_job.status = "可发布"
    data = snapshot(pending_review_job)
    data["fields"]["deadline"] = "2001-01-01"
    confirm_snapshot(pending_review_job, data)
    assert any("截止" in message for message in distribution.distribution_blockers(pending_review_job))
