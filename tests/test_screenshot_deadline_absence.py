"""An explicitly unspecified deadline is not a malformed dated deadline."""
import pytest
from bs4 import BeautifulSoup

from app.models import Job
from app.services.distribution import distribution_blockers, load_confirmed_snapshot
from app.services.screenshot_review import UNSPECIFIED_DEADLINE
from test_screenshot_guided_confirmation import guided
from test_screenshot_guided_rules import errors, ready
from test_screenshot_guided_ui import guided_context, render
from test_screenshot_workflow import make_import, workflow


@pytest.mark.parametrize("clause", [
    "公告未明确统一截止时间",
    "公告未明确统一截止日期",
    "报名截止时间：未明确",
    "截止日期：未公布",
])
def test_source_explicitly_without_deadline_is_not_an_invalid_date(clause):
    record, facts = ready(deadline=UNSPECIFIED_DEADLINE)
    record.ocr_text += "\n" + clause + "。"
    assert errors(record, facts) == []


def test_absence_wording_does_not_require_a_latest_label_on_a_valid_dated_clause():
    record, facts = ready()
    record.ocr_text += "\n公告未明确统一截止时间。报名截止：2099年12月31日。"
    assert errors(record, facts) == []


@pytest.mark.parametrize("deadline", ["", UNSPECIFIED_DEADLINE])
def test_guided_confirmation_accepts_no_deadline_source_and_keeps_warning(session, deadline):
    text = ("星辰公司招聘财务实习生，应届生可投。工作地点：上海。"
            "官方投递邮箱：jobs@example.com。公告未明确统一截止时间。")
    record = make_import(session, text=text, fields={"deadline": deadline})
    assert workflow().get_guided_review_issues(session, record) == ()
    result = guided(session, record)
    job = session.get(Job, result.job_id)
    assert job.deadline == UNSPECIFIED_DEADLINE
    assert load_confirmed_snapshot(job)["fields"]["deadline"] == UNSPECIFIED_DEADLINE
    assert distribution_blockers(job) == []
    assert len(result.distribution_ids) == 2


@pytest.mark.parametrize("separator", ["。", "，"])
@pytest.mark.parametrize("restriction,code", [
    ("报名截止：10月31日", "deadline_unresolved"),
    ("报名截止：2099年2月30日", "deadline_unresolved"),
    ("报名截止：2001年12月31日", "deadline_expired"),
    ("报名截止：2099年11月30日。报名截止：2099年12月31日", "deadline_conflict"),
])
def test_no_deadline_wording_cannot_hide_a_real_restriction(separator, restriction, code):
    record, facts = ready(deadline=UNSPECIFIED_DEADLINE)
    record.ocr_text += "\n公告未明确统一截止时间" + separator + restriction + "。"
    assert any(issue.code == code for issue in errors(record, facts))


def test_deadline_field_explains_optional_absence_instead_of_an_inexistent_choice():
    soup = BeautifulSoup(render("screenshot_review.html", **guided_context(
        field_labels={"deadline": "报名截止日期"})), "html.parser")
    assert "选填" in soup.select_one('label[for="field-deadline"]').get_text()
    hint = soup.select_one("#deadline-help")
    assert hint and "留空" in hint.get_text() and UNSPECIFIED_DEADLINE in hint.get_text()
    assert soup.select_one("#field-deadline")["aria-describedby"] == "deadline-help"
