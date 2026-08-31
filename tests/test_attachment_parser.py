from io import BytesIO

from openpyxl import Workbook

from app.services.attachment_parser import parse_xlsx_role_candidates
from app.services.attachment_parser import create_pending_child_jobs
from app.models import Job
from app.main import create_app
from fastapi.testclient import TestClient
import httpx


def _xlsx_bytes(rows: list[list[str]]) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "岗位说明"
    for row in rows:
        worksheet.append(row)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_parse_xlsx_role_candidates_extracts_rows_under_role_header():
    content = _xlsx_bytes(
        [
            ["序号", "岗位名称", "岗位要求", "人数"],
            ["1", "财务管理岗", "会计、财务管理相关专业", "2"],
            ["2", "审计岗", "审计相关专业", "1"],
        ]
    )

    candidates = parse_xlsx_role_candidates(content)

    assert [candidate.title for candidate in candidates] == ["财务管理岗", "审计岗"]
    assert candidates[0].row_number == 2
    assert "会计、财务管理相关专业" in candidates[0].evidence


def test_parse_xlsx_role_candidates_refuses_sheet_without_clear_role_column():
    content = _xlsx_bytes([["序号", "部门", "说明"], ["1", "财务部", "招聘说明"]])

    candidates = parse_xlsx_role_candidates(content)

    assert candidates == []


def test_create_pending_child_jobs_uses_attachment_rows_without_publishing(session):
    parent = Job(
        fingerprint="附件拆岗父公告", employer_name="测试学院", job_title="2026年招聘公告", job_family="待分类",
        recruitment_type="事业单位公开招聘", location_category="明确上海", location_detail="上海",
        target_audience="需按具体岗位判断", direction_tags="待人工分类", deadline="2026-09-30",
        official_url="https://example.com/notice", source_url="https://example.com/source", evidence_text="公开招聘公告",
        quality_score=80, risk_flags="待最终人工审核", is_demo=False, status="待核验",
        posting_scope="multi_role_announcement", attachment_status="checked",
    )
    session.add(parent)
    session.commit()
    candidates = parse_xlsx_role_candidates(_xlsx_bytes([["岗位名称", "专业要求"], ["财务管理岗", "会计专业"]]))

    children = create_pending_child_jobs(
        session, parent, "岗位说明.xlsx", "https://example.com/roles.xlsx", candidates, "本地管理员"
    )

    assert len(children) == 1
    assert children[0].job_title == "财务管理岗"
    assert children[0].parent_job_id == parent.id
    assert children[0].status == "待核验"
    assert "会计专业" in children[0].evidence_text


def test_verified_xlsx_attachment_can_create_pending_child_jobs_from_detail_page(monkeypatch):
    app = create_app("sqlite+pysqlite:///:memory:")
    content = _xlsx_bytes([["岗位名称", "专业要求"], ["财务管理岗", "会计专业"]])
    with app.state.session_factory() as session:
        parent = Job(
            fingerprint="附件页面父公告", employer_name="测试学院", job_title="2026年招聘公告", job_family="待分类",
            recruitment_type="事业单位公开招聘", location_category="明确上海", location_detail="上海",
            target_audience="需按具体岗位判断", direction_tags="待人工分类", deadline="2026-09-30",
            official_url="https://example.com/notice", source_url="https://example.com/source", evidence_text="公开招聘公告",
            quality_score=80, risk_flags="待最终人工审核", is_demo=False, status="待核验",
            posting_scope="multi_role_announcement", attachment_status="checked",
            attachment_links='[{"name":"岗位说明.xlsx","url":"https://example.com/roles.xlsx"}]',
        )
        session.add(parent)
        session.commit()
        parent_id = parent.id

    monkeypatch.setattr("app.main.httpx.get", lambda *args, **kwargs: httpx.Response(200, content=content))
    client = TestClient(app)
    page = client.get(f"/jobs/{parent_id}")
    response = client.post(
        f"/jobs/{parent_id}/attachments/parse",
        data={"attachment_name": "岗位说明.xlsx", "attachment_url": "https://example.com/roles.xlsx"},
    )

    assert "从附件拆分岗位" in page.text
    assert response.status_code == 200
    assert "已从官方附件生成 1 条待核验岗位" in response.text
    with app.state.session_factory() as session:
        child = session.query(Job).filter(Job.parent_job_id == parent_id).one()
        assert child.job_title == "财务管理岗"
        assert child.status == "待核验"


def test_parent_and_child_detail_pages_show_attachment_split_lineage():
    app = create_app("sqlite+pysqlite:///:memory:")
    with app.state.session_factory() as session:
        parent = Job(
            fingerprint="追溯父公告", employer_name="测试学院", job_title="2026年招聘公告", job_family="待分类",
            recruitment_type="事业单位公开招聘", location_category="明确上海", location_detail="上海",
            target_audience="需按具体岗位判断", direction_tags="待人工分类", deadline="2026-09-30",
            official_url="https://example.com/notice", source_url="https://example.com/source", evidence_text="公开招聘公告",
            quality_score=80, risk_flags="待最终人工审核", is_demo=False, status="待核验",
        )
        session.add(parent)
        session.flush()
        child = Job(
            fingerprint="追溯子岗位", employer_name="测试学院", job_title="财务管理岗", job_family="待人工分类",
            recruitment_type="事业单位公开招聘", location_category="明确上海", location_detail="上海",
            target_audience="需按具体岗位判断", direction_tags="待人工分类", deadline="2026-09-30",
            official_url="https://example.com/notice", source_url="https://example.com/source", evidence_text="附件行",
                quality_score=0, risk_flags="待人工核验", is_demo=False, status="待核验", parent_job_id=parent.id,
                notice_type="新招聘",
        )
        session.add(child)
        session.commit()
        parent_id, child_id = parent.id, child.id

    client = TestClient(app)
    parent_page = client.get(f"/jobs/{parent_id}")
    child_page = client.get(f"/jobs/{child_id}")

    assert "已从本公告拆分的岗位" in parent_page.text
    assert "待核验 1 条" in parent_page.text
    assert "财务管理岗" in parent_page.text
    assert f"/jobs/{child_id}" in parent_page.text
    assert f"/jobs/{child_id}/structure" in parent_page.text
    assert "去结构化" in parent_page.text
    assert "来源公告" in child_page.text
    assert f"/jobs/{parent_id}" in child_page.text
