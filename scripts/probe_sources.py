"""Conservative command-line diagnostics for public recruitment source entries."""

import argparse
from datetime import datetime
import sys
from pathlib import Path

import httpx
from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.database import create_database
from app.main import DEFAULT_DATABASE_URL
from app.models import Source
from app.services.source_diagnostics import SourceDiagnosticResult, diagnose_source, save_diagnostic
from app.sources.boc import fetch_boc_detail, fetch_boc_listings


def _sample_boc(source: Source, client, result: SourceDiagnosticResult, limit: int) -> SourceDiagnosticResult:
    if result.connection_status != "ok":
        return result
    try:
        listings = fetch_boc_listings(client, limit=limit)
        details = [fetch_boc_detail(client, listing) for listing in listings]
    except Exception as exc:
        return SourceDiagnosticResult(
            result.connection_status, "dynamic_or_unverified", result.adapter_status, result.http_status,
            result.final_url, 0, 0, f"已找到招聘入口，但试采失败：{str(exc)[:160]}", "sample_failed"
        )
    if not details:
        return SourceDiagnosticResult(
            result.connection_status, "no_openings", result.adapter_status, result.http_status,
            result.final_url, 0, 0, "招聘列表未发现可验证公告，尚不可自动采集。", "no_openings"
        )
    return SourceDiagnosticResult(
        result.connection_status, "detail_verified", "sample_passed", result.http_status,
        result.final_url, len(listings), len(details), f"已验证 {len(details)} 篇公告详情；仍需隔离库去重试采后才能启用。"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="检查招聘来源入口，不写入岗位数据。")
    parser.add_argument("--tier", choices=("A", "B", "C", "D"))
    parser.add_argument("--adapter")
    parser.add_argument("--depth", choices=("connection", "sample"), default="connection")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--persist-diagnostics", action="store_true")
    args = parser.parse_args()
    if args.depth == "sample" and args.adapter != "boc_announcements":
        parser.error("当前仅支持 --adapter boc_announcements 的详情试采")
    if not 1 <= args.limit <= 3:
        parser.error("--limit 必须在 1 到 3 之间")

    session_factory = create_database(DEFAULT_DATABASE_URL)
    with session_factory() as session, httpx.Client(follow_redirects=True, max_redirects=3) as client:
        statement = select(Source).order_by(Source.id)
        if args.tier:
            statement = statement.where(Source.library_tier == args.tier)
        if args.adapter:
            statement = statement.where(Source.adapter_key == args.adapter)
        sources = session.scalars(statement).all()
        if not sources:
            print("未找到匹配来源")
            return 1
        for source in sources:
            checked_at = datetime.now()
            result = diagnose_source(source, client, checked_at, depth=args.depth)
            if args.depth == "sample" and source.adapter_key == "boc_announcements":
                result = _sample_boc(source, client, result, args.limit)
            if args.persist_diagnostics:
                save_diagnostic(session, source, result, checked_at, depth=args.depth)
                session.commit()
            print(
                f"{source.id}\t{source.name}\t连接={result.connection_status}\t"
                f"内容={result.content_status}\t适配={result.adapter_status}\t{result.message}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
