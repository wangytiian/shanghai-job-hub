import json
from pathlib import Path

import httpx
import pytest

from app.sources.campus_json import (
    CampusAnnouncement,
    CampusJsonSource,
    SJTU_INTERNSHIP_SOURCE,
    SUFE_JOB_SOURCE,
    fetch_campus_announcements,
    fetch_campus_details,
    parse_campus_announcements,
    parse_campus_details,
)


FIXTURES = Path(__file__).parent / "fixtures" / "sources" / "campus_json"

SJTU = CampusJsonSource(
    name="上海交通大学就业网实习",
    base_url="https://www.job.sjtu.edu.cn",
    list_path="/career/zpxx/search/sxzpxx",
    paged_list_path="/career/zpxx/search/sxzpxx/{page_num}/{page_size}",
    detail_path="/career/zpxx/data/zpxx/{announcement_id}",
    detail_page_path="/career/zpxx/zpxx/{announcement_id}",
)
SUFE = CampusJsonSource(
    name="上海财经大学就业网招聘",
    base_url="https://career.sufe.edu.cn",
    list_path="/career/zpxx/search/zpxx",
    paged_list_path="/career/zpxx/search/zpxx/{page_num}/{page_size}",
    detail_path="/career/zpxx/data/zpxx/{announcement_id}",
    detail_page_path="/career/zpxx/zpxx/{announcement_id}",
)


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parse_campus_announcements_preserves_parent_identity_title_date_and_detail_url():
    announcements = parse_campus_announcements(_fixture("list.json"), SJTU)

    assert [(item.parent_id, item.title, item.published_at) for item in announcements] == [
        ("sjtu-100", "上海示例咨询公司 2027 届实习生招聘", "2026-09-06"),
        ("sjtu-101", "北京示例公司实习生招聘", "2026-09-05"),
    ]
    assert announcements[0].detail_url == "https://www.job.sjtu.edu.cn/career/zpxx/zpxx/sjtu-100"
    assert announcements[1].detail_url == "https://www.job.sjtu.edu.cn/career/zpxx/detail/sjtu-101"


def test_parse_campus_announcements_skips_a_record_without_a_public_detail_url_when_no_template_is_configured():
    source_without_detail_page = CampusJsonSource(
        name="未确认详情页来源",
        base_url="https://example.edu.cn",
        list_path="/list",
        paged_list_path="/list/{page_num}/{page_size}",
        detail_path="/data/{announcement_id}",
    )

    announcements = parse_campus_announcements(_fixture("list.json"), source_without_detail_page)

    assert [item.parent_id for item in announcements] == ["sjtu-101"]


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Client:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.payload)


def test_known_school_configs_use_the_confirmed_anonymous_post_paths():
    assert SJTU_INTERNSHIP_SOURCE.list_path == "/career/zpxx/search/sxzpxx"
    assert SJTU_INTERNSHIP_SOURCE.paged_list_path == "/career/zpxx/search/sxzpxx/{page_num}/{page_size}"
    assert SJTU_INTERNSHIP_SOURCE.detail_path == "/career/zpxx/data/zpxx/{announcement_id}"
    assert SUFE_JOB_SOURCE.list_path == "/career/zpxx/search/zpxx"
    assert SUFE_JOB_SOURCE.paged_list_path == "/career/zpxx/search/zpxx/{page_num}/{page_size}"
    assert SUFE_JOB_SOURCE.detail_path == "/career/zpxx/data/zpxx/{announcement_id}"
    assert SJTU_INTERNSHIP_SOURCE.detail_page_path == "/career/zpxx/sxzpxx"
    assert SUFE_JOB_SOURCE.detail_page_path == "/career/zpxx/zpxx"


def test_fetch_campus_announcements_stops_when_a_page_repeats_without_new_ids():
    client = _Client(_fixture("list.json"))

    announcements = fetch_campus_announcements(client, SJTU)

    assert [item.parent_id for item in announcements] == ["sjtu-100", "sjtu-101"]
    assert [call[0] for call in client.calls] == [
        "https://www.job.sjtu.edu.cn/career/zpxx/search/sxzpxx",
        "https://www.job.sjtu.edu.cn/career/zpxx/search/sxzpxx/2/10",
    ]
    assert [call[1]["data"] for call in client.calls] == [
        {},
        {},
    ]


def test_fetch_campus_details_posts_an_empty_form_to_the_configured_detail_path():
    client = _Client(_fixture("detail-without-positions.json"))
    announcement = parse_campus_announcements(
        {
            "code": 200,
            "data": {
                "records": [
                    {
                        "zpxxid": "sufe-200",
                        "zpzt": "证券研究实习生招聘公告",
                        "fbrq": "2026-09-06",
                        "detailUrl": "/career/zpxx/sufe-200",
                    }
                ]
            },
        },
        SUFE,
    )[0]

    details = fetch_campus_details(client, SUFE, announcement)

    assert details[0].identity_key == "sufe-200"
    assert client.calls == [
        (
            "https://career.sufe.edu.cn/career/zpxx/data/zpxx/sufe-200",
            {
                "data": {},
                "headers": {"User-Agent": "LixinRecruitingLocal/0.1 (public-information-research; local-only)"},
                "timeout": 12.0,
            },
        )
    ]


def test_campus_json_requires_code_200_for_list_and_detail_payloads():
    bad_payload = {"code": 201, "data": {"records": []}}

    with pytest.raises(ValueError, match="code=200"):
        parse_campus_announcements(bad_payload, SJTU)
    with pytest.raises(ValueError, match="code=200"):
        parse_campus_details(bad_payload, object())


def test_parse_campus_details_emits_one_detail_per_position_with_body_and_application_evidence():
    announcement = parse_campus_announcements(_fixture("list.json"), SJTU)[0]

    details = parse_campus_details(_fixture("detail-with-positions.json"), announcement)

    assert [item.identity_key for item in details] == ["sjtu-100:position-a", "sjtu-100:position-b"]
    assert [item.title for item in details] == ["财务分析实习生", "数据分析实习生"]
    assert all(item.announcement_title == announcement.title for item in details)
    first, second = details
    assert "面向 2027 届在校生开放实习岗位。" in first.evidence_text
    assert "岗位名称：财务分析实习生" in first.evidence_text
    assert "岗位描述：协助财务分析、尽职调查和报告整理。" in first.evidence_text
    assert "岗位地点：上海市浦东新区" in first.evidence_text
    assert "官方报名入口：https://jobs.example.edu.cn/apply/sjtu-100" in first.evidence_text
    assert "官方投递邮箱：campus@example.edu.cn" in first.evidence_text
    assert first.location_category == "明确上海"
    assert second.location_category != "明确上海"
    assert second.location_detail == "北京市朝阳区"


def test_parse_campus_details_without_positions_keeps_the_parent_identity_and_official_mail_evidence():
    announcement = parse_campus_announcements(
        {
            "code": 200,
            "data": {
                "records": [
                    {
                        "zpxxid": "sufe-200",
                        "zpzt": "证券研究实习生招聘公告",
                        "fbrq": "2026-09-06",
                        "detailUrl": "/career/zpxx/sufe-200",
                    }
                ]
            },
        },
        SUFE,
    )[0]

    details = parse_campus_details(_fixture("detail-without-positions.json"), announcement)

    assert len(details) == 1
    assert details[0].identity_key == "sufe-200"
    assert details[0].title == "证券研究实习生招聘公告"
    assert details[0].location_category == "明确上海"
    assert "官方投递邮箱：research@example.edu.cn" in details[0].evidence_text


def test_parse_campus_details_uses_real_position_fields_company_name_and_only_explicit_work_locations():
    announcement = CampusAnnouncement(
        parent_id="sjtu-300",
        title="全国校园招聘公告",
        published_at="2026-09-07",
        detail_url="https://www.job.sjtu.edu.cn/career/zpxx/sxzpxx",
    )

    details = parse_campus_details(_fixture("detail-real-fields.json"), announcement)

    shanghai, unknown = details
    assert [item.identity_key for item in details] == ["sjtu-300:role-shanghai", "sjtu-300:role-unknown"]
    assert shanghai.employer_name == "示例金融科技有限公司"
    assert "招聘单位：示例金融科技有限公司" in shanghai.evidence_text
    assert "招聘单位：静安区" not in shanghai.evidence_text
    assert "岗位地点：上海市静安区" in shanghai.evidence_text
    assert "官方投递邮箱：role@example.edu.cn" in shanghai.evidence_text
    assert "announcement@example.edu.cn" not in shanghai.evidence_text
    assert shanghai.location_category == "明确上海"
    assert unknown.location_category == "原文未明确"
    assert unknown.location_detail == ""
    assert "官方投递邮箱：operations@example.edu.cn" in unknown.evidence_text


def test_original_article_url_is_not_exposed_as_an_application_url():
    announcement = CampusAnnouncement(
        "notice-original", "校园招聘公告", "2026-09-10", "https://career.example.edu/detail"
    )
    payload = {
        "code": 200,
        "data": {
            "zpxxEditor": "<p>岗位职责：协助运营。任职要求：面向在校生。工作地点：上海。</p>",
            "dwmc": "上海示例科技有限公司",
            "yurl": "https://company.example.com/news/campus-2027",
            "zwxxList": [],
        },
    }

    detail = parse_campus_details(payload, announcement)[0]

    assert detail.official_url == ""
    assert "官方原始链接：https://company.example.com/news/campus-2027" in detail.evidence_text


@pytest.mark.parametrize(
    ("body", "unit_address", "position_address", "expected_category"),
    [
        (
            "<p>岗位职责：协助投研。【工作地点】：上海。可远程办公。</p>",
            "北京市朝阳区示例路1号",
            "黄浦区大沽路",
            "明确上海",
        ),
        (
            "<p>岗位职责：协助量化研究。</p>",
            "中国（上海）自由贸易试验区泳耀路511弄2、3号",
            "泳耀路511弄6号楼7层",
            "明确上海",
        ),
        (
            "<p>岗位职责：协助量化研究。</p>",
            "上海市黄浦区南京东路100号",
            "桂平路391号A座28楼",
            "其他地区",
        ),
        (
            "<p>岗位职责：协助量化研究。</p>",
            "上海市静安区南京西路100号",
            "南京西路100号",
            "明确上海",
        ),
        (
            "<p>岗位职责：协助量化研究。</p>",
            "南京市上海大厦人民路100号",
            "人民路100号",
            "其他地区",
        ),
        (
            "<p>岗位职责：协助量化研究。</p>",
            "上海路100号",
            "上海路100号",
            "其他地区",
        ),
        (
            "<p>岗位职责：协助量化研究。</p>",
            "上海大厦人民路100号",
            "人民路100号",
            "其他地区",
        ),
        (
            "<p>岗位职责：协助运营。工作地点：上海。另一岗位工作地点：苏州。</p>",
            "北京市朝阳区示例路1号",
            "黄浦区大沽路",
            "其他地区",
        ),
        (
            "<p>岗位职责：协助投研。工作地点：上海。</p>",
            "上海市黄浦区南京东路100号",
            "深圳市南山区科技园",
            "其他地区",
        ),
    ],
)
def test_position_location_uses_only_direct_workplace_or_matching_shanghai_address_evidence(
    body, unit_address, position_address, expected_category
):
    announcement = CampusAnnouncement(
        "notice-cityless-position", "实习生招聘", "2026-10-01", "https://www.job.sjtu.edu.cn/career/zpxx/sxzpxx"
    )
    detail = parse_campus_details(
        {
            "code": 200,
            "data": {
                "zpxxEditor": body,
                "dwmc": "上海示例投资公司",
                "xxdz": unit_address,
                "zwxxList": [
                    {
                        "zwxxid": "role-1",
                        "zwmc": "投资实习生",
                        "gzdz": position_address,
                        "zwms": "协助投研",
                    }
                ],
            },
        },
        announcement,
    )[0]

    assert detail.location_category == expected_category
    assert position_address in detail.location_detail


def test_shared_parent_work_location_does_not_override_cityless_sibling_position():
    announcement = CampusAnnouncement(
        "notice-multiple-locations", "多城市实习生招聘", "2026-10-01", "https://www.job.sjtu.edu.cn/career/zpxx/sxzpxx"
    )
    details = parse_campus_details(
        {
            "code": 200,
            "data": {
                "zpxxEditor": "<p>岗位职责：见具体岗位。工作地点：上海。</p>",
                "dwmc": "示例全国集团",
                "xxdz": "北京市朝阳区示例路1号",
                "zwxxList": [
                    {"zwxxid": "role-shanghai", "zwmc": "上海实习生", "gzdz": "上海市黄浦区"},
                    {"zwxxid": "role-other", "zwmc": "异地实习生", "gzdz": "天心区"},
                ],
            },
        },
        announcement,
    )

    assert details[0].location_category == "明确上海"
    assert details[1].location_category != "明确上海"


def test_split_position_cannot_inherit_sibling_student_eligibility_from_parent_body():
    from datetime import datetime

    from app.services.review_intake import evaluate_review_intake
    from app.services.source_candidate_policy import candidate_from_detail, evaluate_candidate

    announcement = CampusAnnouncement(
        "notice-mixed-roles", "多岗位招聘", "2026-10-01", "https://career.sufe.edu.cn/career/zpxx/zpxx"
    )
    details = parse_campus_details(
        {
            "code": 200,
            "data": {
                "zpxxEditor": (
                    "<p>财务实习岗位：接受2027届应届生，负责财务分析。</p>"
                    "<p>策划专员岗位：按岗位描述申请。</p>"
                ),
                "dwmc": "上海示例公司",
                "jltdyx": "apply@example.com",
                "zwxxList": [
                    {"zwxxid": "intern", "zwmc": "财务实习生", "zwms": "岗位职责：协助财务分析。任职要求：在校生。", "gzdz": "上海市徐汇区"},
                    {"zwxxid": "planner", "zwmc": "策划专员", "zwms": "岗位职责：负责活动策划。任职要求：本科及以上。", "gzdz": "上海市徐汇区"},
                ],
            },
        },
        announcement,
    )
    planner = details[1]
    candidate = candidate_from_detail(planner)

    assert "接受2027届应届生" in planner.evidence_text  # Full notice remains for manual review.
    assert evaluate_candidate(candidate_from_detail(details[0]), now=datetime(2026, 10, 5)).verdict == "qualified"
    decision = evaluate_review_intake(candidate, now=datetime(2026, 10, 5))
    assert (decision.verdict, decision.reason_codes) == ("review_only", ("TARGET_AUDIENCE_UNCLEAR",))


def test_split_position_cannot_inherit_sibling_application_email_from_parent_body():
    from app.services.source_candidate_policy import candidate_from_detail

    announcement = CampusAnnouncement(
        "notice-role-email", "多岗位招聘", "2026-10-01", "https://career.sufe.edu.cn/career/zpxx/zpxx"
    )
    details = parse_campus_details(
        {
            "code": 200,
            "data": {
                "zpxxEditor": "<p>财务实习生请投递，官方投递邮箱：intern@example.com。</p>",
                "dwmc": "上海示例公司",
                "zwxxList": [
                    {"zwxxid": "intern", "zwmc": "财务实习生", "zwms": "岗位职责：协助财务分析。", "gzdz": "上海市徐汇区"},
                    {"zwxxid": "planner", "zwmc": "策划专员", "zwms": "岗位职责：负责活动策划。任职要求：本科及以上。", "gzdz": "上海市徐汇区"},
                ],
            },
        },
        announcement,
    )
    planner = candidate_from_detail(details[1])

    assert "intern@example.com" in details[1].evidence_text
    assert (planner.application_kind, planner.application_value, planner.application_evidence) == ("", "", "")


def test_split_position_cannot_inherit_student_claim_from_parent_announcement_title():
    from datetime import datetime

    from app.services.review_intake import evaluate_review_intake
    from app.services.source_candidate_policy import candidate_from_detail

    announcement = CampusAnnouncement(
        "notice-campus-title", "2027届校园招聘公告", "2026-10-01", "https://career.sufe.edu.cn/career/zpxx/zpxx"
    )
    details = parse_campus_details(
        {
            "code": 200,
            "data": {
                "zpxxEditor": "<p>请按具体岗位要求申请。</p>",
                "dwmc": "上海示例公司",
                "jltdyx": "apply@example.com",
                "zwxxList": [
                    {"zwxxid": "intern", "zwmc": "财务实习生", "zwms": "岗位职责：协助财务分析。", "gzdz": "上海市徐汇区"},
                    {"zwxxid": "planner", "zwmc": "策划专员", "zwms": "岗位职责：负责活动策划。任职要求：本科及以上。", "gzdz": "上海市徐汇区"},
                ],
            },
        },
        announcement,
    )

    candidate = candidate_from_detail(details[1])
    decision = evaluate_review_intake(candidate, now=datetime(2026, 10, 5))
    assert (decision.verdict, decision.reason_codes) == ("review_only", ("TARGET_AUDIENCE_UNCLEAR",))


def test_single_position_retains_parent_student_and_application_evidence():
    from datetime import datetime

    from app.services.source_candidate_policy import candidate_from_detail, evaluate_candidate

    announcement = CampusAnnouncement(
        "notice-single-role", "专员招聘", "2026-10-01", "https://career.sufe.edu.cn/career/zpxx/zpxx"
    )
    detail = parse_campus_details(
        {
            "code": 200,
            "data": {
                "zpxxEditor": (
                    "<p>岗位职责：负责活动策划。任职要求：接受2027届应届生。"
                    "工作地点：上海。官方投递邮箱：apply@example.com。</p>"
                ),
                "dwmc": "上海示例公司",
                "zwxxList": [
                    {"zwxxid": "planner", "zwmc": "策划专员", "zwms": "岗位职责：负责活动策划。", "gzdz": "上海市徐汇区"},
                ],
            },
        },
        announcement,
    )[0]

    candidate = candidate_from_detail(detail)
    assert candidate.application_value == "apply@example.com"
    assert evaluate_candidate(candidate, now=datetime(2026, 10, 5)).verdict == "qualified"


@pytest.mark.parametrize(
    ("description", "expected_verdict", "expected_reason"),
    [
        ("协助财务分析、尽职调查和报告整理。", "qualified", ""),
        ("详见公告", "needs_evidence", "BODY_NOT_JOB_SPECIFIC"),
    ],
)
def test_split_position_description_must_have_substantive_role_duties(
    description, expected_verdict, expected_reason
):
    from datetime import datetime

    from app.services.source_candidate_policy import candidate_from_detail, evaluate_candidate

    announcement = CampusAnnouncement(
        "notice-duties", "多岗位招聘", "2026-10-01", "https://career.sufe.edu.cn/career/zpxx/zpxx"
    )
    details = parse_campus_details(
        {
            "code": 200,
            "data": {
                "zpxxEditor": "<p>查看各岗位信息后投递。</p>",
                "dwmc": "上海示例公司",
                "jltdyx": "apply@example.com",
                "zwxxList": [
                    {"zwxxid": "intern", "zwmc": "财务实习生", "zwms": description, "gzdz": "上海市徐汇区"},
                    {"zwxxid": "planner", "zwmc": "策划专员", "zwms": "负责活动策划和项目协同。", "gzdz": "上海市徐汇区"},
                ],
            },
        },
        announcement,
    )
    candidate = candidate_from_detail(details[0])

    decision = evaluate_candidate(candidate, now=datetime(2026, 10, 5))
    assert decision.verdict == expected_verdict
    assert (expected_reason in decision.reason_codes) if expected_reason else not decision.reason_codes


@pytest.mark.parametrize("description", [
    "岗位职责：负责2027届毕业生招聘活动。任职要求：本科及以上学历。",
    "岗位职责：负责招聘活动统筹。任职要求：熟悉2027届毕业生招聘流程。",
    "岗位职责：负责招聘活动统筹。任职要求：2027届毕业生招聘经验优先。",
])
def test_recruiter_work_about_graduates_does_not_prove_recruiter_is_student_eligible(description):
    from datetime import datetime

    from app.services.review_intake import evaluate_review_intake
    from app.services.source_candidate_policy import candidate_from_detail

    announcement = CampusAnnouncement(
        "notice-recruiter", "企业招聘", "2026-10-01", "https://career.sufe.edu.cn/career/zpxx/zpxx"
    )
    details = parse_campus_details(
        {
            "code": 200,
            "data": {
                "zpxxEditor": "<p>招聘信息以具体岗位为准。</p>",
                "dwmc": "上海示例公司",
                "jltdyx": "apply@example.com",
                "zwxxList": [
                    {"zwxxid": "recruiter", "zwmc": "招聘专员", "zwms": description, "gzdz": "上海市徐汇区"},
                    {"zwxxid": "finance", "zwmc": "财务实习生", "zwms": "岗位职责：协助财务分析。任职要求：接受2027届毕业生。", "gzdz": "上海市徐汇区"},
                ],
            },
        },
        announcement,
    )
    recruiter = candidate_from_detail(details[0])

    decision = evaluate_review_intake(recruiter, now=datetime(2026, 10, 5))
    assert (decision.verdict, decision.reason_codes) == ("review_only", ("TARGET_AUDIENCE_UNCLEAR",))


@pytest.mark.parametrize("recruiter_title", ["校招运营专员", "校园招聘运营专员"])
def test_recruiter_title_with_campus_word_is_not_itself_student_eligibility(recruiter_title):
    from datetime import datetime

    from app.services.review_intake import evaluate_review_intake
    from app.services.source_candidate_policy import candidate_from_detail

    announcement = CampusAnnouncement(
        "notice-campus-hr", "多岗位招聘", "2026-10-01", "https://career.sufe.edu.cn/career/zpxx/zpxx"
    )
    details = parse_campus_details(
        {
            "code": 200,
            "data": {
                "zpxxEditor": "<p>岗位具体条件以岗位说明为准。</p>",
                "dwmc": "上海示例公司",
                "jltdyx": "apply@example.com",
                "zwxxList": [
                    {"zwxxid": "recruiter", "zwmc": recruiter_title, "zwms": "岗位职责：负责校园招聘活动组织。任职要求：本科及以上学历。", "gzdz": "上海市徐汇区"},
                    {"zwxxid": "finance", "zwmc": "财务实习生", "zwms": "岗位职责：协助财务分析。任职要求：接受2027届毕业生。", "gzdz": "上海市徐汇区"},
                ],
            },
        },
        announcement,
    )

    decision = evaluate_review_intake(candidate_from_detail(details[0]), now=datetime(2026, 10, 5))
    assert (decision.verdict, decision.reason_codes) == ("review_only", ("TARGET_AUDIENCE_UNCLEAR",))


def test_explicit_all_positions_experience_requirement_excludes_split_intern_role():
    from datetime import datetime

    from app.services.review_intake import evaluate_review_intake
    from app.services.source_candidate_policy import candidate_from_detail

    announcement = CampusAnnouncement(
        "notice-all-senior", "多岗位招聘", "2026-10-01", "https://career.sufe.edu.cn/career/zpxx/zpxx"
    )
    details = parse_campus_details(
        {
            "code": 200,
            "data": {
                "zpxxEditor": "<p>所有岗位均要求3年以上工作经验。</p>",
                "dwmc": "上海示例公司",
                "jltdyx": "apply@example.com",
                "zwxxList": [
                    {"zwxxid": "intern", "zwmc": "数据实习生", "zwms": "岗位职责：协助数据整理。任职要求：接受2027届毕业生。", "gzdz": "上海市徐汇区"},
                    {"zwxxid": "planner", "zwmc": "策划专员", "zwms": "岗位职责：负责活动策划。任职要求：本科及以上。", "gzdz": "上海市徐汇区"},
                ],
            },
        },
        announcement,
    )
    intern = candidate_from_detail(details[0])

    decision = evaluate_review_intake(intern, now=datetime(2026, 10, 5))
    assert decision.verdict == "excluded"
    assert "SENIOR_ROLE" in decision.reason_codes


def test_campus_request_retries_one_real_httpx_timeout():
    from app.services.source_request_budget import RequestBudgetController

    class TimeoutThenSuccess(_Client):
        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if len(self.calls) == 1:
                raise httpx.ReadTimeout("socket stalled", request=httpx.Request("POST", url))
            return _Response(self.payload)

    client = TimeoutThenSuccess({"code": 200, "data": {"records": []}})
    controller = RequestBudgetController(
        total_seconds=10,
        request_timeout_seconds=3,
        interval_seconds=0,
        sleeper=lambda _seconds: None,
    )

    assert fetch_campus_announcements(client, SJTU, pages=1, controller=controller) == []
    assert len(client.calls) == 2
