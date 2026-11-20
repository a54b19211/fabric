"""撤下站点来源里不该有的记录：给每条记录存一个“已撤下”的新版本（墓碑），旧版本原样留着。

    python -m ops.withdraw_records --source phirda_policy --ids ids.txt --reason not_in_column           # 只列出
    python -m ops.withdraw_records --source phirda_policy --ids ids.txt --reason not_in_column --apply   # 存墓碑

ids.txt 一行一个 source_record_id。只处理网页和表格页站点的记录（web_capture / site_record）：墓碑原件是这条记录
当前原件的网址、站点和契约字段，正文和附件清单为空，带 publication_state=withdrawn 和原因（fetch/site_crawler.py ·
_withdrawal）。它和别的原件一样经 store.records.import_archive 存成版本（一次运行，kind=archive），所以：
- 读这条记录（API、检索）看到的是 withdrawn；按版本号读旧版本照样能读到，历史不删；
- 清洗段给墓碑画像时，知识库待办把这条记录撤下（store.index_publication.TOMBSTONES）；
- 以后站点真的又列出这条记录、读到内容，就是一个新版本，记录自然恢复。
已经是墓碑的、库里没有的记录跳过并列出。不删任何东西。
"""
import argparse
import json
import sys

from sqlalchemy import text

from store.record_models import ArchiveImport, ArchiveRecord
from store.records import fabric_engine, import_archive

ROLES = ("web_capture", "site_record")
BATCH = 500


def _current(conn, source_id, record_ids):
    rows = conn.execute(text(
        "SELECT source_record_id, payload->>'raw_content' AS raw FROM fabric_versions "
        "WHERE source_id=:s AND NOT is_demo AND replaced_by IS NULL AND source_record_id=ANY(:ids) "
        "AND COALESCE(payload->'facts'->>'publication_role','source_record')='source_record' "
        "AND payload->'facts'->>'role'=ANY(:roles)"),
        {"s": source_id, "ids": list(record_ids), "roles": list(ROLES)}).mappings().all()
    return {r["source_record_id"]: json.loads(r["raw"]) for r in rows}


def tombstone(data: dict, reason: str) -> dict:
    """当前原件 → 墓碑原件：留下身份和契约字段，正文、附件、网页原文都空。"""
    keep = {k: data[k] for k in ("schema_version", "role", "site", "url", "record_id") if k in data}
    parsed = data.get("parsed") or {}
    return {**keep, "parsed": {"title": parsed.get("title") or "", "publish_date": parsed.get("publish_date") or "",
                               "markdown": "", "parse_method": "withdrawn"},
            "attachments": [], "publication_state": "withdrawn", "withdraw_reason": reason}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--ids", required=True, help="一行一个 source_record_id 的文件")
    parser.add_argument("--reason", required=True, help="撤下原因，写进墓碑（如 not_in_column、page_snapshot）")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with open(args.ids, encoding="utf-8") as stream:
        ids = list(dict.fromkeys(line.strip() for line in stream if line.strip()))
    with fabric_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout='60s'"))
        current = _current(conn, args.source, ids)
    todo = {rid: data for rid, data in current.items() if data.get("publication_state") != "withdrawn"}
    report = {"source": args.source, "asked": len(ids), "to_withdraw": len(todo),
              "already_withdrawn": len(current) - len(todo), "not_found": [i for i in ids if i not in current]}
    print(json.dumps(report, ensure_ascii=False, indent=1))
    if not args.apply or not todo:
        return
    items = list(todo.items())
    for start in range(0, len(items), BATCH):
        chunk = items[start:start + BATCH]
        records = [ArchiveRecord(source_record_id=rid, record_kind="document",
                                 raw_content=json.dumps(tombstone(data, args.reason), ensure_ascii=False,
                                                        sort_keys=True)) for rid, data in chunk]
        run = import_archive(ArchiveImport(
            source_id=args.source, is_demo=False, declared_count=len(records), declared_complete=False,
            scope_description=f"撤下 {len(records)} 条记录（{args.reason}），ops/withdraw_records.py", records=records))
        print(json.dumps({"run": run.get("id"), "state": run.get("state"), "records": len(records)},
                         ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    main()
