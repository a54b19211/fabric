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
import re
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


# 美元引用起始标记：$$ 或 $tag$
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def _split_sql(sql: str) -> list[str]:
    """按「顶层」分号拆分 SQL 语句。

    不能用 `sql.split(";")`：PostgreSQL 里分号合法出现在四处非语句边界 ——
    `$tag$ ... $tag$` 美元引用块（函数体）、单引号字符串、双引号标识符、
    注释。硬拆会把一条语句劈成几段非法 SQL。实测 V019 的三个 plpgsql 函数体
    被拆成 61 段、其中 8 段美元符不平衡，直接用旧实现执行必然报语法错。

    这里用状态机逐字符扫描，只认顶层分号为语句边界：
      - `'...'`  单引号字符串，`''` 为转义
      - `"..."`  双引号标识符，`""` 为转义
      - `$tag$`  美元引用块，tag 可为空（`$$`）
      - `--`     行注释，吞到行尾
      - `/* */`  块注释，支持嵌套
    """
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)

    while i < n:
        ch = sql[i]

        # -- 行注释：连同换行前的内容原样保留，交给 PG 忽略
        if ch == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j == -1 else j
            buf.append(sql[i:j])
            i = j
            continue

        # /* */ 块注释（PG 允许嵌套）
        if ch == "/" and sql.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            buf.append(sql[i:j])
            i = j
            continue

        # 单引号字符串 / 双引号标识符：成对双写即为转义
        if ch in ("'", '"'):
            quote = ch
            j = i + 1
            while j < n:
                if sql[j] == quote:
                    if j + 1 < n and sql[j + 1] == quote:
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(sql[i:j])
            i = j
            continue

        # 美元引用块：找到同 tag 的闭合标记为止
        if ch == "$":
            m = _DOLLAR_TAG.match(sql, i)
            if m:
                tag = m.group(0)
                end = sql.find(tag, i + len(tag))
                if end == -1:
                    # 未闭合：原样交给 PG 报错，避免静默吞掉内容
                    buf.append(sql[i:])
                    i = n
                else:
                    j = end + len(tag)
                    buf.append(sql[i:j])
                    i = j
                continue

        # 顶层分号 → 断句
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


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
                # 按顶层分号拆分（跳过空语句），$tag$ 函数体与引号内分号不拆
                from sqlalchemy import text
                for statement in _split_sql(sql):
                    # 去掉注释行后检查是否还有实际 SQL
                    lines = [
                        line for line in statement.splitlines()
                        if line.strip() and not line.strip().startswith("--")
                    ]
                    stmt = "\n".join(lines).strip()
                    if stmt:
                        # 必须走 exec_driver_sql 而不是 text()：text() 会把 SQL 里的
                        # `:name` 一律当绑定参数解析，而 PostgreSQL 原生语法里
                        # `::text` 类型转换、`'{"is_deleted":true}'::jsonb` 这类
                        # 字面量都含冒号，用 text() 会抛
                        # "A value is required for bind parameter 'true'"。
                        conn.exec_driver_sql(stmt)

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
        status = "✓ 已执行" if version in applied else "○ 待执行"
        print(f"{version:<8} {status:<10} {description}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ZK_Data Fabric 数据库迁移工具")
    parser.add_argument("--list", action="store_true", help="查看迁移状态")
    args = parser.parse_args()

    if args.list:
        list_migrations()
    else:
        run_migrations()
