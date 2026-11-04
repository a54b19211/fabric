"""显式迁移索引投影表；必须使用独立History连接。"""

from pathlib import Path
from sqlalchemy import text
from db.history import history_engine


def migrate_index():
    sql = "\n".join(
        (Path(__file__).parent / "versions" / name).read_text()
        for name in ("V020__weknora_index.sql", "V024__index_artifact.sql", "V027__index_publications_recent.sql")
    )
    with history_engine().begin() as conn:
        # 现役有读写时不无限等锁：等不到就整笔回滚，稍后再跑
        conn.exec_driver_sql("SET LOCAL lock_timeout='3s'")
        conn.exec_driver_sql("SET LOCAL statement_timeout='60s'")
        for statement in sql.split(";"):
            statement = "\n".join(s for s in statement.splitlines() if not s.lstrip().startswith("--")).strip()
            if statement:
                conn.execute(text(statement))


if __name__ == "__main__":
    migrate_index()
