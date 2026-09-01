from pathlib import Path
import subprocess
from datetime import datetime

from fastapi.testclient import TestClient

from app.main import DEFAULT_DATABASE_PATH, create_app
from app.models import Job, ReviewLog
from app.services.ai_settings import AiSettingsService
from app.services.real_collection import RealCollectionResult


class FakeCredentialStore:
    def __init__(self):
        self.secret = None

    def set_secret(self, value: str) -> None:
        self.secret = value

    def get_secret(self) -> str | None:
        return self.secret

    def has_secret(self) -> bool:
        return bool(self.secret)


class FakeBailianClient:
    def test_connection(self, api_key: str, model: str) -> None:
        return None


def create_app_with_fake_ai_settings():
    app = create_app("sqlite+pysqlite:///:memory:")
    app.state.ai_settings_service = AiSettingsService(FakeCredentialStore(), FakeBailianClient())
    return app


def test_default_database_directory_exists_after_app_import():
    assert DEFAULT_DATABASE_PATH.parent.exists()


def test_dashboard_shows_non_official_identity_and_status_counts():
    client = TestClient(create_app("sqlite+pysqlite:///:memory:"))

    response = client.get("/")

    assert response.status_code == 200
    assert "非官方就业信息服务" in response.text
    assert "待审核" in response.text


def test_base_layout_has_mobile_viewport_and_v2_navigation():
    client = TestClient(create_app("sqlite+pysqlite:///:memory:"))

    response = client.get("/")

    assert (
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        in response.text
    )
    for label in (
        "工作总览",
        "公告中心",
        "岗位中心",
        "审核工作台",
        "内容队列",
        "来源监控",
        "AI 模型配置",
    ):
        assert label in response.text
    assert "本地环境" in response.text
    assert "本地管理员" in response.text


def test_ai_settings_page_has_safe_model_controls_and_no_secret_value():
    client = TestClient(create_app_with_fake_ai_settings())

    response = client.get("/settings/ai")

    assert response.status_code == 200
    assert "AI 模型配置" in response.text
    assert "qwen3.7-flash" in response.text
    assert "qwen-vl-ocr" in response.text
    assert 'type="password"' in response.text
    assert "AI 只辅助生成草稿" in response.text
    assert "value=\"sk-" not in response.text


def test_ai_settings_key_and_model_forms_redirect_and_keep_secret_out_of_html():
    client = TestClient(create_app_with_fake_ai_settings())
    secret = "sk-private-browser-key-ABCD"

    saved_key = client.post("/settings/ai/key", data={"api_key": secret}, follow_redirects=False)
    saved_models = client.post(
        "/settings/ai/models",
        data={
            "text_model": "qwen3.7-flash",
            "ocr_model": "qwen-vl-ocr",
            "text_enabled": "on",
            "ocr_enabled": "on",
        },
        follow_redirects=False,
    )
    page = client.get("/settings/ai")

    assert saved_key.status_code == 303
    assert saved_models.status_code == 303
    assert secret not in page.text
    assert "****ABCD" in page.text


def test_ai_settings_test_connection_redirects_to_settings_page():
    client = TestClient(create_app_with_fake_ai_settings())
    client.post("/settings/ai/key", data={"api_key": "sk-private-browser-key-ABCD"})

    response = client.post("/settings/ai/test", follow_redirects=False)
    page = client.get("/settings/ai")

    assert response.status_code == 303
    assert "连接正常" in page.text


def test_sources_navigation_marks_current_page():
    client = TestClient(create_app("sqlite+pysqlite:///:memory:"))

    response = client.get("/sources")

    assert 'aria-current="page"' in response.text
    assert "来源监控" in response.text


def test_dashboard_prioritizes_real_collection_and_keeps_demo_in_dev_tools():
    client = TestClient(create_app("sqlite+pysqlite:///:memory:"))

    response = client.get("/")

    assert "运行每日采集" in response.text
    assert "今日待办" in response.text
    assert "流程状态" in response.text
    assert "来源健康" in response.text
    assert "开发工具" in response.text
    assert "运行模拟采集" in response.text


def test_dashboard_operational_counts_exclude_demo_records():
    client = TestClient(create_app("sqlite+pysqlite:///:memory:"))

    response = client.get("/")

    assert "待处理</span><strong>0</strong>" in response.text
    assert "待审核岗位</span><strong>0</strong>" in response.text
    assert "真实线索总数</span><strong>0</strong>" in response.text


def test_jobs_can_separate_real_clues_from_demo_jobs():
    client = TestClient(create_app("sqlite+pysqlite:///:memory:"))

    demo_page = client.get("/jobs?data_type=demo")
    real_page = client.get("/jobs?data_type=real")

    assert demo_page.status_code == 200
    assert real_page.status_code == 200
    assert "示例金融集团" in demo_page.text
    assert "示例金融集团" not in real_page.text


def test_real_jobs_page_shows_collection_time_for_each_real_clue():
    app = create_app("sqlite+pysqlite:///:memory:")
    with app.state.session_factory() as session:
        session.add(
            Job(
                fingerprint="拉取时间列表|岗位|2026-09-01|上海|公告",
                employer_name="时间展示测试单位",
                job_title="招聘专员",
                job_family="综合职能",
                recruitment_type="初级社招",
                location_category="明确上海",
                location_detail="上海",
                target_audience="毕业两年内",
                direction_tags="工商运营",
                deadline="招满即止",
                official_url="https://example.com/apply",
                source_url="https://example.com/source",
                evidence_text="公开招聘原文证据。",
                quality_score=75,
                risk_flags="待人工核验",
                is_demo=False,
                collected_at=datetime(2026, 9, 1, 16, 30),
                status="待核验",
                notice_type="新招聘",
            )
        )
        session.commit()

    response = TestClient(app).get("/jobs?data_type=real")

    assert response.status_code == 200
    assert "拉取时间" in response.text
    assert "2026-09-01 16:30" in response.text


def test_jobs_reject_invalid_data_type():
    client = TestClient(create_app("sqlite+pysqlite:///:memory:"))

    response = client.get("/jobs?data_type=unknown")

    assert response.status_code == 400


def test_real_job_detail_uses_collapsible_evidence_and_next_step_guidance(monkeypatch):
    app = create_app("sqlite+pysqlite:///:memory:")

    def fake_collect(session, client, limit=12):
        session.add(
            Job(
                fingerprint="详情测试|公告|2026-08-13|上海|公告",
                employer_name="待人工核验（上海国资招聘公告）",
                job_title="详情页真实公告",
                job_family="待分类",
                recruitment_type="待核验",
                location_category="明确上海",
                location_detail="上海（公告来源）",
                target_audience="待人工判断",
                direction_tags="待人工分类",
                deadline="原文待人工确认",
                official_url="",
                source_url="https://example.com/real-source-detail",
                evidence_text="公开招聘公告完整原文。",
                quality_score=0,
                risk_flags="真实线索：尚未人工核验，不得对外发布",
                is_demo=False,
                status="待核验",
            )
        )
        session.commit()
        return RealCollectionResult(1, 0, 0)

    monkeypatch.setattr("app.main.collect_shanghai_sasac", fake_collect)
    client = TestClient(app)
    client.post("/tasks/shanghai-sasac-collection")

    response = client.get("/jobs/11")

    assert response.status_code == 200
    assert "查看完整采集原文" in response.text
    assert "下一步" in response.text
    assert "公告类型建议" in response.text


def test_sources_page_explains_real_and_demo_source_boundaries():
    client = TestClient(create_app("sqlite+pysqlite:///:memory:"))

    response = client.get("/sources")

    assert "当前已接入公开官网来源" in response.text
    assert "演示来源仅用于规划" in response.text
    assert "待专用适配" in response.text
    assert "每日采集" in response.text


def test_wechat_lead_import_page_is_available():
    client = TestClient(create_app("sqlite+pysqlite:///:memory:"))

    response = client.get("/sources/wechat-leads/import")

    assert response.status_code == 200
    assert "公众号招聘线索导入" in response.text
    assert "上海华智公考" in response.text


def test_queue_page_labels_both_channels_and_manual_sending_boundary():
    client = TestClient(create_app("sqlite+pysqlite:///:memory:"))
    client.post(
        "/jobs/1/review",
        data={"action": "approve", "note": "页面测试"},
        follow_redirects=False,
    )
    client.post("/jobs/1/distribution", follow_redirects=False)

    response = client.get("/queues")

    assert "公众号" in response.text
    assert "微信群" in response.text
    assert "不自动发送" in response.text


def test_public_queue_item_opens_a_copy_ready_wechat_draft_page():
    client = TestClient(create_app("sqlite+pysqlite:///:memory:"))
    client.post(
        "/jobs/1/review",
        data={"action": "approve", "note": "页面测试"},
        follow_redirects=False,
    )
    client.post("/jobs/1/distribution", follow_redirects=False)

    queue_page = client.get("/queues")
    draft_page = client.get("/distribution/1/wechat")

    assert "打开公众号复制稿" in queue_page.text
    assert draft_page.status_code == 200
    assert "复制公众号正文" in draft_page.text
    assert "复制标题" in draft_page.text
    assert "复制群消息" in draft_page.text
    assert "招聘速览" in draft_page.text


def test_review_endpoint_approves_a_valid_pending_job():
    client = TestClient(create_app("sqlite+pysqlite:///:memory:"))
    jobs = client.get("/jobs?data_type=demo").text
    assert "示例金融集团" in jobs

    response = client.post(
        "/jobs/1/review",
        data={"action": "approve", "note": "核验完成"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "审核通过" in response.text
    assert "已进入可发布状态" in response.text


def test_review_page_stays_on_job_and_lists_publish_failures():
    app = create_app("sqlite+pysqlite:///:memory:")
    with app.state.session_factory() as session:
        job = Job(
            fingerprint="审核反馈|测试", employer_name="测试单位", job_title="测试公告", job_family="待分类",
            recruitment_type="待核验", location_category="地区待定", location_detail="以公告原文为准",
            target_audience="待人工判断", direction_tags="待人工分类", deadline="原文待人工确认",
            official_url="", source_url="https://example.com/source", evidence_text="公开原文",
            quality_score=0, risk_flags="尚未人工核验，不得对外发布", is_demo=False, status="待审核",
        )
        session.add(job)
        session.commit()
        job_id = job.id

    response = TestClient(app).post(
        f"/jobs/{job_id}/review",
        data={"action": "approve", "note": "已核对公开原文、投递方式和截止日期"},
    )

    assert response.status_code == 200
    assert "暂不能通过" in response.text
    assert "质量分不足70" in response.text
    assert "请补齐后再次提交" in response.text


def test_local_app_rejects_distribution_for_pending_review_job():
    client = TestClient(create_app("sqlite+pysqlite:///:memory:"))

    response = client.post("/jobs/1/distribution")

    assert response.status_code == 400


def test_local_start_script_and_readme_exist():
    project_root = Path(__file__).resolve().parents[1]

    assert (project_root / "run_local.ps1").is_file()
    assert (project_root / "README.md").is_file()


def test_readme_documents_v21_source_library_and_manual_review_boundary():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")

    assert "71 家分层来源库" in readme
    assert "只有 A 类" in readme
    assert "人工审核" in readme
    assert "上海华智公考" in readme


def test_local_start_script_has_valid_powershell_syntax():
    project_root = Path(__file__).resolve().parents[1]
    script = project_root / "run_local.ps1"
    command = (
        "[scriptblock]::Create("
        "[System.IO.File]::ReadAllText('"
        + str(script).replace("'", "''")
        + "')) | Out-Null"
    )

    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_daily_collection_scripts_exist_and_schedule_script_has_valid_powershell_syntax():
    project_root = Path(__file__).resolve().parents[1]
    assert (project_root / "scripts" / "run_due_collection.py").is_file()
    script = project_root / "install_daily_schedule.ps1"
    command = "[scriptblock]::Create([System.IO.File]::ReadAllText('" + str(script).replace("'", "''") + "')) | Out-Null"
    result = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_mobile_primary_navigation_uses_wrapped_grid_instead_of_horizontal_scroll():
    project_root = Path(__file__).resolve().parents[1]
    css = (project_root / "app" / "static" / "app.css").read_text(encoding="utf-8")

    assert ".primary-nav{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));overflow:visible" in css


def test_real_collection_route_keeps_collected_item_pending_verification(monkeypatch):
    app = create_app("sqlite+pysqlite:///:memory:")

    def fake_collect(session, client, limit=12):
        session.add(
            Job(
                fingerprint="真实来源测试|公告|2026-08-13|上海|公告",
                employer_name="待人工核验（上海国资招聘公告）",
                job_title="真实来源测试公告",
                job_family="待分类",
                recruitment_type="待核验",
                location_category="明确上海",
                location_detail="上海（公告来源）",
                target_audience="待人工判断",
                direction_tags="待人工分类",
                deadline="原文待人工确认",
                official_url="",
                source_url="https://example.com/real-source-test",
                evidence_text="这是公开来源的真实线索测试原文。",
                quality_score=0,
                risk_flags="真实线索：尚未人工核验，不得对外发布",
                is_demo=False,
                status="待核验",
            )
        )
        session.commit()
        return RealCollectionResult(1, 0, 0)

    monkeypatch.setattr("app.main.collect_shanghai_sasac", fake_collect)
    client = TestClient(app)

    response = client.post("/tasks/shanghai-sasac-collection", follow_redirects=False)

    assert response.status_code == 303
    assert "真实线索" in client.get("/jobs").text


def test_pending_verification_job_opens_structuring_form_and_submits():
    app = create_app("sqlite+pysqlite:///:memory:")
    with app.state.session_factory() as session:
        job = Job(
            fingerprint="网页结构化|公告|2026-08-26|上海|公告",
            employer_name="待人工核验（测试公告）",
            job_title="网页结构化测试公告",
            job_family="待分类",
            recruitment_type="待核验",
            location_category="地区待定",
            location_detail="以公告原文为准",
            target_audience="待人工判断",
            direction_tags="待人工分类",
            deadline="原文待人工确认",
            official_url="",
            source_url="https://example.com/source",
            evidence_text="公开招聘公告原文证据。",
            quality_score=0,
            risk_flags="真实线索：尚未人工核验，不得对外发布",
            is_demo=False,
            status="待核验",
            notice_type="新招聘",
        )
        session.add(job)
        session.commit()
        job_id = job.id

    client = TestClient(app)
    form = client.get(f"/jobs/{job_id}/structure")
    response = client.post(
        f"/jobs/{job_id}/structure",
        data={
            "employer_name": "测试单位",
            "job_title": "财务分析实习生",
            "job_family": "财务分析",
            "recruitment_type": "实习",
            "location_category": "明确上海",
            "location_detail": "上海市浦东新区",
            "target_audience": "大三实习",
            "direction_tags": "会计审计、金融银行",
            "deadline": "2026-09-30",
            "official_url": "https://example.com/apply",
            "note": "核对公开原文后填写。",
        },
        follow_redirects=False,
    )

    assert form.status_code == 200
    assert "公告结构化" in form.text
    assert "官方报名链接" in form.text
    assert response.status_code == 303
    with app.state.session_factory() as session:
        assert session.get(Job, job_id).status == "待审核"


def test_structuring_page_explains_deadline_can_be_explicitly_unstated():
    app = create_app("sqlite+pysqlite:///:memory:")
    with app.state.session_factory() as session:
        job = Job(
            fingerprint="截止日期表单|公告|2026-08-31|上海|公告",
            employer_name="测试单位",
            job_title="测试招聘公告",
            job_family="待分类",
            recruitment_type="待核验",
            location_category="地区待定",
            location_detail="以公告原文为准",
            target_audience="待人工判断",
            direction_tags="待人工分类",
            deadline="原文待人工确认",
            official_url="",
            source_url="https://example.com/source",
            evidence_text="公开招聘公告原文证据。",
            quality_score=0,
            risk_flags="真实线索：尚未人工核验，不得对外发布",
            is_demo=False,
            status="待核验",
            notice_type="新招聘",
        )
        session.add(job)
        session.commit()
        job_id = job.id

    response = TestClient(app).get(f"/jobs/{job_id}/structure")

    assert response.status_code == 200
    assert "公告未明确统一截止时间" in response.text
    assert 'name="deadline"' in response.text
    assert 'required name="deadline"' not in response.text


def test_pending_verification_detail_links_to_structuring_not_final_approval():
    app = create_app("sqlite+pysqlite:///:memory:")
    with app.state.session_factory() as session:
        job = Job(
            fingerprint="详情结构化|公告|2026-08-26|上海|公告",
            employer_name="待人工核验（测试公告）",
            job_title="待核验详情测试公告",
            job_family="待分类",
            recruitment_type="待核验",
            location_category="地区待定",
            location_detail="以公告原文为准",
            target_audience="待人工判断",
            direction_tags="待人工分类",
            deadline="原文待人工确认",
            official_url="",
            source_url="https://example.com/source",
            evidence_text="公开招聘公告原文证据。",
            quality_score=0,
            risk_flags="真实线索：尚未人工核验，不得对外发布",
            is_demo=False,
            status="待核验",
            notice_type="新招聘",
        )
        session.add(job)
        session.commit()
        job_id = job.id

    response = TestClient(app).get(f"/jobs/{job_id}")

    assert "开始公告结构化" in response.text
    assert "公告结构化 · 第二批" not in response.text


def _create_ai_ready_job(app) -> int:
    with app.state.session_factory() as session:
        job = Job(
            fingerprint="AI预填反馈|公告|2026-08-31|上海|公告",
            employer_name="待人工核验（测试单位）",
            job_title="AI预填反馈测试公告",
            job_family="待分类",
            recruitment_type="待核验",
            location_category="地区待定",
            location_detail="以公告原文为准",
            target_audience="待人工判断",
            direction_tags="待人工分类",
            deadline="原文待人工确认",
            official_url="",
            source_url="https://example.com/source",
            evidence_text="公开招聘公告原文证据。",
            quality_score=0,
            risk_flags="真实线索：尚未人工核验，不得对外发布",
            is_demo=False,
            status="待核验",
            notice_type="新招聘",
        )
        session.add(job)
        session.commit()
        return job.id


def test_ai_prefill_missing_key_returns_to_structuring_page_with_safe_feedback():
    app = create_app_with_fake_ai_settings()
    job_id = _create_ai_ready_job(app)

    response = TestClient(app).post(
        f"/jobs/{job_id}/structure/ai-draft", follow_redirects=False
    )

    assert response.status_code == 303
    assert "ai_feedback=error" in response.headers["location"]
    assert "API" not in response.headers["location"]


def test_structuring_page_renders_ai_loading_contract_and_error_feedback():
    app = create_app_with_fake_ai_settings()
    job_id = _create_ai_ready_job(app)

    response = TestClient(app).get(
        f"/jobs/{job_id}/structure?ai_feedback=error&ai_message=AI%20暂时不可用"
    )

    assert response.status_code == 200
    assert 'data-ai-prefill-form' in response.text
    assert 'data-ai-prefill-button' in response.text
    assert "AI 正在读取公告" in response.text
    assert "AI 暂时不可用" in response.text


def test_jobs_page_shows_batch_notice_confirmation_and_post_creates_audit_log():
    app = create_app("sqlite+pysqlite:///:memory:")
    with app.state.session_factory() as session:
        job = Job(
            fingerprint="批量分类页面|公告|2026-08-31|上海|公告",
            employer_name="待人工核验（测试单位）",
            job_title="测试单位2027届校园招聘公告",
            job_family="待分类",
            recruitment_type="待核验",
            location_category="地区待定",
            location_detail="以公告原文为准",
            target_audience="待人工判断",
            direction_tags="待人工分类",
            deadline="原文待人工确认",
            official_url="",
            source_url="https://example.com/source",
            evidence_text="现公开招聘实习生。",
            quality_score=0,
            risk_flags="真实线索：尚未人工核验，不得对外发布",
            is_demo=False,
            status="待核验",
            notice_type="待判断",
        )
        session.add(job)
        session.commit()
        job_id = job.id

    client = TestClient(app)
    page = client.get("/jobs")
    response = client.post("/jobs/classification/confirm-suggestions", follow_redirects=False)

    assert "批量确认 1 条系统建议的新招聘" in page.text
    assert response.status_code == 303
    assert "classification_feedback=" in response.headers["location"]
    with app.state.session_factory() as session:
        assert session.get(Job, job_id).notice_type == "新招聘"
        assert session.query(ReviewLog).filter_by(job_id=job_id, action="批量分类：新招聘").count() == 1


def test_job_detail_shows_publication_readiness_facts():
    app = create_app("sqlite+pysqlite:///:memory:")
    with app.state.session_factory() as session:
        job = session.get(Job, 1)
        job.posting_scope = "multi_role_announcement"
        job.attachment_status = "checked"
        job.application_method = "email"
        job.application_contact = "apply@example.edu.cn"
        session.commit()

    response = TestClient(app).get("/jobs/1")

    assert response.status_code == 200
    assert "公告范围" in response.text
    assert "多岗位招聘公告" in response.text
    assert "附件核验" in response.text
    assert "已核验" in response.text
    assert "投递方式" in response.text
    assert "邮件投递" in response.text
    assert "apply@example.edu.cn" in response.text


def test_job_detail_shows_saved_official_attachments():
    app = create_app("sqlite+pysqlite:///:memory:")
    with app.state.session_factory() as session:
        job = session.get(Job, 1)
        job.attachment_links = '[{"name":"岗位说明.xlsx","url":"https://example.com/roles.xlsx"}]'
        session.commit()

    response = TestClient(app).get("/jobs/1")

    assert response.status_code == 200
    assert "官方附件（待人工核验）" in response.text
    assert "岗位说明.xlsx" in response.text
    assert "https://example.com/roles.xlsx" in response.text
