"""Read-only reporting tests use isolated SQLite files, never the live database."""

import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "report_collection_yield.py"


@pytest.fixture
def report_db(tmp_path):
    path = tmp_path / "招聘统计 #1.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE,
                created_at TEXT, collected_at TEXT, published_at TEXT,
                source_url TEXT, is_demo INTEGER, status TEXT,
                lifecycle_status TEXT, deadline TEXT, evidence_text TEXT,
                intake_grade TEXT, intake_route TEXT, posting_scope TEXT
            );
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY, task_name TEXT, status TEXT,
                message TEXT, created_at TEXT
            );
            """
        )
    return path


def add_job(path, **overrides):
    values = {
        "created_at": "2026-09-24 12:30:00",
        "collected_at": "2026-09-26 12:30:00",
        "published_at": "2026-09-24 00:00:00",
        "source_url": "https://jobs.example.org/notice/1",
        "is_demo": 0,
        "status": "待核验",
        "lifecycle_status": "正常",
        "deadline": "公告未明确统一截止时间",
        "evidence_text": "学生岗位招聘。",
        "intake_grade": "A",
        "intake_route": "优先待核验",
        "posting_scope": "single_role",
        **overrides,
    }
    with sqlite3.connect(path) as connection:
        values["fingerprint"] = f"unique-{connection.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]}"
        connection.execute(
            f"INSERT INTO jobs ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
            tuple(values.values()),
        )


def run_report(path, *args):
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPT), "--db", str(path), "--as-of", "2026-09-26", *args],
        cwd=path.parent,
        capture_output=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_complete_days_use_first_created_time_and_include_zero_days(report_db):
    add_job(report_db, created_at="2026-09-19 00:00:00")
    add_job(report_db, created_at="2026-09-25 23:59:59.999999")
    add_job(report_db, created_at="2026-09-18 23:59:59", collected_at="2026-09-24 10:00:00")
    add_job(report_db, created_at="2026-09-26 00:00:00")

    report = run_report(report_db)

    assert report["window"]["start_date"] == "2026-09-19"
    assert report["window"]["end_date"] == "2026-09-25"
    assert report["metrics"]["new_real_jobs"] == {
        "total": 2, "daily_average": round(2 / 7, 4), "active_days": 2,
    }
    assert len(report["daily"]) == 7
    assert [row["new_real_jobs"] for row in report["daily"]] == [1, 0, 0, 0, 0, 0, 1]


def test_aware_times_convert_to_beijing_but_naive_times_are_already_local(report_db):
    add_job(report_db, created_at="2026-09-18T16:00:00+00:00")
    add_job(report_db, created_at="2026-09-25T16:00:00Z")
    add_job(report_db, created_at="2026-09-25 23:30:00")

    report = run_report(report_db)

    assert report["daily"][0]["new_real_jobs"] == 1
    assert report["daily"][-1]["new_real_jobs"] == 1
    assert report["metrics"]["new_real_jobs"]["total"] == 2
    assert report["window"]["timezone"] == "Asia/Shanghai (UTC+08:00)"


def test_excludes_demo_archived_rejected_and_expired_without_writing(report_db):
    add_job(report_db, is_demo=1)
    add_job(report_db, status="已归档")
    add_job(report_db, status="仅归档")
    add_job(report_db, lifecycle_status="已截止")
    add_job(report_db, status="淘汰")
    add_job(report_db, status="已过期")
    add_job(report_db, deadline="2026-09-25")
    add_job(report_db, evidence_text="报名时间：2026年9月1日至2026年9月25日。")
    add_job(report_db, deadline="2026-09-26")
    digest_before = hashlib.sha256(report_db.read_bytes()).hexdigest()

    report = run_report(report_db)

    assert report["metrics"]["new_real_jobs"]["total"] == 1
    assert report["excluded"] == {"demo": 1, "inactive_status": 5, "expired_deadline": 2}
    assert hashlib.sha256(report_db.read_bytes()).hexdigest() == digest_before


def test_separates_recent_publication_backfill_unknown_dates_and_screening(report_db):
    add_job(report_db)
    add_job(report_db, intake_grade="B", published_at="2025-09-24")
    add_job(report_db, intake_grade="C", published_at=None)
    add_job(report_db, intake_grade="D", intake_route="过滤留档", published_at="unknown")
    add_job(report_db, published_at="2026-09-26")
    add_job(report_db, intake_grade="C", published_at="2026-09-18")

    report = run_report(report_db)

    assert report["metrics"]["new_real_jobs"]["total"] == 6
    assert report["metrics"]["screened_review_candidates"]["total"] == 3
    assert report["metrics"]["undecided_review_jobs"]["total"] == 2
    assert report["metrics"]["filtered_jobs"]["total"] == 1
    assert report["metrics"]["recently_published_new_jobs"]["total"] == 1
    assert report["metrics"]["recently_published_review_candidates"]["total"] == 1
    assert report["publication_dates"] == {
        "within_window": 1, "before_window": 2, "on_or_after_as_of": 1,
        "missing": 1, "invalid": 1,
    }
    assert report["manual_publishability"] == "未核实人工可发布量；待核验、待审核和 A/B 初筛均不等于可发布。"


def test_filtered_routes_and_non_job_notices_do_not_count_as_review_candidates(report_db):
    add_job(report_db, intake_route="过滤留档")
    add_job(report_db, posting_scope="non_job_notice")
    add_job(report_db, intake_grade="", intake_route="人工复核")

    report = run_report(report_db)

    assert report["metrics"]["screened_review_candidates"]["total"] == 0
    assert report["metrics"]["filtered_jobs"]["total"] == 2
    assert report["metrics"]["undecided_review_jobs"]["total"] == 1


def test_missing_or_invalid_created_dates_are_separate_and_never_inferred(report_db):
    add_job(report_db, created_at=None)
    add_job(report_db, created_at="not-a-date")
    add_job(report_db)

    report = run_report(report_db, "--days", "14")

    assert report["date_quality"] == {"missing_created_at": 1, "invalid_created_at": 1}
    assert report["metrics"]["new_real_jobs"]["total"] == 1
    assert len(report["daily"]) == 14
    assert report["window"]["days"] == 14


def test_source_breakdown_uses_url_hostname_without_source_id(report_db):
    add_job(report_db, source_url="https://JOBS.EXAMPLE.ORG/a")
    add_job(report_db, source_url="http://jobs.example.org:8080/b")
    add_job(report_db, source_url="https://another.example.org/a", intake_grade="C")
    add_job(report_db, source_url="not a url")

    report = run_report(report_db)
    sources = {row["source_host"]: row for row in report["sources"]}

    assert sources["jobs.example.org"]["new_real_jobs"] == 2
    assert sources["another.example.org"]["undecided_review_jobs"] == 1
    assert sources["（来源缺失或无效）"]["new_real_jobs"] == 1


def test_latest_collection_logs_retain_partial_failure_messages(report_db):
    with sqlite3.connect(report_db) as connection:
        connection.executemany(
            "INSERT INTO task_runs VALUES (?, ?, ?, ?, ?)",
            [
                (1, "公开采集·来源甲", "失败", "超时", "2026-09-25 08:00:00"),
                (2, "每日多来源采集", "完成", "新增 0 条，失败 11 项。", "2026-09-26 12:00:00"),
                (3, "模拟采集", "完成", "演示数据", "2026-09-26 13:00:00"),
                (4, "资料整理", "完成", "", "2026-09-26 14:00:00"),
            ],
        )

    report = run_report(report_db)

    assert [row["id"] for row in report["latest_collection_runs"]] == [2, 1]
    assert report["latest_collection_runs"][0]["message"] == "新增 0 条，失败 11 项。"


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "does-not-exist.db"
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPT), "--db", str(path)],
        capture_output=True, encoding="utf-8",
    )

    assert result.returncode != 0
    assert "只读" in result.stderr
    assert not path.exists()


@pytest.mark.parametrize("args", [("--days", "0"), ("--days", "-1"), ("--as-of", "2026-02-30")])
def test_invalid_cli_parameters_fail_cleanly(report_db, args):
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPT), "--db", str(report_db), *args],
        capture_output=True, encoding="utf-8",
    )

    assert result.returncode == 2
    assert "error:" in result.stderr
    assert "Traceback" not in result.stderr


def test_connection_is_read_only_and_default_database_is_project_local(report_db):
    assert SCRIPT.exists(), "read-only report entrypoint has not been implemented"
    from scripts.report_collection_yield import DEFAULT_DATABASE, connect_read_only

    assert DEFAULT_DATABASE == PROJECT_ROOT / "data" / "recruiting_local.db"
    with connect_read_only(report_db) as connection:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM jobs")
