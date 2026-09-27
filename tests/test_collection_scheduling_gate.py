from contextlib import nullcontext
import builtins
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
from types import ModuleType

import pytest

from app.models import Source
from app.services.real_collection import DailyCollectionResult
from app.services.source_library import can_auto_collect, summarize_source_library
from app.sources.catalog import OFFICIAL_SOURCE_CATALOG, ensure_official_source_catalog


TRIAL_STATUS = "A类试运行（自动采集，人工核验）"


def make_source(**overrides):
    values = {
        "name": "定时资格测试来源",
        "url": "https://example.com/jobs",
        "level": "一级",
        "source_type": "高校就业平台",
        "library_tier": "A",
        "is_enabled": True,
        "status": "正常",
        "adaptation_status": "已自动采集",
    }
    return Source(**(values | overrides))


@pytest.mark.parametrize(
    ("tier", "enabled", "adaptation_status", "expected"),
    [
        ("A", True, "已自动采集", True),
        ("A", True, TRIAL_STATUS, True),
        ("A", False, TRIAL_STATUS, False),
        ("B", True, TRIAL_STATUS, False),
        ("B", True, "已自动采集", False),
        ("A", True, "待专用适配", False),
        ("A", True, "A类试运行（待人工验收）", False),
    ],
)
def test_auto_collection_gate_accepts_only_enabled_a_automatic_states(
    tier, enabled, adaptation_status, expected
):
    source = make_source(
        library_tier=tier,
        is_enabled=enabled,
        adaptation_status=adaptation_status,
    )

    assert can_auto_collect(source) is expected


def test_schedulable_summary_uses_automatic_gate_and_excludes_paused_sources():
    sources = [
        make_source(),
        make_source(adaptation_status=TRIAL_STATUS),
        make_source(adaptation_status=TRIAL_STATUS, status="暂停"),
        make_source(adaptation_status=TRIAL_STATUS, is_enabled=False),
        make_source(library_tier="B"),
        make_source(adaptation_status="待专用适配"),
        make_source(name="测试来源（演示）", library_tier="D"),
    ]

    summary = summarize_source_library(sources, OFFICIAL_SOURCE_CATALOG)

    assert summary.schedulable_sources == 2
    assert summary.demo_sources == 1


def test_new_sbs_source_receives_trial_defaults(session):
    ensure_official_source_catalog(session)
    source = session.query(Source).filter_by(source_key="sbs-jobs").one()

    assert source.library_tier == "A"
    assert source.is_enabled is True
    assert source.adaptation_status == TRIAL_STATUS
    assert source.validation_state == "A类试运行"
    assert source.check_frequency_hours == 8


@pytest.mark.parametrize(
    ("tier", "enabled", "status", "adaptation_status"),
    [
        ("A", False, "正常", TRIAL_STATUS),
        ("A", True, "暂停", TRIAL_STATUS),
        ("B", False, "正常", "待专用适配"),
    ],
)
def test_repeated_catalog_sync_preserves_sbs_operator_settings(
    session, tier, enabled, status, adaptation_status
):
    ensure_official_source_catalog(session)
    source = session.query(Source).filter_by(source_key="sbs-jobs").one()
    operator_settings = {
        "library_tier": tier,
        "is_enabled": enabled,
        "status": status,
        "adaptation_status": adaptation_status,
        "check_frequency_hours": 24,
        "validation_state": "人工复查中",
        "next_action": "按人工调整后的频率观察",
        "pause_reason": "人工观察中",
    }
    for field, value in operator_settings.items():
        setattr(source, field, value)
    session.commit()

    ensure_official_source_catalog(session)
    ensure_official_source_catalog(session)
    session.refresh(source)

    assert {field: getattr(source, field) for field in operator_settings} == operator_settings


def test_scheduled_command_import_does_not_initialize_web_app(monkeypatch):
    original_import = builtins.__import__

    def import_without_web_app(name, *args, **kwargs):
        assert name != "app.main", "Scheduled entrypoint must not initialize the web app"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_web_app)
    monkeypatch.setattr(sys, "path", list(sys.path))
    project_root = Path(__file__).resolve().parents[1]
    spec = spec_from_file_location(
        "scheduling_import_under_test", project_root / "scripts" / "run_due_collection.py"
    )
    command = module_from_spec(spec)

    spec.loader.exec_module(command)

    database_path = project_root / "data" / "recruiting_local.db"
    assert command.DEFAULT_DATABASE_URL == f"sqlite:///{database_path.as_posix()}"


@pytest.mark.parametrize(
    ("attempted", "successful", "expected_exit_code"),
    [(0, 0, 0), (2, 0, 1), (2, 1, 0)],
)
def test_scheduled_command_fails_only_when_attempted_sources_all_fail(
    monkeypatch, capsys, attempted, successful, expected_exit_code
):
    # Isolate any accidental future web-app import from the real local DB.
    app_main = ModuleType("app.main")
    app_main.DEFAULT_DATABASE_URL = "sqlite+pysqlite:///:memory:"
    monkeypatch.setitem(sys.modules, "app.main", app_main)
    monkeypatch.setattr(sys, "path", list(sys.path))
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_due_collection.py"
    spec = spec_from_file_location("scheduling_entrypoint_under_test", script_path)
    command = module_from_spec(spec)
    spec.loader.exec_module(command)
    monkeypatch.setattr(command, "create_database", lambda _: lambda: nullcontext(object()))
    monkeypatch.setattr(command.httpx, "Client", lambda **_: nullcontext(object()))
    result = DailyCollectionResult(
        attempted_sources=attempted,
        successful_sources=successful,
        skipped_sources=8 - attempted,
        created_jobs=0,
        updated_jobs=0,
        unchanged_jobs=0,
        failed_jobs=attempted - successful,
    )
    monkeypatch.setattr(command, "collect_due_sources", lambda *_, **__: result)

    assert command.main() == expected_exit_code
    assert f"尝试 {attempted}，成功 {successful}" in capsys.readouterr().out
