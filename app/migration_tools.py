"""One-time, explicit import of legacy SQLite business data into a blank shared database."""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import MetaData, Table, create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
import app.models  # noqa: F401 - load all destination metadata


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class MigrationResult:
    dry_run: bool
    row_counts: dict[str, int]


# Authentication, queued work and provider setup belong to the new environment.
# This ordering preserves the legacy Job -> DistributionItem/ReviewLog relations.
IMPORT_TABLES = (
    "sources",
    "users",
    "jobs",
    "review_logs",
    "distribution_items",
    "task_runs",
)


def _source_table(source_engine, table_name: str) -> Table | None:
    metadata = MetaData()
    try:
        return Table(table_name, metadata, autoload_with=source_engine)
    except Exception:
        return None


def _target_is_empty(target_session_factory: sessionmaker) -> bool:
    with target_session_factory() as session:
        for table_name in IMPORT_TABLES:
            table = Base.metadata.tables[table_name]
            if session.scalar(select(func.count()).select_from(table)):
                return False
    return True


def migrate_sqlite(source_path: Path, target_session_factory: sessionmaker, *, dry_run: bool) -> MigrationResult:
    """Import compatible legacy rows while keeping source read-only and IDs intact."""
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise MigrationError("SQLite 源文件不存在")
    if not _target_is_empty(target_session_factory):
        raise MigrationError("目标数据库非空，已拒绝导入以避免混合数据")

    source_engine = create_engine(f"sqlite:///{source_path.as_posix()}")
    counts: dict[str, int] = {}
    try:
        for table_name in IMPORT_TABLES:
            source_table = _source_table(source_engine, table_name)
            if source_table is None:
                continue
            with source_engine.connect() as connection:
                counts[table_name] = len(connection.execute(select(source_table)).mappings().all())
        if dry_run:
            return MigrationResult(dry_run=True, row_counts=counts)

        with target_session_factory() as session:
            for table_name in IMPORT_TABLES:
                source_table = _source_table(source_engine, table_name)
                if source_table is None:
                    continue
                destination = Base.metadata.tables[table_name]
                destination_columns = {column.name for column in destination.columns}
                with source_engine.connect() as connection:
                    rows = [
                        {key: value for key, value in row.items() if key in destination_columns}
                        for row in connection.execute(select(source_table)).mappings()
                    ]
                if rows:
                    session.execute(destination.insert(), rows)
            session.commit()
    except Exception as exc:
        raise MigrationError(f"SQLite 导入失败，目标事务已回滚：{str(exc)[:200]}") from exc
    finally:
        source_engine.dispose()
    return MigrationResult(dry_run=False, row_counts=counts)
