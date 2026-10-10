"""Bounded public-source trials and explicit sample import into the local DB.

No scheduler, automatic approval, remote DB, or authentication is involved.
"""
import argparse
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from sqlalchemy import select

from app.database import create_session_factory
from app.models import Job, Source, SourceTrialRun, SourceTrialSample
from app.services.b_pilot_intake import import_trial_run
from app.services.source_trials import TrialBudget, run_source_trial
from app.sources.catalog import OFFICIAL_SOURCE_CATALOG
from app.sources.shanghai_sasac import USER_AGENT

KEYS = ("ncss-shanghai", "sues-internships", "sit-internships", "shcpa-jobs")


def backup_database(database: Path, output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    backup = output / f"before-{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:8]}.db"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(backup) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("备份完整性检查失败；不执行写入")
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=(*KEYS, "all"), default="all")
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--details", type=int, default=10)
    parser.add_argument("--import-run")
    parser.add_argument("--samples", type=int, nargs="+")
    args = parser.parse_args()
    database = (ROOT / "data/recruiting_local.db").resolve()
    if not database.is_file() or database.parent != ROOT / "data":
        raise RuntimeError("仅支持现有本地数据库")
    output = ROOT / "outputs/four-source-20261010"
    backup = backup_database(database, ROOT / "backups/four-source-20261010")
    factory = create_session_factory(f"sqlite:///{database.as_posix()}")
    print(json.dumps({"backup": str(backup), "integrity": "ok"}, ensure_ascii=False), flush=True)
    if args.import_run:
        if args.source == "all" or not args.samples:
            raise ValueError("导入必须指定单一来源、轮次和明确样本 ID")
        with factory() as session:
            source = session.scalar(select(Source).where(Source.source_key == args.source))
            result = import_trial_run(session, source.id, args.import_run, None,
                                      sample_ids=tuple(args.samples))
            jobs = session.scalars(select(Job).where(Job.fingerprint.startswith(source.name + "|"))).all()
            data = {"source": args.source, "run_id": args.import_run, "samples": args.samples,
                    "result": asdict(result), "job_ids": [job.id for job in jobs]}
            output.mkdir(parents=True, exist_ok=True)
            (output / f"{args.source}-import-{datetime.now():%H%M%S}.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(data, ensure_ascii=False), flush=True)
        return
    budget = TrialBudget(pages=args.pages, list_limit=30, detail_limit=args.details)
    keys = KEYS if args.source == "all" else (args.source,)
    for key in keys:
        definition = next(item for item in OFFICIAL_SOURCE_CATALOG if item.source_key == key)
        with factory() as session:
            source = session.scalar(select(Source).where(Source.source_key == key))
            if source is None:
                source = Source(**(definition.__dict__ | {"official_career_url": definition.url,
                    "status": "正常", "check_frequency_hours": 8, "adapter_version": "public-v1"}))
                session.add(source)
                session.commit()
            if key == "ncss-shanghai" and source.adapter_version != "ncss-maincard-v2":
                source.adapter_version = "ncss-maincard-v2"
                session.commit()
            source_id = source.id
        error = ""
        run_id = None
        task_key = f"four-source-{key}-{uuid4().hex}"
        try:
            with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=False) as client:
                run_id = run_source_trial(factory, client, source_id, "local-approved-expansion",
                                           budget, task_key)
        except Exception as exc:
            error = f"{type(exc).__name__}: {str(exc)[:500]}"
        with factory() as session:
            run = session.scalar(select(SourceTrialRun).where(SourceTrialRun.source_id == source_id,
                                                              SourceTrialRun.idempotency_key == task_key))
            samples = session.scalars(select(SourceTrialSample).where(
                SourceTrialSample.run_id == run.run_id)).all() if run else []
            data = {"source": key, "source_id": source_id, "run_id": run.run_id if run else run_id,
                    "state": run.state if run else "not_started", "error": error,
                    "list_count": run.list_count if run else 0,
                    "detail_attempts": run.detail_attempt_count if run else 0,
                    "detail_successes": run.detail_success_count if run else 0,
                    "samples": [{field: getattr(sample, field) for field in (
                        "id", "identity_key", "title", "employer_name", "published_at", "deadline",
                        "location_detail", "decision", "reason_codes", "application_kind",
                        "application_value", "source_url", "evidence_text")} for sample in samples]}
            output.mkdir(parents=True, exist_ok=True)
            (output / f"{key}-trial.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({k: v for k, v in data.items() if k != "samples"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
