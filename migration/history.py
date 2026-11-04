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
        for name in (
            "V017__history_records.sql",
            "V018__graph_record_kind.sql",
            "V021__history_version_rank.sql",
            "V022__registry_changes.sql",
            "V023__derived_clean_text.sql",
            "V025__clean_stage.sql",
        )
    )
    with history_engine().begin() as conn:
        # 现役有读请求时不无限等锁：等不到就整笔回滚，稍后再跑
        conn.exec_driver_sql("SET LOCAL lock_timeout='3s'")
        conn.exec_driver_sql("SET LOCAL statement_timeout='60s'")
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
            if source_id == "external_archive":
                conn.execute(
                    text(
                        "UPDATE history_sources SET catalog=catalog || CAST(:metadata AS jsonb) WHERE source_id=:id"
                    ),
                    {
                        "id": source_id,
                        "metadata": canonical(
                            {
                                "business_types": catalog["business_types"],
                                "read_scope": catalog["read_scope"],
                            }
                        ),
                    },
                )
            if catalog.get("acquisition_channel") == "site_plugin":
                # 站点来源的说明文字随采集目录更新（已有行 ON CONFLICT 不会改）；只改描述字段，
                # 不动 access、依赖和许可状态
                conn.execute(
                    text(
                        "UPDATE history_sources SET catalog=catalog || CAST(:metadata AS jsonb) WHERE source_id=:id"
                    ),
                    {"id": source_id, "metadata": canonical({k: catalog[k] for k in (
                        "name", "business_types", "acquisition_channel", "history_coverage", "native_id",
                        "read_scope", "limitations") if k in catalog})},
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
