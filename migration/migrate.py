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

    migrations = _collect_migrations()
    if not migrations:
        logger.info("[Migration] 未找到迁移文件")
        return

    from sqlalchemy import text

    with engine.connect() as conn:
        state = read_state(conn, migrations)
        conn.rollback()
        _ensure_runner_tables(conn)
        for (version, description, checksum), target in state.renumbered:
            conn.execute(text(f"""
                INSERT INTO {RENUMBERED_TABLE} (recorded_version, recorded_description, recorded_checksum, current_file)
                VALUES (:v, :d, :c, :f) ON CONFLICT DO NOTHING
            """), {"v": version, "d": description, "c": checksum, "f": target})
            logger.info(f"[Migration] 识别为改号: {version} {description} = {target}")
        conn.commit()

        pending = [m for m in migrations if m.name not in state.applied]
        if not pending:
            logger.info("[Migration] 所有迁移已是最新")
            return

        for path in pending:
            version, description = _parse_filename(path)
            sql = path.read_text(encoding="utf-8")
            checksum = _checksum(sql)

            logger.info(f"[Migration] 执行: {path.name}")
            try:
                # Let PostgreSQL parse the entire migration, including dollar quotes and comments.
                # Driver SQL does not reinterpret colons in SQL literals as bound parameters.
                conn.exec_driver_sql(sql)
                if version in state.held_slots:
                    conn.execute(text(f"""
                        INSERT INTO {SHARED_SLOT_TABLE} (version, description, checksum, slot_held_by, applied_at)
                        VALUES (:v, :d, :c, :h, :t)
                    """), {"v": version, "d": description, "c": checksum, "h": state.held_slots[version],
                           "t": datetime.now(timezone.utc)})
                else:
                    conn.execute(text(f"""
                        INSERT INTO {MIGRATION_TABLE} (version, description, checksum, applied_at)
                        VALUES (:v, :d, :c, :t)
                    """), {"v": version, "d": description, "c": checksum, "t": datetime.now(timezone.utc)})
                conn.commit()
                logger.info(f"[Migration] 完成: {version} — {description}")
            except Exception as e:
                conn.rollback()
                logger.error(f"[Migration] 失败: {path.name} | {e}")
                raise


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
