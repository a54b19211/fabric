"""显式迁移索引投影表；必须使用独立History连接。"""

from pathlib import Path
from sqlalchemy import text
from db.history import history_engine


def migrate_index():
    sql = (Path(__file__).parent / "versions/V020__weknora_index.sql").read_text()
    with history_engine().begin() as conn:
        for statement in sql.split(";"):
            statement = "\n".join(s for s in statement.splitlines() if not s.lstrip().startswith("--")).strip()
            if statement:
                conn.execute(text(statement))


if __name__ == "__main__":
    migrate_index()
