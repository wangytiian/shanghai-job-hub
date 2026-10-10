"""Preview or explicitly regrade verified historical SBS C leads into pending B."""

import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.b_intake_regrade import apply_b_regrade, preview_b_regrade  # noqa: E402


DATABASE = PROJECT_ROOT / "data" / "recruiting_local.db"
BACKUP_DIR = PROJECT_ROOT / "backups"


def _backup_sqlite(database: Path) -> Path:
    database = database.absolute()
    project_root = PROJECT_ROOT.absolute()
    if database.is_symlink() or database.parent.is_symlink():
        raise ValueError("数据库路径不能经过符号链接")
    if not database.is_relative_to(project_root):
        raise ValueError("数据库路径超出项目目录")
    backup_dir = BACKUP_DIR.absolute()
    if backup_dir.is_symlink() or not backup_dir.is_relative_to(project_root):
        raise ValueError("备份目录超出项目目录")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"recruiting_local-before-b-regrade-{stamp}-{uuid4().hex[:8]}.db"
    if backup_path.exists():
        raise FileExistsError(backup_path)
    with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(backup_path)) as backup:
            source.backup(backup)
            backup.commit()
            integrity = backup.execute("PRAGMA integrity_check").fetchone()
            if integrity != ("ok",):
                raise RuntimeError(f"数据库备份完整性校验失败：{integrity}")
    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="按明确岗位 ID 执行；默认仅预览")
    parser.add_argument("--ids", default="", help="逗号分隔的岗位 ID，例如 108,109")
    args = parser.parse_args()
    if not DATABASE.is_file():
        parser.error(f"找不到本地数据库：{DATABASE}")
    if args.ids and not args.apply:
        parser.error("--ids 只能与 --apply 同时使用")
    if args.apply and not args.ids:
        parser.error("执行重判必须明确提供 --ids")
    try:
        selected = sorted({int(value.strip()) for value in args.ids.split(",") if value.strip()})
    except ValueError:
        parser.error("--ids 必须是逗号分隔的整数")
    if any(value <= 0 for value in selected):
        parser.error("岗位 ID 必须为正整数")

    engine = create_engine(f"sqlite+pysqlite:///{DATABASE.as_posix()}")
    with Session(engine) as session:
        preview = preview_b_regrade(session)
        eligible = {int(row["id"]) for row in preview if row["decision"] == "eligible"}
        if not args.apply:
            print(json.dumps({"eligible": [row for row in preview if row["decision"] == "eligible"],
                              "skipped": [{"id": row["id"], "reason": row["reason"]}
                                          for row in preview if row["decision"] != "eligible"]},
                             ensure_ascii=False, indent=2))
            return 0
        invalid = sorted(set(selected) - eligible)
        if invalid:
            raise ValueError(f"指定岗位不在当前可重判预览中：{invalid}；数据库未修改")
        backup_path = _backup_sqlite(DATABASE)
        print(f"已完成在线备份并校验：{backup_path}")
        applied = apply_b_regrade(session, datetime.now(), selected, strict=True)
        if applied != len(selected):
            raise RuntimeError(f"重判数量与指定 ID 不一致：预期 {len(selected)}，实际 {applied}")
        print(json.dumps({"selected_ids": selected, "applied": applied,
                          "backup_path": str(backup_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as exc:
        print(f"执行失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
