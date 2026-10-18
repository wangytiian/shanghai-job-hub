"""Template contracts without importing the app or starting HTTP/network clients."""

from pathlib import Path
from types import SimpleNamespace
import shutil
import subprocess

import pytest

from jinja2 import Environment, FileSystemLoader, select_autoescape
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]


def render(name, **context):
    environment = Environment(loader=FileSystemLoader(ROOT / "app/templates"), autoescape=select_autoescape())
    defaults = dict(request=SimpleNamespace(state=SimpleNamespace(current_user=None)), cloud_runtime=False,
        active_nav="screenshots", global_status=SimpleNamespace(pending_work_count=0, unhealthy_source_count=0,
        last_task_at="", operator_name="测试"))
    return environment.get_template(name).render(**defaults, **context)


def context():
    return dict(batch=SimpleNamespace(id=1, public_id="example", status="ready", source_name="招聘来源",
        source_url="", title="", supplemental_text="", edited_text="人工正文", ocr_text="原始识别正文",
        row_version=3, content_revision=2, error_message="", error_code="", material_complete=False,
        selected_roles_json="[]", job_id=None), assets=[], pieces=[], facts=None,
        values={"employer_name": '<script>alert("x")</script>'}, field_labels={"employer_name":"招聘单位"},
        job=None, item=None, draft=None, group_message="内部草稿 · 未审核", error="", feedback="",
        verification_checks={"source_checked":"原始来源", "scope_checked":"岗位或公告范围",
            "audience_checked":"面向学生人群", "location_checked":"工作地点",
            "application_checked":"官方投递入口", "timeliness_checked":"时效"}, can_review=True)


def test_upload_template_exposes_bounded_multiple_input_and_preserves_source():
    soup = BeautifulSoup(render("screenshot_import.html", recent=[], submission_key="key-1",
                               source_url="https://mp.weixin.qq.com/s/example"), "html.parser")
    form = soup.select_one("form[data-screenshot-upload]")
    assert form and form.get("method").lower() == "post"
    assert form.select_one('input[name="images"][multiple]')
    assert form.select_one('[name="submission_key"]')["value"] == "key-1"
    assert form.select_one('[name="source_url"]')["value"] == "https://mp.weixin.qq.com/s/example"
    assert form.select_one('[name="supplemental_text"][maxlength="24000"]')
    assert "图片将发送至已配置的识别服务" in soup.get_text()


def test_review_template_retains_versions_and_manual_confirmation_controls():
    html = render("screenshot_review.html", **context())
    soup = BeautifulSoup(html, "html.parser")
    form = soup.select_one("form[data-screenshot-review]")
    assert form.select_one('[name="expected_row_version"]')["value"] == "3"
    assert form.select_one('[name="expected_job_row_version"]')
    assert form.select_one('[name="expected_job_version"]')
    assert form.select_one('[name="field_employer_name"]')["value"] == '<script>alert("x")</script>'
    assert "<script>alert" not in html
    assert form.select_one('[name="edited_text"]').get_text() == "人工正文"
    assert form.select_one('[name="supplemental_text"][maxlength="24000"]')
    for name in ("material_complete", "role_details_checked", "deadline_unspecified_confirmed",
                 "source_checked", "scope_checked", "audience_checked", "location_checked", "application_checked", "timeliness_checked"):
        assert form.select_one(f'[name="{name}"]')
    assert form.select_one('[name="quality_score"]').get("value", "") == ""
    assert not soup.select('[data-copy-key]')


def test_formal_copy_controls_use_server_payload_versions():
    data = context()
    data.update(job=SimpleNamespace(status="可发布", version=5, row_version=7, quality_score=85,
        verification_checks="{}", verification_version=5, intake_grade="A"),
        item=SimpleNamespace(id=9, row_version=11))
    soup = BeautifulSoup(render("screenshot_review.html", **data), "html.parser")
    controls = soup.select_one('[data-copy-endpoint]')
    assert controls["data-copy-endpoint"] == "/distribution/9/copy-payload?expected_job_version=5&expected_item_row_version=11"
    assert {node["data-copy-key"] for node in controls.select('[data-copy-key]')} == {"title", "digest", "html", "group_message"}


def test_client_fetches_current_payload_and_keeps_manual_form_during_poll():
    script = (ROOT / "app/static/screenshot_import.js").read_text(encoding="utf-8")
    assert "X-CSRF-Token" in script and "FormData" in script
    assert "dirty" in script and "setTimeout" in script and "response.ok" in script
    old = (ROOT / "app/templates/wechat_draft.html").read_text(encoding="utf-8")
    assert "copy-payload?expected_job_version=" in old
    assert "copyText(button.dataset.copyText)" not in old


def test_each_role_can_be_edited_and_given_its_own_evidence():
    from app.services.screenshot_contracts import ExtractedField, ScreenshotFacts
    data = context()
    data["facts"] = ScreenshotFacts({}, (
        {"job_title": ExtractedField("财务实习岗", "explicit", ()), "eligibility": ExtractedField("本科", "explicit", ())},
        {"job_title": ExtractedField("研究助理岗", "explicit", ()), "eligibility": ExtractedField("硕士", "explicit", ())},
    ), (), 2, (ExtractedField("统一官方报名", "explicit", ()),))
    soup = BeautifulSoup(render("screenshot_review.html", **data), "html.parser")
    assert soup.select_one('[name="role_0_eligibility"]').get_text() == "本科"
    assert soup.select_one('[name="role_1_eligibility"]').get_text() == "硕士"
    assert soup.select_one('[name="role_evidence_1_eligibility"]')
    assert "统一官方报名" in soup.get_text()


def test_common_conditions_have_separate_value_and_evidence_fields_inside_review_form():
    from app.services.screenshot_contracts import EvidenceRef, ExtractedField, ScreenshotFacts
    data = context()
    quote = "公共条件：所有岗位统一官方报名。"
    data["facts"] = ScreenshotFacts({}, (), (), 2, (
        ExtractedField("所有岗位统一官方报名", "conflict", (EvidenceRef(None, None, 0, len(quote), quote, "manual"),)),
        ExtractedField("所有应聘者不得提交虚假资料", "explicit", ()),
    ))
    soup = BeautifulSoup(render("screenshot_review.html", **data), "html.parser")
    form = soup.select_one("form[data-screenshot-review]")
    assert form.select_one('[name="common_0"]')
    assert form.select_one('[name="common_0"]').get_text() == "所有岗位统一官方报名"
    assert form.select_one('[name="common_1"]').get_text() == "所有应聘者不得提交虚假资料"
    assert form.select_one('[name="common_evidence_0"]').get_text() == ""
    assert form.select_one('[name="common_evidence_1"]')
    assert quote in form.get_text()
    assert "不能删除" in form.get_text() and "存在冲突" in form.get_text()


@pytest.mark.parametrize("warning", ["岗位边界不明确，请人工定位", "多个公司合集，请拆批", "岗位归属冲突"], ids=["role-boundary", "multiple-companies", "ownership"])
def test_scope_resolution_is_explicit_unchecked_and_uses_actual_route_names(warning):
    from app.services.screenshot_contracts import ScreenshotFacts
    data = context()
    data["facts"] = ScreenshotFacts({}, (), (warning,), 2)
    soup = BeautifulSoup(render("screenshot_review.html", **data), "html.parser")
    form = soup.select_one("form[data-screenshot-review]")
    scope = form.select_one('[data-scope-confirmation]')
    assert scope
    for name in ("scope_single_employer", "scope_role_boundaries"):
        checkbox = scope.select_one(f'input[name="{name}"]')
        assert checkbox and not checkbox.has_attr("checked")
    assert scope.select_one('[name="scope_employer_name"]')
    assert scope.select_one('[name="scope_excerpt"]')
    assert scope.select_one('[name="scope_note"]')
    assert "公司" in scope.get_text() and "所选全部岗位" in scope.get_text()
    assert "连续原文" in scope.get_text() and "拆批" in scope.get_text()


def test_scope_resolution_shows_for_multi_role_but_not_unrelated_warning():
    from app.services.screenshot_contracts import ExtractedField, ScreenshotFacts
    data = context()
    data["facts"] = ScreenshotFacts({}, (), ("图片分片已保留，请核对邮箱",), 2)
    soup = BeautifulSoup(render("screenshot_review.html", **data), "html.parser")
    assert not soup.select_one('[data-scope-confirmation]')
    data["values"]["posting_scope"] = "multi_role_announcement"
    soup = BeautifulSoup(render("screenshot_review.html", **data), "html.parser")
    assert soup.select_one('[data-scope-confirmation]')


def test_multiple_employer_labels_show_scope_resolution_even_if_ai_warning_is_absent():
    data = context()
    data["batch"].edited_text = "招聘单位：甲公司\n岗位：财务岗\n招聘单位：乙公司\n岗位：研究岗"
    soup = BeautifulSoup(render("screenshot_review.html", **data), "html.parser")
    assert soup.select_one('[data-scope-confirmation]')


def test_polling_finishes_once_even_without_row_version_change():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable for the isolated JavaScript runtime check")
    script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
async function scenario(fieldName, eventName = 'input') {
  let timer, reloads = 0; const handlers = {};
  const review = {dataset:{reviewUrl:'/imports/screenshots/id',verification:'{}',selectedRoles:'[]'},elements:{namedItem:()=>null},querySelectorAll:()=>[],addEventListener:(name,fn)=>{handlers[name]=fn}};
  const nodes = {'[data-progress-label]':{},'[data-piece-count]':{},'[data-progress-error]':{}};
  const progress = {dataset:{initialStatus:'recognizing',rowVersion:'1',statusUrl:'/status'},querySelector:name=>nodes[name]};
  const document = {querySelector:name=>({'[data-screenshot-review]':review,'[data-screenshot-progress]':progress}[name]||null),querySelectorAll:()=>[],getElementById:()=>null};
  const window = {location:{reload:()=>{reloads++}},setTimeout:fn=>{timer=fn},addEventListener:()=>{}};
  const context = {document,window,console,Date,Set,JSON,Number,sessionStorage:{getItem:()=>null},fetch:async()=>({ok:true,json:async()=>({status:'ready',completed:1,total:1,row_version:1,error:''})})};
  vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
  if (fieldName) handlers[eventName]({target:{name:fieldName}});
  await timer();
  assert.equal(reloads,fieldName?0:1);
  if (fieldName) assert.match(nodes['[data-progress-error]'].textContent,/保存修改/);
}
scenario(null).then(()=>scenario('edited_text')).then(()=>scenario('common_0')).then(()=>scenario('common_evidence_0')).then(()=>scenario('scope_single_employer','change')).then(()=>scenario('scope_excerpt')).catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run([node, "-e", script, str(ROOT / "app/static/screenshot_import.js")],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
