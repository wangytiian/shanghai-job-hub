from app.services.ai_structuring import parse_ai_draft


def test_ai_draft_blocks_student_distribution_for_senior_doctoral_roles():
    draft = parse_ai_draft(
        '{"job_title":"教学科研人员","target_audience":"博士毕业生/博士后",'
        '"student_fit_level":"核心适配",'
        '"distribution_recommendation":"进入学生分发审核",'
        '"rationale":"", "confidence":"高"}',
        evidence_text="要求副教授及以上职称，或博士毕业生、出站博士后。",
    )

    assert draft.student_fit_level == "不适合核心学生用户"
    assert draft.distribution_recommendation == "不进入学生分发"
    assert "副教授" in draft.rationale


def test_ai_draft_keeps_ai_suggested_shanghai_location_when_evidence_supports_it():
    draft = parse_ai_draft(
        '{"location_category":"明确上海",'
        '"location_detail":"上海（具体地点以原文为准）",'
        '"student_fit_level":"核心适配",'
        '"distribution_recommendation":"进入学生分发审核",'
        '"rationale":"原文明确工作地点为上海。", "confidence":"高"}',
        evidence_text="工作地点：上海市。",
    )

    assert draft.location_category == "明确上海"
    assert draft.location_detail == "上海市"


def test_ai_draft_derives_job_family_and_shanghai_location_from_announcement_context():
    draft = parse_ai_draft(
        '{"job_title":"教学科研人员（专技岗位）", "job_family":"",'
        '"location_category":"", "location_detail":"",'
        '"target_audience":"博士毕业生/博士后",'
        '"student_fit_level":"", "distribution_recommendation":"",'
        '"rationale":"", "confidence":""}',
        evidence_text="上海市团校公开招聘教学科研人员，要求副教授及以上职称或博士毕业生。",
    )

    assert draft.job_family == "教育科研、公共管理"
    assert draft.location_category == "原文未明确"
    assert draft.location_detail == ""


def test_ai_draft_normalizes_json_lists_and_only_marks_shanghai_when_workplace_is_explicit():
    draft = parse_ai_draft(
        '{"job_title":"财务实习生","job_family":["财会审计","金融银行"],'
        '"direction_tags":["会计审计","金融银行"],'
        '"location_category":"", "location_detail":"",'
        '"target_audience":["大三实习","大四/应届生"],'
        '"student_fit_level":"核心适配",'
        '"distribution_recommendation":"进入学生分发审核",'
        '"rationale":"", "confidence":"中"}',
        evidence_text="招聘单位位于上海市，工作地点：上海市浦东新区。",
    )

    assert draft.job_family == "财会审计、金融银行"
    assert draft.direction_tags == "会计审计、金融银行"
    assert draft.target_audience == "大三实习、大四/应届生"
    assert draft.location_category == "明确上海"
    assert draft.location_detail == "上海市浦东新区"


def test_ai_draft_keeps_announcement_title_separate_from_a_specific_role():
    draft = parse_ai_draft(
        '{"announcement_title":"2027届校园招聘公告", "job_title":"财务管理培训生",'
        '"posting_scope":"single_role", "confidence":"中"}',
        evidence_text="2027届校园招聘公告。岗位名称：财务管理培训生。",
    )

    assert draft.announcement_title == "2027届校园招聘公告"
    assert draft.job_title == "财务管理培训生"
