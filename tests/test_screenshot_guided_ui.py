"""Guided review presentation and HTTP contracts, using isolated data only."""
from dataclasses import replace
from types import SimpleNamespace
import json
import shutil
import subprocess

import pytest

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from app.models import ScreenshotImport
from app.services.screenshot_contracts import ExtractedField, ScreenshotFacts
from app.services.screenshot_extraction import facts_to_dict
from test_screenshot_frontend import context, render, ROOT
from test_screenshot_web import screenshot_app
from test_screenshot_workflow import make_import


def guided_context(**updates):
    data = context()
    data.update(guided_review=True, review_issues=[], readiness_versions={},
        deadline_unknown_allowed=True, ocr_complete=False)
    data.update(updates)
    return data


def test_guided_summary_and_two_internal_previews_precede_collapsed_fields():
    soup = BeautifulSoup(render("screenshot_review.html", **guided_context()), "html.parser")
    assert soup.select_one('[name="review_mode"][value="guided"]')
    advanced = soup.select_one('details[data-guided-advanced]')
    assert advanced and not advanced.has_attr('open')
    assert advanced.select_one('[name="field_employer_name"]')
    assert advanced.select_one('[name="edited_text"]')
    html = str(soup)
    assert html.index('data-guided-summary') < html.index('id="preview-group"') < html.index('data-guided-advanced')
    assert soup.select_one('#preview-article')
    assert '内部草稿' in soup.select_one('[data-guided-previews]').get_text()
    assert {'必填', '选填', '条件必填'} <= set(soup.select_one('[data-field-legend]').stripped_strings)


def test_guided_diagnostics_are_read_only_and_only_final_button_gives_consent():
    facts = ScreenshotFacts({'rationale': ExtractedField('<script>危险</script>', 'conflict', ())}, (), (), 2)
    data = guided_context(facts=facts, field_labels={'employer_name': '招聘单位', 'rationale': '判断说明'})
    soup = BeautifulSoup(render('screenshot_review.html', **data), 'html.parser')
    assert not soup.select('[name="field_rationale"], [name="field_evidence_rationale"], [name="quality_score"]')
    diagnostics = soup.select_one('details[data-guided-diagnostics]')
    assert diagnostics and not diagnostics.has_attr('open') and not diagnostics.select('[name]')
    assert '<script>危险</script>' in diagnostics.get_text()
    ack, = soup.select('[name="acknowledge"]')
    assert ack.name == 'button' and ack['value'] == 'approve_and_generate'
    assert ack.get_text(strip=True) == '我已核对关键内容，确认生成'
    assert not soup.select('[name="source_checked"], [name="role_details_checked"]')


def test_guided_optional_location_classification_and_diagnostics_stay_collapsed():
    facts = ScreenshotFacts({'rationale': ExtractedField('诊断存在冲突', 'conflict', ())}, (), (), 2)
    soup = BeautifulSoup(render('screenshot_review.html', **guided_context(facts=facts,
        field_labels={'location_category': '地点分类', 'rationale': '判断说明'})), 'html.parser')
    assert '选填' in soup.select_one('label[for="field-location_category"]').get_text()
    assert not soup.select_one('[data-prefill-extra]').has_attr('open')
    assert soup.select_one('.guided-actions').parent.name == 'form'


def test_real_guided_page_does_not_count_ai_diagnostic_conflicts_as_pending_facts(tmp_path):
    app = screenshot_app(tmp_path)
    with app.state.session_factory() as session:
        record = make_import(session)
        facts = ScreenshotFacts.from_json(record.fields_json)
        fields = {**facts.fields, **{key: ExtractedField('只读诊断', 'conflict', facts.fields['employer_name'].evidence_refs)
            for key in ('confidence', 'evidence', 'rationale', 'uncertainties')}}
        record.fields_json = json.dumps(facts_to_dict(replace(facts, fields=fields)), ensure_ascii=False)
        session.commit()
        public_id = record.public_id
    response = TestClient(app).get(f'/imports/screenshots/{public_id}')
    summary = BeautifulSoup(response.text, 'html.parser').select_one('[data-prefill-summary]').get_text()
    assert '公告字段 0 项' in summary


def test_c_grade_bound_job_keeps_audience_proof_controls_even_with_clear_ai_values():
    job = SimpleNamespace(status='待核验', version=2, row_version=4, quality_score=0,
        verification_checks='{}', verification_version=0, intake_grade='C', intake_reason='[TARGET_AUDIENCE_UNCLEAR]')
    data = guided_context(job=job, values={'student_fit_level': '核心适配', 'target_audience': '本科应届生'},
        review_issues=[dict(path='fields.target_audience',code='audience_confirmation_required',
            severity='error',message='受众需确认',action='请填写依据')])
    soup = BeautifulSoup(render('screenshot_review.html', **data), 'html.parser')
    proof = soup.select_one('#audience-confirmation')
    assert proof and proof.select_one('[name="audience_excerpt"]') and proof.select_one('[name="audience_note"]')
    assert soup.select_one('[data-issue-path="fields.target_audience"]')['href'] == '#audience-confirmation'


@pytest.mark.parametrize('code', ['audience_confirmation_required', 'audience_confirmation_invalid'])
def test_unbound_audience_issue_displays_proof_even_with_clear_ai_values(code):
    data = guided_context(values={'student_fit_level':'核心适配','target_audience':'本科应届生'},
        review_issues=[dict(path='fields.target_audience',code=code,
            severity='error',message='受众证明待补充',action='请填写官方依据')])
    soup = BeautifulSoup(render('screenshot_review.html', **data), 'html.parser')
    assert soup.select_one('#audience-confirmation [name="audience_excerpt"]')
    assert not soup.select_one('#audience-confirmation').has_attr('hidden')
    assert soup.select_one('[data-issue-path]')['href'] == '#audience-confirmation'


def test_scope_confirmation_issue_targets_the_proof_disclosure():
    data = guided_context(values={'posting_scope':'single_role'}, review_issues=[dict(
        path='fields.posting_scope',code='scope_confirmation_required',severity='error',
        message='岗位归属需确认',action='请填写范围证明')])
    soup = BeautifulSoup(render('screenshot_review.html', **data), 'html.parser')
    scope = soup.select_one('details#scope-confirmation')
    assert scope and scope.select_one('[name="scope_excerpt"]')
    assert soup.select_one('[data-issue-path]')['href'] == '#scope-confirmation'


def test_guided_proof_controls_can_be_revealed_after_a_new_json_issue():
    data = guided_context(values={'posting_scope':'single_role','student_fit_level':'核心适配','target_audience':'本科应届生'})
    soup = BeautifulSoup(render('screenshot_review.html', **data), 'html.parser')
    for identity in ('audience-confirmation','scope-confirmation'):
        proof = soup.select_one(f'#{identity}')
        assert proof and proof.has_attr('hidden')


def test_saved_selected_role_summary_uses_only_selected_role_facts(tmp_path):
    app = screenshot_app(tmp_path)
    with app.state.session_factory() as session:
        record = make_import(session)
        facts = ScreenshotFacts.from_json(record.fields_json)
        refs = facts.fields['employer_name'].evidence_refs
        roles = tuple({key: ExtractedField(value,'explicit',refs) for key,value in role.items()} for role in (
            {'job_title':'财务助理','target_audience':'硕士应届生','location_detail':'上海'},
            {'job_title':'设计实习生','target_audience':'本科在读','location_detail':'北京'}))
        record.fields_json = json.dumps(facts_to_dict(replace(facts,roles=roles)),ensure_ascii=False)
        record.selected_roles_json = '[1]'
        session.commit()
        public_id = record.public_id
    response = TestClient(app).get(f'/imports/screenshots/{public_id}')
    soup = BeautifulSoup(response.text,'html.parser')
    summary = soup.select_one('[data-guided-summary]')
    assert summary.select_one('[data-summary-field="job_title"]').get_text(strip=True) == '设计实习生'
    assert summary.select_one('[data-summary-field="target_audience"]').get_text(strip=True) == '本科在读'
    assert summary.select_one('[data-summary-field="location_detail"]').get_text(strip=True) == '北京'
    assert '财务助理' not in summary.get_text()
    assert soup.select_one('[name="role_0_job_title"]')['value'] == '财务助理'
    preview = soup.select_one('#preview-group').get_text()
    assert '地点：上海' not in preview and '适合人群：应届生' not in preview
    assert '地点：北京' in preview and '本科在读' in preview


def test_unknown_deadline_summary_is_explicit_and_never_hides_yearless_source():
    soup = BeautifulSoup(render('screenshot_review.html', **guided_context()), 'html.parser')
    assert '本次确认将按此保留' in soup.select_one('[data-guided-summary]').get_text()
    assert not soup.select_one('[name="deadline_unspecified_confirmed"]')
    soup = BeautifulSoup(render('screenshot_review.html', **guided_context(deadline_unknown_allowed=False)), 'html.parser')
    assert '年份或日期待核对' in soup.select_one('[data-guided-summary]').get_text()


def test_guided_issue_text_is_escaped_and_paths_are_navigation_targets():
    issue = dict(code='missing_fact', path='fields.employer_name', severity='error',
        message='<script>缺单位</script>', action='补齐单位')
    html = render('screenshot_review.html', **guided_context(review_issues=[issue]))
    soup = BeautifulSoup(html, 'html.parser')
    assert '<script>缺单位</script>' not in html
    assert soup.select_one('[data-issue-path="fields.employer_name"]')
    assert soup.select_one('[data-issue-count]').get_text(strip=True) == '1'
    assert soup.select_one('[data-sticky-issue-count]').get_text(strip=True) == '1'
    assert soup.select_one('[data-issue-path]')['href'] == '#field-employer_name'


def test_generic_issue_names_the_chinese_field_and_top_banner_links_to_issues():
    data = guided_context(field_labels={'posting_scope':'公告范围'}, review_issues=[dict(
        path='fields.posting_scope',code='unresolved_field',severity='error',
        message='存在字段冲突或不确定值',action='对照原文')])
    soup = BeautifulSoup(render('screenshot_review.html', **data), 'html.parser')
    assert '公告范围' in soup.select_one('[data-issue-path]').get_text()
    assert soup.select_one('[data-guided-top-issues] [data-issue-count]').get_text(strip=True) == '1'
    assert soup.select_one('[data-guided-top-issues] a')['href'] == '#guided-issues'


def test_readiness_get_reports_all_issues_and_does_not_change_record(tmp_path):
    app = screenshot_app(tmp_path)
    with app.state.session_factory() as session:
        record = make_import(session, fields={'employer_name': '', 'location_detail': '', 'official_url': ''})
        public_id, before = record.public_id, (record.row_version, record.content_revision, record.updated_at)
    client = TestClient(app)
    response = client.get(f'/imports/screenshots/{public_id}/readiness')
    assert response.status_code == 200
    body = response.json()
    assert {i['path'] for i in body['issues']} >= {'fields.employer_name', 'fields.location_detail', 'fields.official_url'}
    assert body['versions']['expected_row_version'] == before[0]
    with app.state.session_factory() as session:
        record = session.query(ScreenshotImport).one()
        assert (record.row_version, record.content_revision, record.updated_at) == before and record.job_id is None
    page = client.get(f'/imports/screenshots/{public_id}')
    assert 'data-guided-summary' in page.text


def test_guided_confirmation_returns_all_issues_latest_versions_and_saved_inputs(tmp_path):
    app = screenshot_app(tmp_path)
    with app.state.session_factory() as session:
        record = make_import(session, fields={'employer_name': '', 'location_detail': '', 'official_url': ''})
        public_id, version = record.public_id, record.row_version
    response = TestClient(app).post(f'/imports/screenshots/{public_id}/confirm', data={
        'review_mode': 'guided', 'acknowledge': 'approve_and_generate', 'expected_row_version': version,
        'title': '保留人工输入', 'field_employer_name': '', 'field_location_detail': '', 'field_official_url': ''})
    assert response.status_code == 409
    body = response.json()
    assert {i['path'] for i in body['issues']} >= {'fields.employer_name', 'fields.location_detail', 'fields.official_url'}
    assert body['draft_saved'] is True
    with app.state.session_factory() as session:
        record = session.query(ScreenshotImport).one()
        assert record.title == '保留人工输入'
        assert body['versions']['expected_row_version'] == record.row_version
        assert record.job_id is None


def test_stale_guided_form_does_not_claim_other_users_saved_revision(tmp_path):
    app = screenshot_app(tmp_path)
    with app.state.session_factory() as session:
        record = make_import(session)
        old_version, public_id = record.row_version, record.public_id
        from app.services.screenshot_workflow import save_import_edits
        save_import_edits(session, record.id, {'title': '他人已保存的内容'}, expected_row_version=old_version)
    response = TestClient(app).post(f'/imports/screenshots/{public_id}/confirm', data={
        'review_mode': 'guided', 'acknowledge': 'approve_and_generate',
        'expected_row_version': old_version, 'title': '旧页面输入'})
    assert response.status_code == 409
    assert response.json()['draft_saved'] is False
    with app.state.session_factory() as session:
        assert session.query(ScreenshotImport).one().title == '他人已保存的内容'


def test_guided_error_returns_own_committed_versions_after_other_writer(tmp_path, monkeypatch):
    from app.services import screenshot_workflow as workflow
    from app.services.screenshot_review import ReviewIssue
    app = screenshot_app(tmp_path)
    with app.state.session_factory() as session:
        record = make_import(session)
        public_id, initial = record.public_id, record.row_version
    observed = {}

    def interleaved_confirmation(session, import_id, data, **kwargs):
        own = workflow.save_import_edits(session, import_id, data['edits'],
            expected_row_version=kwargs['expected_row_version'])
        observed['own'] = dict(expected_row_version=own.row_version,content_revision=own.content_revision,
            expected_job_row_version=None,expected_job_version=None)
        with app.state.session_factory() as competing:
            other = workflow.save_import_edits(competing,import_id,{'title':'OTHER WRITER'},
                expected_row_version=own.row_version)
            observed['other'] = other.row_version
        session.expire_all()
        exc = workflow.ScreenshotReviewValidationError((ReviewIssue('fields','edit_conflict','并发更新','error','请刷新比较'),))
        exc.draft_saved = True
        exc.own_saved_versions = dict(observed['own'])
        raise exc

    monkeypatch.setattr(workflow,'confirm_guided_screenshot_import',interleaved_confirmation)
    client = TestClient(app)
    response = client.post(f'/imports/screenshots/{public_id}/confirm',data={
        'review_mode':'guided','acknowledge':'approve_and_generate','expected_row_version':initial,'title':'MY EDIT'})
    assert response.status_code == 409 and response.json()['draft_saved'] is True
    assert response.json()['versions'] == observed['own']
    assert observed['other'] > observed['own']['expected_row_version']
    stale = client.post(f'/imports/screenshots/{public_id}/save',data={
        'review_mode':'guided','expected_row_version':response.json()['versions']['expected_row_version'],'title':'MY EDIT'})
    assert stale.status_code == 409 and stale.json()['draft_saved'] is False
    with app.state.session_factory() as session:
        assert session.query(ScreenshotImport).one().title == 'OTHER WRITER'


def test_guided_save_captures_own_tokens_before_commit_and_readiness_race(tmp_path, monkeypatch):
    from app.services import screenshot_workflow as workflow
    app = screenshot_app(tmp_path)
    with app.state.session_factory() as session:
        record = make_import(session)
        public_id, initial, import_id = record.public_id, record.row_version, record.id
    original_save = workflow.save_import_edits
    observed = {}

    def save_with_interleaved_commit(session, *args, **kwargs):
        observed['commit_argument'] = kwargs.get('commit',True)
        original_commit = session.commit
        def commit_then_other_writer():
            original_commit()
            if 'own' in observed:
                return
            own = session.get(ScreenshotImport,import_id)
            observed['own'] = dict(expected_row_version=own.row_version,content_revision=own.content_revision,
                expected_job_row_version=None,expected_job_version=None)
            with app.state.session_factory() as competing:
                original_save(competing,import_id,{'title':'OTHER WRITER'},expected_row_version=own.row_version)
            session.expire_all()
        session.commit = commit_then_other_writer
        return original_save(session,*args,**kwargs)

    monkeypatch.setattr(workflow,'save_import_edits',save_with_interleaved_commit)
    response = TestClient(app).post(f'/imports/screenshots/{public_id}/save',data={
        'review_mode':'guided','expected_row_version':initial,'title':'MY EDIT'})
    assert response.status_code == 200
    assert response.json()['versions'] == observed['own']
    assert observed['commit_argument'] is False


def confirmed_fixture(app):
    from app.services import screenshot_workflow as workflow
    with app.state.session_factory() as session:
        record = make_import(session)
        workflow.confirm_guided_screenshot_import(session,record.id,{'acknowledge':True},
            action='approve_and_generate',expected_row_version=record.row_version)
        record.status = 'needs_attention'  # A historical AI diagnostic is not the formal copy gate.
        session.commit()
        return record.public_id


def test_valid_formal_copy_gate_projects_completed_step_and_review_status(tmp_path):
    app = screenshot_app(tmp_path)
    public_id = confirmed_fixture(app)
    soup = BeautifulSoup(TestClient(app).get(f'/imports/screenshots/{public_id}').text,'html.parser')
    assert soup.select_one('[data-progress-label]').get_text(strip=True) == '已完成终审，两种格式可复制'
    assert soup.select_one('.screenshot-steps [aria-current="step"]').get_text(strip=True).startswith('3')
    assert '本版已核验' in soup.select_one('[data-guided-save-state]').get_text()


def test_formal_status_is_not_complete_when_copy_gate_has_blockers(tmp_path, monkeypatch):
    from app.services import distribution
    app = screenshot_app(tmp_path)
    public_id = confirmed_fixture(app)
    monkeypatch.setattr(distribution,'distribution_blockers',lambda job,item=None:['版本已失效'])
    soup = BeautifulSoup(TestClient(app).get(f'/imports/screenshots/{public_id}').text,'html.parser')
    assert soup.select_one('[data-progress-label]').get_text(strip=True) == '有内容需要补充或重试'
    assert soup.select_one('.screenshot-steps [aria-current="step"]').get_text(strip=True).startswith('2')
    assert '识别完成后仍须人工核验' in soup.select_one('[data-guided-save-state]').get_text()


def test_legacy_template_defaults_to_review_step_without_formal_ready_projection():
    soup = BeautifulSoup(render('screenshot_review.html',**context()),'html.parser')
    assert soup.select_one('.screenshot-steps [aria-current="step"]').get_text(strip=True).startswith('2')
    assert soup.select_one('[data-progress-label]').get_text(strip=True) == '识别完成，请核对'


def test_guided_client_retains_inputs_and_updates_versions_without_reload_on_409():
    script = (ROOT / 'app/static/screenshot_import.js').read_text(encoding='utf-8')
    assert 'renderIssues' in script and 'updateVersions' in script
    assert 'aria-invalid' in script and 'aria-describedby' in script
    assert 'data-issue-path' in script and 'parentElement' in script
    assert 'innerHTML' not in script


def test_client_409_keeps_edits_updates_own_versions_and_opens_issue_field():
    """Run the shipped script against a deterministic DOM; never launch a browser."""
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is unavailable')
    script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
class Element {
  constructor(tag='DIV',id=''){this.tagName=tag;this.id=id;this.attrs={};this.dataset={};this.children=[];this.handlers={};this.value='';this.textContent='';this.hidden=false;}
  setAttribute(k,v){this.attrs[k]=String(v);}
  getAttribute(k){return this.attrs[k]??null;}
  removeAttribute(k){delete this.attrs[k];}
  append(...nodes){nodes.forEach(n=>{n.parentElement=this;this.children.push(n)});}
  replaceChildren(...nodes){this.children=[];this.append(...nodes);}
  addEventListener(k,f){this.handlers[k]=f;}
  focus(){document.activeElement=this;}
  scrollIntoView(){}
  matches(){return this.tagName==='INPUT';}
}
const nodes={};
['expected_row_version','expected_job_row_version','expected_job_version','source_name','source_url','edited_text'].forEach(name=>{nodes[name]=new Element('INPUT');});
nodes.expected_row_version.value='3';nodes.source_url.value='https://example.com/unsaved';nodes.edited_text.value='人工输入应保留';
const form=new Element('FORM');form.dataset={reviewUrl:'/review',verification:'{}',selectedRoles:'[]',reviewIssuesJson:'[]'};
form.hasAttribute=name=>name==='data-guided-review';form.elements={namedItem:name=>nodes[name]||null};
const list=new Element('UL'), empty=new Element(), count=new Element(), sticky=new Element(), error=new Element(), feedback=new Element();
const saveState=new Element(),actionState=new Element();
const advanced=new Element('DETAILS','guided-advanced'), target=new Element('INPUT','field-employer_name'), title=new Element('H2','guided-issues-title');
const scope=new Element('DETAILS','scope-confirmation'), audience=new Element('DIV','audience-confirmation');scope.hidden=true;audience.hidden=true;
const selectedRole=new Element('INPUT');selectedRole.value='1';selectedRole.checked=true;
const roleSummary=new Element(), audienceSummary=new Element(), locationSummary=new Element();
['job_title','target_audience','location_detail'].forEach((key,index)=>{nodes[`role_1_${key}`]=new Element('INPUT');nodes[`role_1_${key}`].value=['设计实习生','本科在读','北京'][index];});
advanced.append(target);form.append(advanced,scope,audience);
const button=new Element('BUTTON');button.name='acknowledge';button.value='approve_and_generate';button.setAttribute('formaction','/imports/screenshots/example/confirm');
form.querySelector=name=>({'[data-review-issues]':list,'[data-issues-empty]':empty,'[data-form-error]':error,'[data-guided-save-state]':saveState,'[data-guided-action-state]':actionState,'[data-summary-field="job_title"]':roleSummary,'[data-summary-field="target_audience"]':audienceSummary,'[data-summary-field="location_detail"]':locationSummary}[name]||null);
form.querySelectorAll=name=>name==='button[type="submit"]'?[button]:name==='[name="selected_roles"]'?[selectedRole]:name==='[data-issue-count], [data-sticky-issue-count]'?[count,sticky]:name==='[data-guided-invalid]'&&target.getAttribute('data-guided-invalid')?[target]:[];
const document={cookie:'',activeElement:null,createElement:tag=>new Element(tag.toUpperCase()),
  querySelector:name=>({'[data-screenshot-review]':form,'[data-form-error]':error,'[data-form-feedback]':feedback}[name]||null),
  querySelectorAll:name=>name==='[data-issue-count], [data-sticky-issue-count]'?[count,sticky]:[],getElementById:id=>({'guided-advanced':advanced,'field-employer_name':target,'guided-issues-title':title,'scope-confirmation':scope,'audience-confirmation':audience}[id]||null)};
let calls=0, navigations=0;
const context={document,window:{location:{assign:()=>navigations++,reload:()=>navigations++},addEventListener:()=>{}},
  sessionStorage:{getItem:()=>null},FormData:class{set(){}},console,Date,Set,JSON,Number,
  fetch:async()=>{calls++;return {ok:false,headers:{get:()=> 'application/json'},json:async()=>({
    error:calls===1?'草稿已保存，尚不能成稿':'内容已被他人修改，请刷新对照后重试',draft_saved:calls===1,
    versions:{expected_row_version:calls===1?8:9,expected_job_row_version:4,expected_job_version:2,content_revision:calls===1?2:3},
    issues:[{path:'fields.employer_name',code:'required',severity:'error',message:'单位缺失<script>',action:'补齐单位'},
      {path:'fields.posting_scope',code:'scope_confirmation_required',severity:'error',message:'范围须确认',action:'补齐范围证明'},
      {path:'fields.target_audience',code:'audience_confirmation_required',severity:'error',message:'受众须确认',action:'补齐受众证明'}]})}}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
(async () => {
  form.handlers.input();
  assert.equal(roleSummary.textContent,'设计实习生');assert.equal(audienceSummary.textContent,'本科在读');assert.equal(locationSummary.textContent,'北京');
  await form.handlers.submit({preventDefault(){},submitter:button});
  assert.equal(calls,1);assert.equal(navigations,0);
  assert.equal(nodes.expected_row_version.value,8);assert.equal(nodes.edited_text.value,'人工输入应保留');
  assert.equal(nodes.source_url.value,'https://example.com/unsaved');
  assert.match(saveState.textContent,/本次提交已保存为版本 2/);assert.match(saveState.textContent,/红标仍需处理/);
  assert.equal(actionState.textContent,'本次草稿已保存，处理红标后再次确认');
  assert.equal(count.textContent,'3');assert.equal(sticky.textContent,'3');
  assert.equal(scope.hidden,false);assert.equal(scope.open,true);assert.equal(audience.hidden,false);
  assert.equal(target.getAttribute('aria-invalid'),'true');assert.equal(target.getAttribute('aria-describedby'),'review-issue-0');
  const anchor=list.children[0].children[0];assert.match(anchor.textContent,/<script>/);
  anchor.handlers.click({preventDefault(){}});assert.equal(advanced.open,true);assert.equal(document.activeElement,target);
  const scopeAnchor=list.children[1].children[0];assert.equal(scopeAnchor.href,'#scope-confirmation');
  scopeAnchor.handlers.click({preventDefault(){}});assert.equal(scope.open,true);assert.equal(document.activeElement,scope);
  await form.handlers.submit({preventDefault(){},submitter:button});
  assert.equal(calls,2);assert.equal(navigations,0);
  assert.equal(nodes.expected_row_version.value,8,'unsaved concurrency failure must not rebase retained edits');
  assert.equal(nodes.edited_text.value,'人工输入应保留');assert.match(error.textContent,/他人修改/);
  assert.match(saveState.textContent,/本次提交未保存/);assert.match(actionState.textContent,/比较/);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run([node, '-e', script, str(ROOT / 'app/static/screenshot_import.js')],
        capture_output=True, text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr
