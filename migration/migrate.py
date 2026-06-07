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
"""
import argparse
import hashlib
from pathlib import Path
from datetime import datetime, timezone

from loguru import logger

from db.session import get_engine

VERSIONS_DIR = Path(__file__).parent / "versions"
MIGRATION_TABLE = "schema_migrations"


def _ensure_migration_table(conn):
    conn.execute(__import__("sqlalchemy").text(f"""
        CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} (
            version     TEXT        PRIMARY KEY,
            description TEXT        DEFAULT '',
            checksum    TEXT        DEFAULT '',
            applied_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """))
    conn.commit()


def _applied_versions(conn) -> set:
    from sqlalchemy import text
    rows = conn.execute(text(f"SELECT version FROM {MIGRATION_TABLE}")).fetchall()
    return {r[0] for r in rows}


def _checksum(content: str) -> str:
    return hashlib.md5(content.encode()).hexdigest()


def _parse_filename(path: Path) -> tuple[str, str]:
    """从文件名解析版本号和描述，如 V001__init_tables.sql → ('V001', 'init tables')"""
    stem = path.stem  # V001__init_tables
    parts = stem.split("__", 1)
    version = parts[0]
    description = parts[1].replace("_", " ") if len(parts) > 1 else ""
    return version, description


def _collect_migrations() -> list[Path]:
    if not VERSIONS_DIR.exists():
        return []
    files = sorted(
        f for f in VERSIONS_DIR.glob("V*.sql")
        if ".rollback." not in f.name
    )
    return files


def run_rollback(steps: int = 1):
    """
    回滚最近 N 步迁移。
    查找对应迁移的 .rollback.sql 文件，按降序执行回滚 SQL，
    并从 schema_migrations 表中删除对应记录。
    """
    engine = get_engine()
    if engine is None:
        logger.error("[Migration] DB 未配置，无法执行回滚")
        return

    migrations = _collect_migrations()
    if not migrations:
        logger.info("[Migration] 未找到迁移文件")
        return

    with engine.connect() as conn:
        _ensure_migration_table(conn)
        applied = _applied_versions(conn)

        applied_migrations = [
            m for m in migrations
            if _parse_filename(m)[0] in applied
        ]
        if not applied_migrations:
            logger.info("[Migration] 没有已执行的迁移可供回滚")
            return

        targets = applied_migrations[-steps:]
        targets = list(reversed(targets))

        for path in targets:
            version, description = _parse_filename(path)
            rollback_path = path.with_name(f"{path.stem}.rollback.sql")
            if not rollback_path.exists():
                logger.warning(
                    f"[Migration] 跳过 {version}: 缺少回滚文件 {rollback_path.name}"
                )
                continue

            sql = rollback_path.read_text(encoding="utf-8")
            logger.info(f"[Migration] 回滚: {path.name} ({description})")
            try:
                from sqlalchemy import text
                for statement in sql.split(";"):
                    lines = [
                        line for line in statement.splitlines()
                        if line.strip() and not line.strip().startswith("--")
                    ]
                    stmt = "\n".join(lines).strip()
                    if stmt:
                        conn.execute(text(stmt))

                conn.execute(text(
                    f"DELETE FROM {MIGRATION_TABLE} WHERE version = :v"
                ), {"v": version})
                conn.commit()
                logger.info(f"[Migration] 回滚完成: {version}")
            except Exception as e:
                conn.rollback()
                logger.error(f"[Migration] 回滚失败: {path.name} | {e}")
                raise


def run_migrations():
    engine = get_engine()
    if engine is None:
        logger.error("[Migration] DB 未配置，无法执行迁移")
        return

    migrations = _collect_migrations()
    if not migrations:
        logger.info("[Migration] 未找到迁移文件")
        return

    with engine.connect() as conn:
        _ensure_migration_table(conn)
        applied = _applied_versions(conn)

        pending = [m for m in migrations if _parse_filename(m)[0] not in applied]
        if not pending:
            logger.info("[Migration] 所有迁移已是最新")
            return

        for path in pending:
            version, description = _parse_filename(path)
            sql = path.read_text(encoding="utf-8")
            checksum = _checksum(sql)

            logger.info(f"[Migration] 执行: {path.name}")
            try:
                # 按分号拆分，逐条执行（跳过空语句）
                from sqlalchemy import text
                for statement in sql.split(";"):
                    # 去掉注释行后检查是否还有实际 SQL
                    lines = [
                        line for line in statement.splitlines()
                        if line.strip() and not line.strip().startswith("--")
                    ]
                    stmt = "\n".join(lines).strip()
                    if stmt:
                        conn.execute(text(stmt))

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
        _ensure_migration_table(conn)
        applied = _applied_versions(conn)

    print(f"\n{'版本':<8} {'状态':<10} {'描述'}")
    print("-" * 50)
    for path in migrations:
        version, description = _parse_filename(path)
        status = "[done]" if version in applied else "[pending]"
        print(f"{version:<8} {status:<10} {description}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ZK_Data Fabric 数据库迁移工具")
    parser.add_argument("--list", action="store_true", help="查看迁移状态")
    parser.add_argument("--rollback", type=int, nargs="?", const=1, metavar="N",
                        help="回滚最近 N 步迁移（默认 1 步）")
    args = parser.parse_args()

    if args.list:
        list_migrations()
    elif args.rollback is not None:
        run_rollback(args.rollback)
    else:
        run_migrations()
