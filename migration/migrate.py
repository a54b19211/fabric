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
import hashlib
import re
from dataclasses import dataclass, field
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


# A migration is identified by what schema_migrations stores for it: version, description and checksum.
# The rebuild line and the team line numbered V020-V023 in parallel, so a version alone does not say which
# migration ran. Every row is looked up in migration.known_migrations (every file the repository ever had):
#   current / earlier  -> today's file of that version has run;
#   renumbered         -> today's file with that content (under its new number) has run; the row stays as it is and
#                         the mapping is recorded in RENUMBERED_TABLE;
#   superseded/unknown -> nothing is run and nothing is written: a person has to say what this database is.
# A file whose version slot in schema_migrations is held by a renumbered row is recorded in SHARED_SLOT_TABLE.
# Rows are never deleted or rewritten.
RENUMBERED_TABLE = "schema_migration_renumbered"
SHARED_SLOT_TABLE = "schema_migrations_shared_slot"


class MigrationStateError(RuntimeError):
    """The recorded migrations do not match any migration of this repository; nothing was run or written."""


@dataclass
class MigrationState:
    applied: set[str] = field(default_factory=set)  # file names that have run
    renumbered: list[tuple] = field(default_factory=list)  # (recorded row, file today)
    held_slots: dict[str, str] = field(default_factory=dict)  # version -> description of the row holding it


def _table_exists(conn, name) -> bool:
    from sqlalchemy import text

    return conn.execute(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": name}).scalar_one()


def _rows(conn, table, columns):
    from sqlalchemy import text

    if not _table_exists(conn, table):
        return []
    return [tuple(row) for row in conn.execute(text(f"SELECT {columns} FROM {table} ORDER BY 1"))]


def read_state(conn, migrations) -> MigrationState:
    """What has run, from schema_migrations and the runner's own tables. Raises MigrationStateError on any row
    that is not a known migration (or a known one no file today stands for), before anything is written."""
    from migration.known_migrations import KNOWN

    known = {(version, description, checksum): (kind, target, seen)
             for version, description, checksum, kind, target, seen in KNOWN}
    files = {_parse_filename(path)[0]: path for path in migrations}
    state, problems = MigrationState(), []
    for version, description, checksum in _rows(conn, MIGRATION_TABLE, "version, description, checksum"):
        kind, target, seen = known.get((version, description, checksum), (None, None, None))
        if kind in ("current", "earlier"):
            state.applied.add(target)
        elif kind == "renumbered":
            state.applied.add(target)
            state.renumbered.append(((version, description, checksum), target))
            if version in files and files[version].name != target:
                state.held_slots[version] = description
        elif kind == "superseded":
            today = files[version].name if version in files else "no file"
            problems.append(f"{version} '{description}' (checksum {checksum}) is an earlier migration (seen in "
                            f"{seen}) that no file stands for today; today's {version} is {today} and has not run")
        else:
            problems.append(f"{version} '{description}' (checksum {checksum or 'empty'}) is not a migration any "
                            f"commit of this repository had")
    for version, description, checksum in _rows(conn, SHARED_SLOT_TABLE, "version, description, checksum"):
        path = files.get(version)
        if path is None or (version, description, checksum) not in known:
            problems.append(f"{SHARED_SLOT_TABLE} row {version} '{description}' does not match a known migration")
        else:
            state.applied.add(known[(version, description, checksum)][1])
    if problems:
        raise MigrationStateError("schema_migrations 里有无法识别的记录，没有执行也没有写入任何内容：\n- "
                                  + "\n- ".join(problems))
    return state


def _ensure_runner_tables(conn):
    from sqlalchemy import text

    _ensure_migration_table(conn)
    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {RENUMBERED_TABLE} (
            recorded_version     TEXT NOT NULL,
            recorded_description TEXT NOT NULL,
            recorded_checksum    TEXT NOT NULL,
            current_file         TEXT NOT NULL,
            recognised_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (recorded_version, recorded_description)
        )"""))
    conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {SHARED_SLOT_TABLE} (
            version     TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            checksum    TEXT NOT NULL,
            slot_held_by TEXT NOT NULL,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )"""))
    conn.commit()


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
