"""Explicit operational commands; cloud application startup never seeds data."""

import argparse
from getpass import getpass
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import select

from app.auth.service import create_user
from app.config import load_settings
from app.database import create_session_factory
from app.migration_tools import migrate_sqlite
from app.models import Source, SourceTrialRun
from app.services.source_trials import (
    TrialBudget,
    is_registered_trial_source,
    run_source_trial,
)
from app.seed import seed_demo_data
from app.sources.catalog import ensure_official_source_catalog


def create_admin(args: argparse.Namespace) -> None:
    password = getpass("管理员密码：")
    session_factory = create_session_factory(args.database_url)
    with session_factory() as session:
        user = create_user(
            session,
            args.username,
            password,
            args.display_name or args.username,
            role="admin",
            must_change_password=False,
        )
    print(f"已创建管理员账号：{user.username}")


def init_data(args: argparse.Namespace) -> None:
    session_factory = create_session_factory(args.database_url)
    with session_factory() as session:
        ensure_official_source_catalog(session)
        if not args.sources_only:
            seed_demo_data(session)
    print("初始化完成")


def import_sqlite(args: argparse.Namespace) -> None:
    session_factory = create_session_factory(args.database_url)
    result = migrate_sqlite(args.source, session_factory, dry_run=args.dry_run)
    mode = "预演完成，未写入目标库" if result.dry_run else "导入完成"
    print(f"{mode}：{result.row_counts}")


def source_trial_command(args: argparse.Namespace) -> None:
    session_factory = create_session_factory(args.database_url)
    with session_factory() as session:
        source = session.scalar(select(Source).where(Source.source_key == args.source_key))
        if source is None or not is_registered_trial_source(source):
            raise ValueError("来源键不存在，或该来源尚无已注册试采适配器")
        source_id = source.id
    budget = TrialBudget(
        pages=args.pages,
        list_limit=args.list_limit,
        detail_limit=args.detail_limit,
        total_seconds=args.total_seconds,
    )
    with httpx.Client(follow_redirects=True, max_redirects=3) as client:
        run_id = run_source_trial(
            session_factory,
            client,
            source_id,
            args.requested_by,
            budget,
            args.idempotency_key or str(uuid4()),
        )
    with session_factory() as session:
        state = session.scalar(
            select(SourceTrialRun.state).where(SourceTrialRun.run_id == run_id)
        )
    print(f"试采结束（{state}），run_id：{run_id}")


def build_parser() -> argparse.ArgumentParser:
    settings = load_settings()
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    parser.add_argument("--database-url", default=settings.database_url)
    subparsers = parser.add_subparsers(dest="command", required=True)
    admin = subparsers.add_parser("create-admin")
    admin.add_argument("--username", required=True)
    admin.add_argument("--display-name", default="")
    admin.set_defaults(handler=create_admin)
    init = subparsers.add_parser("init-data")
    init.add_argument("--sources-only", action="store_true")
    init.set_defaults(handler=init_data)
    importer = subparsers.add_parser("migrate-sqlite")
    importer.add_argument("--source", type=Path, required=True)
    importer.add_argument("--dry-run", action="store_true")
    importer.set_defaults(handler=import_sqlite)
    trial = subparsers.add_parser("source-trial")
    trial.add_argument("--source-key", required=True)
    trial.add_argument("--requested-by", default="cli")
    trial.add_argument("--pages", type=int, choices=range(1, 4), default=1)
    trial.add_argument("--list-limit", type=int, choices=range(1, 31), default=10)
    trial.add_argument("--detail-limit", type=int, choices=range(1, 11), default=3)
    trial.add_argument("--total-seconds", type=int, choices=range(1, 601), default=600)
    trial.add_argument("--idempotency-key", default="")
    trial.set_defaults(handler=source_trial_command)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
