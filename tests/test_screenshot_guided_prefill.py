"""Same-source repair may repair units, never invent a second factual version."""
import json
from dataclasses import replace

import pytest

from app.services import screenshot_tasks as tasks
from app.services.screenshot_contracts import EXTRACTION_RULE_VERSION, EvidenceRef
from app.services.screenshot_prefill import build_repair_prompt


def field(value, quote, state="explicit"):
    return {"value": value, "state": state, "quotes": [quote] if quote else []}


def payload(fields=None, roles=None, common=None):
    return json.dumps({"fields": fields or {}, "roles": roles or [],
                       "common_requirements": common or [], "warnings": []}, ensure_ascii=False)


def extract(text, first_fields, second_fields, *, roles=None, repaired_roles=None,
            common=None, repaired_common=None, invalid_second=False):
    initial = {**first_fields, "deadline": field("2099年1月1日", "不存在的依据")}
    repair = dict(second_fields)
    if invalid_second:
        repair["deadline"] = field("2099年1月1日", "不存在的依据")
    elif "deadline" not in first_fields:
        repair["deadline"] = field("", "", "missing")
    replies = iter((payload(initial, roles, common), payload(repair, repaired_roles, repaired_common)))
    prompts = []
    def call(prompt):
        prompts.append(prompt)
        return next(replies)
    facts, diagnostic = tasks._extract_prefill(call, text, text, (), 0, 1)
    assert len(prompts) == 2
    return facts, diagnostic, prompts


@pytest.mark.parametrize("invalid_second", [False, True])
def test_same_source_cosmetic_eligibility_and_diagnostics_do_not_conflict(invalid_second):
    quote = "面向在读博士生、2027届应届硕士毕业生"
    first = {"eligibility": field(quote, quote), "confidence": field("高", quote),
             "rationale": field("面向硕博学生", quote), "uncertainties": field("范围待核对", quote),
             "evidence": field("原图的学生范围", quote)}
    second = {"eligibility": field("在读博士生、2027届应届硕士毕业生", quote),
              "confidence": field("中", quote), "rationale": field("研究生专场", quote),
              "uncertainties": field("本科生不适配", quote), "evidence": field("已核对原图范围", quote)}
    facts, diagnostic, _ = extract(quote, first, second, invalid_second=invalid_second)
    assert facts.fields["eligibility"].value == quote
    assert facts.fields["eligibility"].state == "explicit"
    for name in ("confidence", "rationale", "uncertainties", "evidence"):
        assert facts.fields[name].value == second[name]["value"]
        assert facts.fields[name].state == "explicit"
    assert not any("事实冲突" in warning for warning in facts.warnings)
    assert diagnostic["partial"] is invalid_second


@pytest.mark.parametrize("name,left,right", [
    ("deadline", "2027年10月1日", "2027年10月2日"),
    ("salary", "6000元", "8000元"),
    ("application_contact", "a@example.com", "b@example.com"),
    ("eligibility", "本科", "硕士"),
    ("eligibility", "需要经验", "不需要经验"),
    ("target_audience", "2027届", "2028届"),
    ("employer_name", "甲公司", "乙公司"),
    ("job_title", "财务岗", "审计岗"),
])
def test_grounded_factual_changes_still_conflict(name, left, right):
    quote = left + "；" + right
    first = {name: field(left, quote)}
    # Trigger repair with an independent invalid unit, including for deadline.
    invalid = "salary" if name == "deadline" else "deadline"
    first[invalid] = field("错误", "不存在的依据")
    replies = iter((payload(first), payload({name: field(right, quote), invalid: field("", "", "missing")})))
    facts, _ = tasks._extract_prefill(lambda prompt: next(replies), quote, quote, (), 0, 1)
    assert facts.fields[name].state == "conflict"
    assert left in facts.fields[name].value and right in facts.fields[name].value


def test_omitted_repair_units_keep_initial_valid_and_unclear():
    text = "星辰公司；本科"
    first = payload({"employer_name": field("星辰公司", text), "salary": field("8000元", "不存在")})
    replies = iter((first, "{}"))
    facts, diagnostic = tasks._extract_prefill(lambda prompt: next(replies), text, text, (), 0, 1)
    assert facts.fields["employer_name"].value == "星辰公司"
    assert facts.fields["salary"].state == "unclear"
    assert diagnostic["partial"]


def test_same_source_tag_formatting_keeps_initial_value_and_literal_numbers():
    text = "财务2027、不接受审计"
    facts, _, _ = extract(text, {"direction_tags": field("财务2027、 不接受审计", text)},
                          {"direction_tags": field("财务2027,不接受审计", text)})
    assert facts.fields["direction_tags"].value == "财务2027、 不接受审计"
    assert facts.fields["direction_tags"].state == "explicit"
    facts, _, _ = extract(text + "；财务2028、接受审计",
                          {"direction_tags": field("财务2027、不接受审计", text)},
                          {"direction_tags": field("财务2028、接受审计", "财务2028、接受审计")})
    assert facts.fields["direction_tags"].state == "conflict"


def test_prefix_normalization_needs_identical_evidence_and_same_source():
    text = "面向本科；本科"
    first = tasks._recover_prefill(payload({"eligibility": field("面向本科", "面向本科")}), text, (), 0, 1)
    second = tasks._recover_prefill(payload({"eligibility": field("本科", "本科")}), text, (), 0, 1)
    merged = tasks._merge_prefill_chunks((first, second), complete_text=text, content_revision=1)
    assert merged.fields["eligibility"].state == "conflict"
    facts, _, _ = extract(text, {"eligibility": field("面向本科", "面向本科")},
                          {"eligibility": field("本科", "本科")})
    assert facts.fields["eligibility"].state == "conflict"


def test_cross_chunk_diagnostics_are_latest_valid_but_quotes_are_still_validated():
    text = "本科"
    first = tasks._recover_prefill(payload({"confidence": field("高", text),
                                            "rationale": field("财经学生相关", text)}), text, (), 0, 1)
    second = tasks._recover_prefill(payload({"confidence": field("中", text),
                                             "rationale": field("范围仍需判断", text)}), text, (), 0, 1)
    facts = tasks._merge_prefill_chunks((first, second), complete_text=text, content_revision=1)
    assert facts.fields["confidence"].value == "中"
    assert facts.fields["rationale"].value == "范围仍需判断"
    assert not any("事实冲突" in warning for warning in facts.warnings)
    invalid = replace(second.fields["rationale"], evidence_refs=(EvidenceRef(None, None, 0, 2, "伪造", "manual"),))
    malformed = replace(second, fields={**second.fields, "rationale": invalid})
    with pytest.raises(ValueError, match="汇总引用"):
        tasks._merge_prefill_chunks((first, malformed), complete_text=text, content_revision=1)


def test_repair_resolves_only_invalid_units_and_adopts_grounded_missing_data():
    text = "星辰公司；本科；薪资6000元"
    first = {"employer_name": field("星辰公司", "星辰公司"), "salary": field("错误", "不存在")}
    facts, diagnostic, _ = extract(text, first, {"salary": field("6000元", "薪资6000元"),
                                                 "target_audience": field("本科", "本科")})
    assert facts.fields["employer_name"].value == "星辰公司"
    assert facts.fields["salary"].value == "6000元"
    assert facts.fields["target_audience"].value == "本科"
    assert not diagnostic["partial"]


def test_same_role_repair_preserves_cosmetic_field_and_conflicts_changed_title():
    quote = "财务岗 面向本科"
    initial_roles = [{"job_title": field("财务岗", "财务岗"), "eligibility": field("面向本科", "面向本科")}]
    repair_roles = [{"job_title": field("财务岗", "财务岗"), "eligibility": field("本科", "面向本科")}]
    facts, _, _ = extract(quote, {}, {}, roles=initial_roles, repaired_roles=repair_roles)
    assert facts.roles[0]["eligibility"].value == "面向本科"
    assert facts.roles[0]["eligibility"].state == "explicit"
    text = "财务岗或审计岗"
    facts, diagnostic, _ = extract(text, {}, {},
        roles=[{"job_title": field("财务岗", text)}],
        repaired_roles=[{"job_title": field("审计岗", text)}])
    assert len(facts.roles) == 1
    assert facts.roles[0]["job_title"].state == "conflict"
    assert diagnostic["partial"]


def test_prompt_guides_lixin_undergraduates_without_mutating_quoted_restrictions():
    text = "面向在读博士生、2027届应届硕士毕业生"
    _, _, prompts = extract(text, {"target_audience": field(text, text)}, {})
    assert EXTRACTION_RULE_VERSION == "screenshot-extraction-v3-guided-review"
    for prompt in (*prompts, build_repair_prompt(text, "{}", "错误")):
        assert "上海立信" in prompt and "本科" in prompt and "财经" in prompt
        assert "研究生" in prompt and "核心适配" in prompt
        assert text in prompt
        assert "diagnostic" not in prompt
    assert "仅修复" in prompts[1] and "已通过" in prompts[1]
