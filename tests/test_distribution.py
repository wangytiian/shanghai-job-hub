import pytest

from app.models import Job
from app.services.distribution import build_wechat_draft, create_distribution_items
from app.services.tasks import run_demo_collection


def test_second_demo_collection_does_not_duplicate_jobs(session):
    run_demo_collection(session)
    run_demo_collection(session)

    assert session.query(Job).count() == 10


def test_publishable_job_creates_public_article_and_matching_group_message(session, pending_review_job):
    pending_review_job.status = "可发布"
    session.commit()

    items = create_distribution_items(session, pending_review_job.id)

    assert {item.channel for item in items} == {"公众号", "微信群"}
    article = next(item for item in items if item.channel == "公众号")
    assert pending_review_job.official_url in article.content
    assert pending_review_job.target_audience in next(
        item.audience_group for item in items if item.channel == "微信群"
    )


def test_non_publishable_job_cannot_create_distribution_items(session, pending_review_job):
    with pytest.raises(ValueError, match="可发布"):
        create_distribution_items(session, pending_review_job.id)


def test_wechat_draft_uses_inline_styles_and_keeps_official_facts(session, pending_review_job):
    draft = build_wechat_draft(pending_review_job)

    assert draft.title == "示例金融集团｜财务分析实习生"
    assert pending_review_job.official_url in draft.html
    assert "招聘速览" in draft.html
    assert "沪上求职汇" in draft.html
    assert "AI辅助建议" not in draft.html
    assert "非官方就业信息服务" in draft.html
    assert "style=" in draft.html
    assert "class=" not in draft.html
    assert pending_review_job.official_url in draft.plain_text


def test_wechat_draft_for_email_application_uses_email_and_hides_unknown_location(session, pending_review_job):
    pending_review_job.application_method = "email"
    pending_review_job.application_contact = "apply@example.edu.cn"
    pending_review_job.location_detail = "以公告原文为准"

    draft = build_wechat_draft(pending_review_job)

    assert "apply@example.edu.cn" in draft.html
    assert "请将材料发送至" in draft.html
    assert "工作地点" not in draft.html
    assert "AI辅助建议" not in draft.html


def test_wechat_draft_for_checked_multi_role_announcement_uses_summary_copy(session, pending_review_job):
    pending_review_job.posting_scope = "multi_role_announcement"
    pending_review_job.attachment_status = "checked"
    pending_review_job.job_title = "2026年公开招聘公告"

    draft = build_wechat_draft(pending_review_job)

    assert "公告速览" in draft.html
    assert "正在招聘财务分析实习生" not in draft.html


def test_wechat_draft_explains_unstated_deadline(session, pending_review_job):
    pending_review_job.deadline = "公告未明确统一截止时间"

    draft = build_wechat_draft(pending_review_job)

    assert "建议尽快查看官方原文或附件确认报名安排" in draft.html
    assert "请在截止日期前" not in draft.html
