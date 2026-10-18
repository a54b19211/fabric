"""只迁移新历史三表，必须显式提供独立URL；不加载旧APP配置。"""

from pathlib import Path

from sqlalchemy import text

from biz_rules.history_models import canonical
from biz_rules.history_sources import SOURCE_CATALOG
from db.history import history_engine


def migrate_history():
    sql = (Path(__file__).parent / "versions" / "V017__history_records.sql").read_text()
    with history_engine().begin() as conn:
        for statement in sql.split(";"):
            statement = "\n".join(
                line
                for line in statement.splitlines()
                if not line.lstrip().startswith("--")
            ).strip()
            if statement:
                conn.execute(text(statement))
        for source_id, catalog in SOURCE_CATALOG.items():
            conn.execute(
                text("""
              INSERT INTO history_sources(source_id,catalog) VALUES (:id,CAST(:catalog AS jsonb))
              ON CONFLICT (source_id) DO NOTHING
            """),
                {"id": source_id, "catalog": canonical(catalog)},
            )


if __name__ == "__main__":
    migrate_history()
    print("历史三表迁移完成（仅独立历史数据库）")
