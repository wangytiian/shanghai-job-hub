"""Read-only collection yield report, using complete Beijing calendar days.

Examples (from the project directory):
    .venv/Scripts/python.exe scripts/report_collection_yield.py --days 7
    .venv/Scripts/python.exe scripts/report_collection_yield.py --days 14 --as-of 2026-09-26

The default database is resolved relative to this file, not the working directory.
No application startup, schema migration, collection, or database write is performed.
"""

import argparse
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Reuse only the existing pure deadline parsers, never their write/expiry helpers.
from app.services.deadline_policy import extract_application_deadline, parse_known_deadline


DEFAULT_DATABASE = PROJECT_ROOT / "data" / "recruiting_local.db"
BEIJING = timezone(timedelta(hours=8))
INACTIVE_STATUSES = {"仅归档", "已归档", "归档", "已截止", "已过期", "过期", "淘汰", "已淘汰"}
METRIC_LABELS = {
    "new_real_jobs": "去重新增真实记录（排除演示、归档、淘汰、已截止；含下列初筛类别）",
    "screened_review_candidates": "A/B 初筛审核候选（尚未确认值得审核或可发布）",
    "undecided_review_jobs": "C 级或未分级，仍待人工判断",
    "filtered_jobs": "D 级、过滤留档或非岗位公告，不计审核候选",
    "recently_published_new_jobs": "新增记录中，原公告发布日期也在同一统计窗口内",
    "recently_published_review_candidates": "A/B 初筛候选中，原公告发布日期也在同一统计窗口内",
}


def connect_read_only(path: Path | str) -> sqlite3.Connection:
    """mode=ro fails for missing files; query_only adds connection-level protection."""
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _beijing_day(value: object) -> date | None:
    if value is None or not str(value).strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except (ValueError, TypeError):
        return None
    # This project's datetime.now() values are naive Beijing local time, not UTC.
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(BEIJING)
    return parsed.date()


def _source_host(value: str | None) -> str:
    try:
        parsed = urlsplit(value or "")
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return parsed.hostname.lower()
    except ValueError:
        pass
    return "（来源缺失或无效）"


def build_report(database: Path | str = DEFAULT_DATABASE, *, days: int = 7, as_of: date | None = None) -> dict:
    if days < 1:
        raise ValueError("--days 必须大于 0")
    report_day = as_of or datetime.now(BEIJING).date()
    first_day = report_day - timedelta(days=days)
    daily = {
        (first_day + timedelta(days=offset)).isoformat(): dict.fromkeys(METRIC_LABELS, 0)
        for offset in range(days)
    }
    source_counts: dict[str, Counter] = {}
    statuses: Counter = Counter()
    excluded = {"demo": 0, "inactive_status": 0, "expired_deadline": 0}
    date_quality = {"missing_created_at": 0, "invalid_created_at": 0}
    publication_dates = dict.fromkeys(
        ("within_window", "before_window", "on_or_after_as_of", "missing", "invalid"), 0
    )

    with closing(connect_read_only(database)) as connection:
        # A single read transaction keeps jobs and logs on the same snapshot.
        connection.execute("BEGIN")
        rows = connection.execute(
            "SELECT id, created_at, published_at, source_url, is_demo, status, "
            "lifecycle_status, deadline, evidence_text, intake_grade, intake_route, posting_scope "
            "FROM jobs"
        )
        for row in rows:
            if row["is_demo"] != 0:
                excluded["demo"] += 1
                continue
            if row["status"] in INACTIVE_STATUSES or row["lifecycle_status"] in INACTIVE_STATUSES:
                excluded["inactive_status"] += 1
                continue
            deadline = parse_known_deadline(row["deadline"] or "") or extract_application_deadline(
                row["evidence_text"] or ""
            )
            if deadline is not None and deadline < report_day:
                excluded["expired_deadline"] += 1
                continue
            created_day = _beijing_day(row["created_at"])
            if created_day is None:
                missing = row["created_at"] is None or not str(row["created_at"]).strip()
                date_quality["missing_created_at" if missing else "invalid_created_at"] += 1
                continue
            if not first_day <= created_day < report_day:
                continue

            published_day = _beijing_day(row["published_at"])
            if published_day is None:
                missing = row["published_at"] is None or not str(row["published_at"]).strip()
                publication_bucket = "missing" if missing else "invalid"
            elif published_day < first_day:
                publication_bucket = "before_window"
            elif published_day >= report_day:
                publication_bucket = "on_or_after_as_of"
            else:
                publication_bucket = "within_window"
            publication_dates[publication_bucket] += 1

            filtered = (
                row["intake_grade"] == "D"
                or row["intake_route"] == "过滤留档"
                or row["posting_scope"] == "non_job_notice"
            )
            candidate = not filtered and row["intake_grade"] in {"A", "B"}
            recent = publication_bucket == "within_window"
            counts = {
                "new_real_jobs": 1,
                "screened_review_candidates": int(candidate),
                "undecided_review_jobs": int(not filtered and not candidate),
                "filtered_jobs": int(filtered),
                "recently_published_new_jobs": int(recent),
                "recently_published_review_candidates": int(recent and candidate),
            }
            for metric, count in counts.items():
                daily[created_day.isoformat()][metric] += count
            source_counts.setdefault(_source_host(row["source_url"]), Counter()).update(counts)
            statuses[row["status"] or "（状态缺失）"] += 1

        has_logs = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_runs'"
        ).fetchone()
        latest_runs = [] if not has_logs else [
            dict(row) for row in connection.execute(
                "SELECT id, task_name, status, message, created_at FROM task_runs "
                "WHERE task_name LIKE '%采集%' AND task_name NOT LIKE '%模拟%' "
                "AND task_name NOT LIKE '%演示%' ORDER BY created_at DESC, id DESC LIMIT 10"
            )
        ]

    metrics = {}
    for metric in METRIC_LABELS:
        total = sum(row[metric] for row in daily.values())
        metrics[metric] = {
            "total": total,
            "daily_average": round(total / days, 4),
            "active_days": sum(row[metric] > 0 for row in daily.values()),
        }
    return {
        "database": str(Path(database).resolve()),
        "window": {
            "as_of": report_day.isoformat(),
            "days": days,
            "start_date": first_day.isoformat(),
            "end_date": (report_day - timedelta(days=1)).isoformat(),
            "timezone": "Asia/Shanghai (UTC+08:00)",
            "date_basis": "jobs.created_at；不采用 collected_at、updated_at 或最后核验时间",
            "naive_timestamp_assumption": "无时区时间按原系统北京时间解释；带时区时间转为北京时间",
            "current_host_utc_offset": str(datetime.now().astimezone().utcoffset()),
        },
        "metric_labels": METRIC_LABELS,
        "metrics": metrics,
        "daily": [{"date": day, **counts} for day, counts in daily.items()],
        "sources": [
            {"source_host": host, **dict(counts)}
            for host, counts in sorted(source_counts.items(), key=lambda item: (-item[1]["new_real_jobs"], item[0]))
        ],
        "status_counts": dict(sorted(statuses.items())),
        "publication_dates": publication_dates,
        "date_quality": date_quality,
        "excluded": excluded,
        "latest_collection_runs": latest_runs,
        "manual_publishability": "未核实人工可发布量；待核验、待审核和 A/B 初筛均不等于可发布。",
        "notes": [
            "窗口为 --as-of 前的完整自然日，不含 --as-of 当天；无新增日期补零并计入日均分母。",
            "按现有唯一 fingerprint 对应的 Job 记录计数；重复采集不新增计数，未额外进行跨来源语义去重。",
            "来源按 source_url 主机统计；Job 无 source_id，不能由此归因到具体 Source 配置。",
            "日报中近期发布子集仍按首次入库日归属；旧公告补采和发布日期缺失不计近期发布。",
            "publication_dates、status_counts、sources 仅覆盖窗口内新增；excluded 和 date_quality 为全库快照。",
            "状态与分级取数据库当前快照，历史 --as-of 不会还原当时审核或归档状态；截止日期按 --as-of 判断。",
            "最新采集日志为当前快照最近 10 条（可含当天）；完成状态仍可能含单条失败，请结合 message 阅读。",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读招聘产量 JSON 报告（按完整北京时间自然日统计）")
    parser.add_argument("--days", type=int, default=7, help="完整自然日数，默认 7；可使用 14")
    parser.add_argument("--as-of", type=date.fromisoformat, help="统计截止日 YYYY-MM-DD，不包含当天；默认北京时间今天")
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE, help="SQLite 文件路径；默认项目 data/recruiting_local.db")
    args = parser.parse_args(argv)
    try:
        report = build_report(args.db, days=args.days, as_of=args.as_of)
    except (OSError, sqlite3.Error, ValueError, OverflowError) as exc:
        parser.error(f"只读统计失败：{exc}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    # Keep redirected JSON and Chinese diagnostics UTF-8 on Windows too.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
