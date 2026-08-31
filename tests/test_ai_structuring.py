from app.services.ai_structuring import parse_ai_draft


def test_parse_ai_draft_keeps_only_supported_fields_and_missing_values_empty():
    draft = parse_ai_draft('{"notice_type":"新招聘","employer_name":"测试单位","deadline":null,"unknown":"x"}')
    assert draft.notice_type == "新招聘"
    assert draft.employer_name == "测试单位"
    assert draft.deadline == ""


def test_parse_ai_draft_keeps_scope_attachment_and_application_method():
    draft = parse_ai_draft(
        '{"posting_scope":"multi_role_announcement","attachment_status":"checked",'
        '"application_method":"email","application_contact":"hr@example.com"}'
    )

    assert draft.posting_scope == "multi_role_announcement"
    assert draft.attachment_status == "checked"
    assert draft.application_method == "email"
    assert draft.application_contact == "hr@example.com"


def test_build_prompt_requires_json_and_forbids_fabrication():
    from app.services.ai_structuring import build_structuring_prompt

    prompt = build_structuring_prompt("测试招聘公告", "https://example.com", "公开原文")

    assert "JSON" in prompt
    assert "不得猜测" in prompt
    assert "测试招聘公告" in prompt
