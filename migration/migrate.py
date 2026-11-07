"""
数据库迁移执行器。

用法:
    python -m migration.migrate          # 执行所有未跑过的迁移
    python -m migration.migrate --list   # 查看迁移状态

迁移文件命名规范：
    migration/versions/V{版本号}__{描述}.sql
    例如：V001__init_tables.sql
          V002__add_chictr_meta.sql

执行规则：
    - 按版本号升序执行
    - 每条迁移只执行一次（通过 schema_migrations 表记录）
    - 幂等：已执行的迁移不会重复跑
    - 已执行的迁移按（版本号, 描述, 校验和）识别，不只看版本号：两条开发线曾并行使用 V020–V023，
      改号的迁移按 migration/known_migrations.py 认出来，原记录不删不改；认不出的记录直接报错，什么也不执行
"""
import argparse
from pathlib import Path
from datetime import datetime, timezone

from loguru import logger

from db.session import get_engine
from migration.app_ledger import (  # noqa: F401 - the names tests and operators use from this module
    MIGRATION_TABLE,
    RENUMBERED_TABLE,
    SHARED_SLOT_TABLE,
    MigrationState,
    MigrationStateError,
    _checksum,
    _ensure_migration_table,
    _ensure_runner_tables,
    _parse_filename,
    read_state,
)
from migration import runner
from migration.sqltext import split_sql

VERSIONS_DIR = Path(__file__).parent / "versions"

_split_sql = split_sql  # the splitter lives in migration.sqltext; this name is kept for the callers of the runner


def _collect_migrations() -> list[Path]:
    if not VERSIONS_DIR.exists():
        return []
    files = sorted(VERSIONS_DIR.glob("V*.sql"))
    return files


def run_migrations():
    engine = get_engine()
    if engine is None:
        logger.error("[Migration] DB 未配置，无法执行迁移")
        return
    return runner.apply("app", engine)


def list_migrations():
    engine = get_engine()
    if engine is None:
        logger.error("[Migration] DB 未配置")
        return

    migrations = _collect_migrations()
    with engine.connect() as conn:
        state = read_state(conn, migrations)

    renamed = {target: f"（记录为 {row[0]} {row[1]}）" for row, target in state.renumbered}
    print(f"\n{'版本':<8} {'状态':<10} {'描述'}")
    print("-" * 50)
    for path in migrations:
        version, description = _parse_filename(path)
        status = "✓ 已执行" if path.name in state.applied else "○ 待执行"
        print(f"{version:<8} {status:<10} {description}{renamed.get(path.name, '')}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ZK_Data Fabric 数据库迁移工具")
    parser.add_argument("--list", action="store_true", help="查看迁移状态")
    args = parser.parse_args()

    if args.list:
        list_migrations()
    else:
        run_migrations()
