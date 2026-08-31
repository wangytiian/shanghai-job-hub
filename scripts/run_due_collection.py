"""Scheduled entrypoint: collect only enabled public sources that are due."""

import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.database import create_database
from app.main import DEFAULT_DATABASE_URL
from app.services.real_collection import collect_due_sources


def main() -> int:
    session_factory = create_database(DEFAULT_DATABASE_URL)
    with session_factory() as session, httpx.Client(follow_redirects=True) as client:
        result = collect_due_sources(session, client, force=False)
    print(
        f"每日采集：尝试 {result.attempted_sources}，成功 {result.successful_sources}，"
        f"跳过 {result.skipped_sources}；新增 {result.created_jobs}，"
        f"更新 {result.updated_jobs}，无变化 {result.unchanged_jobs}。"
    )
    return 0 if result.successful_sources else 1


if __name__ == "__main__":
    raise SystemExit(main())
