import json

import httpx
import pytest


def adapter():
    from app.sources import campus_jsonp
    return campus_jsonp


def wrapped(value):
    return 'researchCallback(' + json.dumps(value, ensure_ascii=False) + ');'


def row(**changes):
    data = dict(wid='a' * 32, zwmc='运营实习生', yrdwmc='示例企业', sxsj='2026-09-30 16:00:00',
                gzd='上海市 黄浦区', jlzyyx='apply@example.org', zwms='<p>岗位职责：协助整理运营数据并制作日报。</p><p>任职要求：本科在校生。</p>', subOpList=[])
    data.update(changes)
    return data


def listing(module, data=None):
    return module.parse_jsonp_listings(wrapped({'listData': [data or row()]}), module.SUES_INTERNSHIP_SOURCE)[0]


def test_jsonp_only_accepts_exact_fixed_callback_json():
    module = adapter()
    assert module.decode_jsonp(wrapped({'listData': []})) == {'listData': []}
    for value in ['evil({})', 'researchCallback({});alert(1)', 'researchCallback((function(){return {}})())', 'researchCallback([])', 'researchCallback({"x":NaN})']:
        with pytest.raises(ValueError):
            module.decode_jsonp(value)


def test_single_job_preserves_facts_and_does_not_guess_unknown_date():
    module = adapter()
    data = row(subOpList=[{'content': '运营实习生', 'field2': '2020-01-01', 'field4': '3000--3499'}], xlyq='本科')
    detail = module.parse_jsonp_details(wrapped({'listData': [data]}), module.SUES_INTERNSHIP_SOURCE, listing(module))[0]
    assert detail.identity_key.startswith('a' * 32)
    assert detail.location_category == '明确上海'
    assert detail.official_url == ''
    assert '官方投递邮箱：apply@example.org' in detail.evidence_text
    assert '截止' not in detail.evidence_text
    assert '原始正文' in detail.evidence_text
    assert '学历3000' not in detail.evidence_text


def test_aggregate_role_cannot_inherit_all_parent_body_as_one_job():
    module = adapter()
    data = row(subOpList=[{'content': '运营实习等', 'field4': '5500--5999'}])
    detail = module.parse_jsonp_details(wrapped({'listData': [data]}), module.SUES_INTERNSHIP_SOURCE, listing(module))[0]
    from app.services.source_candidate_policy import policy_evidence_text
    assert '本科在校生' not in policy_evidence_text(detail.evidence_text)
    assert '岗位描述：\n' in detail.evidence_text


def test_multiple_roles_do_not_inherit_other_role_qualifications_or_email():
    module = adapter()
    data = row(jlzyyx='', zwms='<p>运营实习生</p><p>岗位职责：整理运营日报数据。任职要求：本科在校生。投递邮箱：role@example.org</p><p>研究员</p><p>岗位职责：进行科研建模研究。任职要求：博士。</p>', subOpList=[{'content': '运营实习生', 'field4': '本科'}, {'content': '研究员', 'field4': '博士'}])
    details = module.parse_jsonp_details(wrapped({'listData': [data]}), module.SUES_INTERNSHIP_SOURCE, listing(module))
    from app.services.source_candidate_policy import policy_evidence_text
    assert len(details) == 2
    scoped = policy_evidence_text(details[1].evidence_text)
    assert '本科' not in scoped and 'role@example.org' not in scoped
    assert '博士' in scoped


def test_placeholder_is_marked_and_source_page_is_not_apply_url():
    module = adapter()
    detail = module.parse_jsonp_details(wrapped({'listData': [row(zwms='岗位职责：可随意填', jlzyyx='', gzd='北京市', gszy='https://example.org')]}), module.SUES_INTERNSHIP_SOURCE, listing(module))[0]
    assert detail.location_category == '其他地区'
    assert '可随意填' in detail.evidence_text
    assert detail.official_url == ''


def test_explicit_body_application_url_is_retained_but_company_home_is_not():
    module = adapter()
    data = row(jlzyyx='', zwms='<p>岗位职责：协助分析运营数据。</p><p>网申地址：https://careers.example.org/apply</p><p>公司官网：https://example.org</p>')
    detail = module.parse_jsonp_details(wrapped({'listData': [data]}), module.SUES_INTERNSHIP_SOURCE, listing(module))[0]
    assert detail.official_url == 'https://careers.example.org/apply'
    assert '官方报名入口：https://careers.example.org/apply' in detail.evidence_text


def test_redirect_is_rejected_even_with_redirect_following_client():
    module = adapter()
    requested = []
    def respond(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={'Location': 'https://outside.example.org/login'}) if len(requested) == 1 else httpx.Response(200, text=wrapped({'listData': [row()]}))
    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        with pytest.raises(httpx.HTTPStatusError):
            module.fetch_jsonp_listings(client, module.SUES_INTERNSHIP_SOURCE)
    assert len(requested) == 1


@pytest.mark.parametrize('academic,blocked', [('硕士及以上', True), ('硕士,本科', False)])
def test_common_structured_qualification_applies_to_split_roles(academic, blocked):
    module = adapter()
    from app.services.source_candidate_policy import candidate_from_detail, evaluate_candidate, policy_evidence_text
    data = row(xlyq=academic, zwms='<p>运营实习生</p><p>岗位职责：整理运营日报及相关数据。</p><p>市场实习生</p><p>岗位职责：分析市场数据并整理相关报告。</p>', subOpList=[{'content': '运营实习生'}, {'content': '市场实习生'}])
    details = module.parse_jsonp_details(wrapped({'listData': [data]}), module.SUES_INTERNSHIP_SOURCE, listing(module))
    for detail in details:
        assert '学历要求：' + academic in policy_evidence_text(detail.evidence_text)
        decision = evaluate_candidate(candidate_from_detail(detail))
        assert ('GRADUATE_DEGREE_REQUIRED' in decision.reason_codes) == blocked
        if not blocked:
            assert decision.verdict == 'qualified'


def test_common_academic_field_cannot_replace_missing_exact_role_body():
    module = adapter()
    from app.services.source_candidate_policy import candidate_from_detail, evaluate_candidate, policy_evidence_text
    from app.services.review_intake import evaluate_review_intake
    data = row(xlyq='本科', zwms='<p>若干岗位，资格另行确认。</p>', subOpList=[{'content': '运营专员'}, {'content': '分析专员'}])
    details = module.parse_jsonp_details(wrapped({'listData': [data]}), module.SUES_INTERNSHIP_SOURCE, listing(module))
    for detail in details:
        assert '岗位描述：\n' in policy_evidence_text(detail.evidence_text)
        assert 'BODY_NOT_JOB_SPECIFIC' in evaluate_candidate(candidate_from_detail(detail)).reason_codes
        assert 'BODY_NOT_JOB_SPECIFIC' in evaluate_review_intake(candidate_from_detail(detail)).reason_codes


def test_request_shape_timeout_bounds_and_dedup():
    module = adapter()
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, text=wrapped({'listData': [row()]}))
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        entries = module.fetch_jsonp_listings(client, module.SIT_INTERNSHIP_SOURCE, pages=3)
        module.fetch_jsonp_details(client, module.SIT_INTERNSHIP_SOURCE, entries[0])
    assert len(entries) == 1
    assert requests[0].url.params['fbbk'] == '1'
    assert requests[0].url.params['eachPageRows'] == '10'
    assert requests[-1].url.params['type'] == 'input'
    assert requests[-1].url.params['wid'] == 'a' * 32
    assert requests[-1].url.params['callback'] == 'researchCallback'
    assert entries[0].detail_url.endswith('&type=1')
    with pytest.raises(ValueError):
        module.fetch_jsonp_listings(None, module.SUES_INTERNSHIP_SOURCE, pages=4)


@pytest.mark.parametrize('payload', [{'listData': {}}, {'listData': [None]}, {}, {'listData': []}])
def test_bad_detail_shapes_fail_closed(payload):
    module = adapter()
    with pytest.raises(ValueError):
        module.parse_jsonp_details(wrapped(payload), module.SUES_INTERNSHIP_SOURCE, listing(module))
