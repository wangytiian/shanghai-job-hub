"""Web contract for explicit B-trial import and evidence-backed C-to-B review."""

from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import select

from app.auth.service import create_user
from app.config import Settings
from app.main import create_app
from app.models import Job, Source, SourceTrialRun, SourceTrialSample
from app.services.real_collection import REAL_RISK_FLAG
from app.services.review_intake import REVIEW_NOTE
from app.services.source_candidate_policy import RULE_VERSION


PASSWORD = "A secure password"


def _app(*, cloud=False):
    settings = (
        Settings(app_env="cloud", database_url="postgresql://example.invalid/test")
        if cloud
        else Settings(app_env="test", database_url="sqlite+pysqlite:///:memory:")
    )
    return create_app("sqlite+pysqlite:///:memory:", settings=settings)


def _add_trial(app, *, source_key="sjtu-internships", state="completed", include_skipped=False):
    current = datetime.now().replace(microsecond=0)
    with app.state.session_factory() as session:
        source = session.scalar(select(Source).where(Source.source_key == source_key))
        assert source is not None
        assert source.library_tier == "B" and source.is_enabled is False
        source_host = {
            "sjtu-internships": "www.job.sjtu.edu.cn",
            "sufe-jobs": "career.sufe.edu.cn",
            "ncss-shanghai": "www.ncss.cn",
        }[source_key]
        run_id = f"pilot-web-{source_key}-{state}"
        run = SourceTrialRun(
            run_id=run_id,
            source_id=source.id,
            adapter_version=source.adapter_version,
            rule_version=RULE_VERSION,
            started_at=current - timedelta(minutes=5),
            finished_at=None if state == "running" else current,
            state=state,
            list_count=2 if include_skipped else 1,
            detail_attempt_count=2 if include_skipped else 1,
            detail_success_count=2 if include_skipped else 1,
            candidate_count=1,
            requested_by="test operator",
            idempotency_key=run_id,
        )
        session.add(run)
        session.flush()
        body = (
            "岗位职责：协助整理财务数据。任职要求：在校生，工作地点上海。"
            "官方投递邮箱：apply@example.edu"
        )
        session.add(
            SourceTrialSample(
                run_id=run_id,
                identity_key="notice-1:role-1",
                announcement_id="notice-1",
                job_id="role-1",
                employer_name="上海示例科技有限公司",
                title="财务实习生",
                location_category="明确上海",
                location_detail="上海市杨浦区",
                published_at=current.date().isoformat(),
                deadline=(current.date() + timedelta(days=90)).isoformat(),
                evidence_text=body,
                source_url=f"https://{source_host}/jobs/notice-1",
                application_kind="email",
                application_value="apply@example.edu",
                application_evidence="官方投递邮箱：apply@example.edu",
                decision="qualified",
                reason_codes="[]",
                content_hash=sha256(" ".join(body.split()).encode("utf-8")).hexdigest(),
            )
        )
        if include_skipped:
            missing_application = "岗位职责：协助财务分析，工作地点上海。"
            session.add(
                SourceTrialSample(
                    run_id=run_id,
                    identity_key="notice-2:role-2",
                    announcement_id="notice-2",
                    job_id="role-2",
                    employer_name="上海示例科技有限公司",
                    title="研究助理",
                    location_category="明确上海",
                    location_detail="上海市杨浦区",
                    published_at=current.date().isoformat(),
                    deadline=(current.date() + timedelta(days=90)).isoformat(),
                    evidence_text=missing_application,
                    source_url=f"https://{source_host}/jobs/notice-2",
                    decision="needs_evidence",
                    reason_codes='["TARGET_AUDIENCE_UNCLEAR", "APPLICATION_MISSING"]',
                    content_hash=sha256(
                        " ".join(missing_application.split()).encode("utf-8")
                    ).hexdigest(),
                )
            )
        session.commit()
        return source.id, run_id


def _add_review_only_job(app):
    current = datetime.now()
    body = (
        "岗位职责：协助整理财务数据，工作地点上海。"
        "官方补充说明：接受2027届本科毕业生申请。"
    )
    fingerprint = sha256(" ".join(f"{body}\n[]".split()).encode("utf-8")).hexdigest()
    with app.state.session_factory() as session:
        job = Job(
            fingerprint="pilot-web-review:role-1",
            employer_name="上海示例科技有限公司",
            announcement_title="财务助理招聘",
            job_title="财务助理",
            job_family="待分类",
            recruitment_type="待核验",
            location_category="明确上海",
            location_detail="上海市杨浦区",
            target_audience="待人工判断",
            direction_tags="待人工分类",
            deadline=(current.date() + timedelta(days=90)).isoformat(),
            official_url="https://career.example.edu/jobs/role-1",
            source_url="https://career.example.edu/jobs/role-1",
            evidence_text=body,
            quality_score=0,
            risk_flags=REAL_RISK_FLAG,
            is_demo=False,
            published_at=current,
            content_fingerprint=fingerprint,
            status="待核验",
            notice_type="新招聘",
            intake_grade="C",
            intake_route="人工复核",
            intake_reason=REVIEW_NOTE,
        )
        session.add(job)
        session.commit()
        return job.id, job.version, job.content_fingerprint


def _trial_sample_id(app, run_id, *, decision="qualified"):
    with app.state.session_factory() as session:
        return session.scalar(
            select(SourceTrialSample.id).where(
                SourceTrialSample.run_id == run_id,
                SourceTrialSample.decision == decision,
            )
        )


def _login(app, username, *, role="member", can_review=False):
    with app.state.session_factory() as session:
        create_user(
            session,
            username,
            PASSWORD,
            username,
            role=role,
            can_review=can_review,
            must_change_password=False,
        )
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/login").status_code == 200
    response = client.post(
        "/login",
        data={
            "username": username,
            "password": PASSWORD,
            "csrf_token": client.cookies.get("recruiting_login_csrf"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


def _promotion_data(version, fingerprint, **overrides):
    data = {
        "expected_version": str(version),
        "expected_fingerprint": fingerprint,
        "evidence_url": "https://career.example.edu/jobs/role-1",
        "evidence_excerpt": "接受2027届本科毕业生申请",
        "note": "已核对官方补充说明，确认面向应届本科毕业生开放",
    }
    data.update(overrides)
    return data


def _has_hidden_field(html, name, value):
    return any(
        'type="hidden"' in tag
        and f'name="{name}"' in tag
        and f'value="{value}"' in tag
        for tag in re.findall(r"<input\b[^>]*>", html)
    )


@pytest.mark.parametrize("source_key", ["sjtu-internships", "sufe-jobs"])
def test_trial_report_offers_import_only_for_finished_allowlisted_round(source_key):
    app = _app()
    source_id, run_id = _add_trial(app, source_key=source_key)
    _add_trial(app, source_key=source_key, state="running")

    page = TestClient(app).get(f"/sources/{source_id}/trials")

    assert page.status_code == 200
    assert f'action="/sources/{source_id}/trials/{run_id}/import"' in page.text
    assert _has_hidden_field(page.text, "sample_id", _trial_sample_id(app, run_id))
    assert f'action="/sources/{source_id}/trials/pilot-web-{source_key}-running/import"' not in page.text
    assert "导入待核验" in page.text


def test_rejected_trial_only_offers_invalidation_when_old_job_exists():
    app = _app()
    source_id, run_id = _add_trial(app, include_skipped=True)
    rejected_id = _trial_sample_id(app, run_id, decision="needs_evidence")

    no_old_job = TestClient(app).get(f"/sources/{source_id}/trials")
    assert not _has_hidden_field(no_old_job.text, "sample_id", rejected_id)

    job_id, _, _ = _add_review_only_job(app)
    with app.state.session_factory() as session:
        source = session.get(Source, source_id)
        job = session.get(Job, job_id)
        job.fingerprint = f"{source.name}|notice-2:role-2"
        session.commit()

    page = TestClient(app).get(f"/sources/{source_id}/trials")

    assert page.status_code == 200
    assert _has_hidden_field(page.text, "sample_id", rejected_id)
    assert "核查并撤销旧核验" in page.text
    assert "查看来源列表（非岗位直链）" in page.text


def test_other_b_source_has_no_import_button_and_cannot_import_by_direct_post():
    app = _app()
    source_id, run_id = _add_trial(app, source_key="ncss-shanghai")
    # NCSS is now explicitly authorised; an unknown source still cannot import.
    with app.state.session_factory() as session:
        session.get(Source, source_id).source_key = "unlisted-public-source"
        session.commit()
    client = TestClient(app)
    with app.state.session_factory() as session:
        before = session.query(Job).count()

    page = client.get(f"/sources/{source_id}/trials")
    response = client.post(
        f"/sources/{source_id}/trials/{run_id}/import",
        data={"sample_id": _trial_sample_id(app, run_id)},
        follow_redirects=True,
    )

    assert page.status_code == 200
    assert f'action="/sources/{source_id}/trials/{run_id}/import"' not in page.text
    assert response.status_code in {200, 400, 403}
    with app.state.session_factory() as session:
        assert session.query(Job).count() == before


def test_local_import_post_creates_pending_job_and_reports_created_and_skipped_counts():
    app = _app()
    source_id, run_id = _add_trial(app, include_skipped=True)
    with app.state.session_factory() as session:
        before = session.query(Job).count()

    response = TestClient(app).post(
        f"/sources/{source_id}/trials/{run_id}/import",
        data={"sample_id": _trial_sample_id(app, run_id)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "新增 1" in response.text
    assert "跳过 0" in response.text
    with app.state.session_factory() as session:
        jobs = session.scalars(select(Job).where(Job.is_demo.is_(False))).all()
        source = session.get(Source, source_id)
        assert session.query(Job).count() == before + 1
        assert len(jobs) == 1
        assert jobs[0].status == "待核验"
        assert jobs[0].quality_score == 0
        assert REAL_RISK_FLAG in jobs[0].risk_flags
        assert (source.library_tier, source.is_enabled) == ("B", False)


def test_imported_pilot_job_labels_its_listing_link_honestly():
    app = _app()
    source_id, run_id = _add_trial(app)
    client = TestClient(app)
    client.post(
        f"/sources/{source_id}/trials/{run_id}/import",
        data={"sample_id": _trial_sample_id(app, run_id)},
    )
    with app.state.session_factory() as session:
        job_id = session.scalar(select(Job.id).where(Job.is_demo.is_(False)))

    page = client.get(f"/jobs/{job_id}")

    assert "查看来源列表" in page.text
    assert "不是单条岗位直链" in page.text


def test_import_without_explicit_sample_selection_creates_no_job():
    app = _app()
    source_id, run_id = _add_trial(app)

    response = TestClient(app).post(f"/sources/{source_id}/trials/{run_id}/import")

    assert response.status_code == 422
    with app.state.session_factory() as session:
        assert session.query(Job).filter_by(is_demo=False).count() == 0


def test_unknown_trial_run_reports_error_and_creates_no_job():
    app = _app()
    source_id, _ = _add_trial(app)
    with app.state.session_factory() as session:
        before = session.query(Job).count()

    response = TestClient(app).post(
        f"/sources/{source_id}/trials/unknown-run/import",
        data={"sample_id": _trial_sample_id(app, "pilot-web-sjtu-internships-completed")},
        follow_redirects=True,
    )

    assert response.status_code in {200, 400}
    assert "不存在" in response.text
    with app.state.session_factory() as session:
        assert session.query(Job).count() == before


def test_review_only_job_shows_evidence_form_with_current_version_and_fingerprint():
    app = _app()
    job_id, version, fingerprint = _add_review_only_job(app)

    page = TestClient(app).get(f"/jobs/{job_id}")

    assert page.status_code == 200
    assert f'action="/jobs/{job_id}/intake-promotion"' in page.text
    assert _has_hidden_field(page.text, "expected_version", version)
    assert _has_hidden_field(page.text, "expected_fingerprint", fingerprint)
    for field in ("evidence_url", "evidence_excerpt", "note"):
        assert f'name="{field}"' in page.text


def test_unconfirmed_b_job_shows_confirmation_form_and_remains_pending_after_post():
    app = _app()
    job_id, version, fingerprint = _add_review_only_job(app)
    with app.state.session_factory() as session:
        job = session.get(Job, job_id)
        job.intake_grade = "B"
        job.intake_route = "普通待核验"
        session.commit()

    client = TestClient(app)
    page = client.get(f"/jobs/{job_id}")
    assert page.status_code == 200
    assert f'action="/jobs/{job_id}/intake-promotion"' in page.text
    assert "确认受众" in page.text

    response = client.post(
        f"/jobs/{job_id}/intake-promotion",
        data=_promotion_data(version, fingerprint),
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "受众证据已确认" in response.text
    with app.state.session_factory() as session:
        job = session.get(Job, job_id)
        assert (job.intake_grade, job.status) == ("B", "待核验")
        assert job.intake_reason != REVIEW_NOTE


def test_unconfirmed_b_detail_template_renders_confirmation_form_without_http():
    app = _app()
    job_id, _, _ = _add_review_only_job(app)
    with app.state.session_factory() as session:
        job = session.get(Job, job_id)
        job.intake_grade = "B"
        job.intake_route = "普通待核验"
        session.flush()
        html = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "app" / "templates")).get_template(
            "job_detail.html"
        ).render(
            job=job,
            review_note=REVIEW_NOTE,
            can_promote_intake=True,
            global_status=SimpleNamespace(pending_work_count=0, unhealthy_source_count=0,
                                          last_task_at=None, operator_name="测试"),
            cloud_runtime=False,
            attachments=[],
            child_jobs=[],
            parent_job=None,
        )
    assert f'action="/jobs/{job_id}/intake-promotion"' in html
    assert "提交证据并确认受众" in html


def test_promotion_form_is_hidden_for_d_grade_or_unrelated_c_reason():
    app = _app()
    job_id, _, _ = _add_review_only_job(app)
    client = TestClient(app)
    with app.state.session_factory() as session:
        job = session.get(Job, job_id)
        job.intake_grade = "D"
        session.commit()
    d_page = client.get(f"/jobs/{job_id}")
    with app.state.session_factory() as session:
        job = session.get(Job, job_id)
        job.intake_grade = "C"
        job.intake_reason = "缺少投递方式"
        session.commit()
    unrelated_c_page = client.get(f"/jobs/{job_id}")

    action = f'action="/jobs/{job_id}/intake-promotion"'
    assert action not in d_page.text
    assert action not in unrelated_c_page.text


def test_local_evidence_post_promotes_only_to_b_and_keeps_final_review_pending():
    app = _app()
    job_id, version, fingerprint = _add_review_only_job(app)

    response = TestClient(app).post(
        f"/jobs/{job_id}/intake-promotion",
        data=_promotion_data(version, fingerprint),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "操作成功" in response.text
    assert "B｜普通待核验" in response.text
    with app.state.session_factory() as session:
        job = session.get(Job, job_id)
        assert (job.intake_grade, job.status) == ("B", "待核验")
        assert job.quality_score == 0
        assert job.version > version


def test_invalid_promotion_evidence_shows_feedback_without_changing_grade():
    app = _app()
    job_id, version, fingerprint = _add_review_only_job(app)

    response = TestClient(app).post(
        f"/jobs/{job_id}/intake-promotion",
        data=_promotion_data(version, fingerprint, evidence_url="javascript:alert(1)"),
        follow_redirects=True,
    )

    assert response.status_code in {200, 400}
    assert "需要补充信息" in response.text
    assert "HTTP(S)" in response.text
    with app.state.session_factory() as session:
        assert session.get(Job, job_id).intake_grade == "C"


def test_member_direct_posts_are_forbidden_even_if_buttons_are_hidden():
    app = _app(cloud=True)
    source_id, run_id = _add_trial(app)
    job_id, version, fingerprint = _add_review_only_job(app)
    client = _login(app, "member")
    csrf = {"X-CSRF-Token": client.cookies.get("recruiting_csrf")}

    assert client.post(
        f"/sources/{source_id}/trials/{run_id}/import",
        data={"sample_id": _trial_sample_id(app, run_id)},
        headers=csrf,
    ).status_code == 403
    assert client.post(
        f"/jobs/{job_id}/intake-promotion",
        data=_promotion_data(version, fingerprint),
        headers=csrf,
    ).status_code == 403
    assert f'action="/sources/{source_id}/trials/{run_id}/import"' not in client.get(
        f"/sources/{source_id}/trials"
    ).text
    assert f'action="/jobs/{job_id}/intake-promotion"' not in client.get(
        f"/jobs/{job_id}"
    ).text


def test_reviewer_can_promote_with_csrf_but_cannot_import_trial():
    app = _app(cloud=True)
    source_id, run_id = _add_trial(app)
    job_id, version, fingerprint = _add_review_only_job(app)
    client = _login(app, "reviewer", can_review=True)
    csrf = {"X-CSRF-Token": client.cookies.get("recruiting_csrf")}

    assert client.post(
        f"/sources/{source_id}/trials/{run_id}/import",
        data={"sample_id": _trial_sample_id(app, run_id)},
        headers=csrf,
    ).status_code == 403
    assert f'action="/jobs/{job_id}/intake-promotion"' in client.get(f"/jobs/{job_id}").text
    response = client.post(
        f"/jobs/{job_id}/intake-promotion",
        data=_promotion_data(version, fingerprint),
        headers=csrf,
        follow_redirects=False,
    )
    assert response.status_code in {200, 303}
    with app.state.session_factory() as session:
        assert session.get(Job, job_id).intake_grade == "B"


def test_cloud_admin_needs_csrf_for_import_and_promotion():
    app = _app(cloud=True)
    source_id, run_id = _add_trial(app)
    job_id, version, fingerprint = _add_review_only_job(app)
    client = _login(app, "admin", role="admin")

    assert client.post(f"/sources/{source_id}/trials/{run_id}/import").status_code == 403
    assert client.post(
        f"/jobs/{job_id}/intake-promotion", data=_promotion_data(version, fingerprint)
    ).status_code == 403
    with app.state.session_factory() as session:
        assert session.query(Job).filter_by(is_demo=False).count() == 1
        assert session.get(Job, job_id).intake_grade == "C"


def test_cloud_admin_can_import_with_csrf():
    app = _app(cloud=True)
    source_id, run_id = _add_trial(app)
    client = _login(app, "admin", role="admin")

    page = client.get(f"/sources/{source_id}/trials")
    assert f'action="/sources/{source_id}/trials/{run_id}/import"' in page.text
    response = client.post(
        f"/sources/{source_id}/trials/{run_id}/import",
        data={"sample_id": _trial_sample_id(app, run_id)},
        headers={"X-CSRF-Token": client.cookies.get("recruiting_csrf")},
        follow_redirects=False,
    )
    assert response.status_code in {200, 303}
    with app.state.session_factory() as session:
        assert session.query(Job).filter_by(is_demo=False).count() == 1


def test_cloud_admin_can_promote_with_csrf():
    app = _app(cloud=True)
    job_id, version, fingerprint = _add_review_only_job(app)
    client = _login(app, "admin", role="admin")

    response = client.post(
        f"/jobs/{job_id}/intake-promotion",
        data=_promotion_data(version, fingerprint),
        headers={"X-CSRF-Token": client.cookies.get("recruiting_csrf")},
        follow_redirects=False,
    )

    assert response.status_code in {200, 303}
    with app.state.session_factory() as session:
        job = session.get(Job, job_id)
        assert (job.intake_grade, job.status) == ("B", "待核验")
