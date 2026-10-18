"""Regression: one invalid AI field must not leave the work area empty."""
import json

import httpx
import pytest

from app.models import ScreenshotImport, WorkItem
from app.services.screenshot_contracts import OcrResult
from app.services.screenshot_extraction import facts_from_dict, facts_to_dict
from app.services import screenshot_tasks
from test_screenshot_tasks import FakeAI, add_import, task_db, queue_import, run_screenshot


TEXT = "示例公司招聘财务实习生\n工作地点：上海\n资格：本科在校生"


def response(*, invalid=False):
    fields = {
        "employer_name": {"value":"示例公司","state":"explicit","quotes":["示例公司招聘财务实习生"]},
        "job_title": {"value":"财务实习生","state":"explicit","quotes":["示例公司招聘财务实习生"]},
    }
    if invalid:
        fields["deadline"] = {"value":"2099-01-01","state":"explicit","quotes":["不存在的日期依据"]}
    return json.dumps({"fields":fields,"roles":[],"common_requirements":[],"warnings":[]},ensure_ascii=False)


def setup_extraction(task_db, callback):
    factory, root = task_db
    ai = FakeAI()
    ai.ocr_callback = lambda: OcrResult(TEXT,"test-ocr","stop",1,None)
    ai.text_callback = callback
    record_id = add_import(factory,root)
    queue_import(factory,record_id,ai)
    assert run_screenshot(factory,ai,root)
    return factory,root,ai,record_id


def test_invalid_first_response_is_automatically_repaired_once(task_db):
    repaired=json.loads(response())
    repaired["fields"]["deadline"]={"value":"","state":"missing","quotes":[]}
    replies=iter((response(invalid=True),json.dumps(repaired,ensure_ascii=False)))
    factory,root,ai,record_id=setup_extraction(task_db,lambda:next(replies))
    assert run_screenshot(factory,ai,root)
    with factory() as session:
        record=session.get(ScreenshotImport,record_id)
        facts=json.loads(record.fields_json)
        assert facts["fields"]["employer_name"]["value"]=="示例公司"
        assert record.status=="ready"
    assert len(ai.text_calls)==2
    assert len(ai.ocr_calls)==1


def test_failed_repair_keeps_valid_facts_and_marks_bad_field(task_db):
    factory,root,ai,record_id=setup_extraction(task_db,lambda:response(invalid=True))
    assert run_screenshot(factory,ai,root)
    with factory() as session:
        record=session.get(ScreenshotImport,record_id)
        facts=json.loads(record.fields_json)
        assert facts["fields"]["employer_name"]["value"]=="示例公司"
        assert facts["fields"]["deadline"]["value"]==""
        assert facts["fields"]["deadline"]["state"]=="unclear"
        assert record.status=="needs_attention"
        assert record.error_code=="partial_prefill"
        assert facts["warnings"]
    assert len(ai.text_calls)==2


def test_repair_timeout_still_preserves_valid_initial_facts(task_db):
    calls=iter((response(invalid=True),httpx.ReadTimeout('provider details must not leak')))
    def callback():
        reply=next(calls)
        if isinstance(reply,Exception):
            raise reply
        return reply
    factory,root,ai,record_id=setup_extraction(task_db,callback)
    assert run_screenshot(factory,ai,root)
    with factory() as session:
        record=session.get(ScreenshotImport,record_id)
        assert json.loads(record.fields_json)["fields"]["employer_name"]["value"]=="示例公司"
        assert 'provider details' not in record.error_message
        assert record.status=="needs_attention"


def test_new_schema_freeze_is_used_without_changing_config(task_db):
    factory,root,ai,record_id=setup_extraction(task_db,lambda:response())
    seen=[]
    def freeze(session,schema):
        seen.append(schema)
        return ai.freeze_text_request(session)
    ai.freeze_screenshot_text_request=freeze
    assert run_screenshot(factory,ai,root)
    assert seen and seen[0]["strict"] is True


def test_ungrounded_common_paraphrase_is_not_silently_accepted(task_db):
    factory,root=task_db
    ai=FakeAI()
    body=TEXT+"\n所有岗位缴费200元"
    ai.ocr_callback=lambda:OcrResult(body,"test-ocr","stop",1,None)
    payload=json.loads(response())
    payload["common_requirements"]=[{"value":"无需缴费","state":"explicit","quotes":["所有岗位缴费200元"]}]
    ai.text_callback=lambda:json.dumps(payload,ensure_ascii=False)
    record_id=add_import(factory,root)
    queue_import(factory,record_id,ai)
    assert run_screenshot(factory,ai,root)
    assert run_screenshot(factory,ai,root)
    with factory() as session:
        record=session.get(ScreenshotImport,record_id)
        assert record.status=="needs_attention"
        facts=json.loads(record.fields_json)
        assert facts['fields']['employer_name']['value']=='示例公司'
        assert all(field['value']!='无需缴费' for field in facts['common_requirements'])
        assert any(warning.startswith('AI公共条件待核对') for warning in facts['warnings'])


def crossed_role_response():
    return json.dumps({"fields":{},"roles":[
        {"job_title":{"value":"财务岗","state":"explicit","quotes":["财务岗本科"]},
         "eligibility":{"value":"硕士","state":"explicit","quotes":["研究岗硕士"]}},
        {"job_title":{"value":"研究岗","state":"unclear","quotes":["研究岗硕士"]}},
    ],"common_requirements":[],"warnings":[]},ensure_ascii=False)


@pytest.mark.parametrize("on_repair", [False, True])
def test_every_response_applies_discarded_role_boundary_barriers(on_repair):
    source="公司A招聘财务岗本科\n研究岗硕士"
    initial=crossed_role_response() if not on_repair else '{"fields":{"deadline":{"value":"2099","state":"explicit","quotes":["不存在"]}}}'
    replies=iter((initial,crossed_role_response()))
    calls=[]
    def call(prompt):
        calls.append(prompt)
        return next(replies)
    facts,diagnostic=screenshot_tasks._extract_prefill(call,source,source,(),0,1)
    assert facts.roles[0]["job_title"].value=="财务岗"
    assert facts.roles[0]["eligibility"].value==""
    assert facts.roles[0]["eligibility"].state=="unclear"
    assert any("岗位边界" in warning for warning in facts.warnings)
    assert len(calls)==2 and diagnostic["partial"]


def test_empty_repair_preserves_initial_valid_facts_and_unresolved_markers():
    replies=iter((response(invalid=True),"{}"))
    facts,diagnostic=screenshot_tasks._extract_prefill(lambda prompt:next(replies),TEXT,TEXT,(),0,1)
    assert facts.fields["employer_name"].value=="示例公司"
    assert facts.fields["deadline"].state=="unclear"
    assert facts.fields["deadline"].value==""
    assert diagnostic["partial"] and not diagnostic["repaired"]
    assert facts_from_dict(facts_to_dict(facts)).fields["deadline"].state=="unclear"


def test_partial_repair_omitting_failed_field_does_not_claim_resolution():
    replies=iter((response(invalid=True),response()))
    facts,diagnostic=screenshot_tasks._extract_prefill(lambda prompt:next(replies),TEXT,TEXT,(),0,1)
    assert facts.fields["employer_name"].value=="示例公司"
    assert facts.fields["deadline"].state=="unclear"
    assert diagnostic["partial"] and not diagnostic["repaired"]


def test_final_cross_chunk_merge_preserves_failed_field_unclear(task_db,monkeypatch):
    first="示例公司招聘财务实习生"
    second="工作地点：上海\n资格：本科在校生"
    monkeypatch.setattr(screenshot_tasks,"split_text_chunks",lambda source:(first,second))
    replies=iter((response(invalid=True),response(invalid=True),"{}"))
    factory,root,ai,record_id=setup_extraction(task_db,lambda:next(replies))
    assert run_screenshot(factory,ai,root)
    with factory() as session:
        record=session.get(ScreenshotImport,record_id)
        facts=facts_from_dict(json.loads(record.fields_json))
        assert facts.fields["employer_name"].value=="示例公司"
        assert facts.fields["deadline"].state=="unclear"
        assert record.status=="needs_attention"
    assert len(ai.text_calls)==3


def test_combined_recovery_warnings_are_bounded_and_keep_review_blockers():
    replies=[]
    for prefix in ("first","second"):
        payload=json.loads(response(invalid=True))
        payload["warnings"]=[f"{prefix}普通提醒{index}" for index in range(98)]
        payload["warnings"] += [f"AI公共条件待核对：{prefix}公共限制",f"岗位边界不明确：{prefix}岗位"]
        replies.append(json.dumps(payload,ensure_ascii=False))
    pending=iter(replies)
    facts,diagnostic=screenshot_tasks._extract_prefill(lambda prompt:next(pending),TEXT,TEXT,(),0,1)
    restored=facts_from_dict(facts_to_dict(facts))
    assert diagnostic["partial"]
    assert len(restored.warnings)<=100
    assert any(warning.startswith("AI公共条件待核对") for warning in restored.warnings)
    assert any("岗位边界" in warning for warning in restored.warnings)
    assert any("较多" in warning for warning in restored.warnings)
