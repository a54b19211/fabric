"""只迁移新历史三表，必须显式提供独立URL；不加载旧APP配置。"""

from pathlib import Path

from sqlalchemy import text

from biz_rules.history_models import canonical
from biz_rules.history_sources import SOURCE_CATALOG
from db.history import history_engine


def migrate_history():
    directory = Path(__file__).parent / "versions"
    sql = "\n".join(
        (directory / name).read_text()
        for name in ("V017__history_records.sql", "V018__graph_record_kind.sql")
    )
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
            if source_id == "primekg_graph":
                # 只更新当前能力说明，不重置已持久来源依赖/许可或版本。
                conn.execute(
                    text(
                        "UPDATE history_sources SET catalog=catalog || CAST(:metadata AS jsonb) WHERE source_id=:id"
                    ),
                    {
                        "id": source_id,
                        "metadata": canonical(
                            {"history_coverage": catalog["history_coverage"]}
                        ),
                    },
                )


if __name__ == "__main__":
    migrate_history()
    print("历史三表迁移完成（仅独立历史数据库）")
