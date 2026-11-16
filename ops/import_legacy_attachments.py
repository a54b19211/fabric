"""One-off: import the old zk attachments table into fabric as attachment versions under their page versions.

For each row of the old `attachments` table (zk_legacy) whose parent document was migrated (legacy_aliases,
status imported), the original file (/data/files/zk-attachments/raw/<site>/<file_uuid>.<ext>) is stored by
sha256 (fetch.web_client.store_original) as a site-shaped attachment version (same record id rule as
fetch.site_crawler), and linked to the parent page version. Linking reuses store.records: the parent page's
own original is re-imported in the same archive run, its fingerprint is unchanged, so no new parent version
is made and _link_attachments records the link as same_run_parent_version.
Idempotent: unchanged attachments are not stored again, links are written once, the run key is deterministic.

    PYTHONPATH=. .venv/bin/python ops/import_legacy_attachments.py --legacy-url postgresql://...@/zk_legacy --plan|--apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import create_engine, text

from fetch.site_crawler import ATT_SCHEMA, _record, attachment_record_id
from fetch.web_client import store_original
from store.record_models import ArchiveImport, ArchiveRecord
from store.records import fabric_engine, import_archive

FILES = Path("/data/files/zk-attachments/raw")
BATCH = 400


def _original(file_uuid: str, file_name: str, url: str) -> Path | None:
    """The stored original. Doc/xls originals also have a converted .pdf next to them: keep the original."""
    found = [p for d in ("cde", "nmpa") for p in (FILES / d).glob(file_uuid + ".*")]
    if len(found) <= 1:
        return found[0] if found else None
    for name in (url, file_name):
        ext = Path(name.split("?")[0]).suffix.lower()
        for p in found:
            if ext and p.suffix.lower() == ext:
                return p
    return next((p for p in found if p.suffix.lower() != ".pdf"), found[0])


def _parents(conn, uuids):
    """legacy parent uuid -> (source, record id, current version id, raw original, media type, kind)."""
    ids = [f"legacy:{site}:{u}" for u in uuids for site in ("cde", "nmpa")]
    rows = conn.execute(text("""
        SELECT DISTINCT ON (a.legacy_record_id) a.legacy_record_id, a.real_source_id, a.real_record_id,
               v.version_id, v.record_kind, v.payload->>'raw_content' AS raw, v.payload->>'raw_media_type' AS media
        FROM legacy_aliases a JOIN fabric_versions v
          ON v.source_id=a.real_source_id AND v.source_record_id=a.real_record_id AND v.replaced_by IS NULL
         AND v.is_demo=a.is_demo
        WHERE a.status='imported' AND NOT a.is_demo AND a.legacy_record_id = ANY(:ids)
        ORDER BY a.legacy_record_id, (v.version_id=a.real_version_id) DESC, v.published_seq DESC"""),
        {"ids": ids}).mappings()
    return {r["legacy_record_id"].rsplit(":", 1)[-1]: r for r in rows}


def _linked(conn):
    return {(r[0], r[1]) for r in conn.execute(text(
        "SELECT v.source_record_id, l.parent_record_id FROM fabric_attachment_links l "
        "JOIN fabric_versions v ON v.version_id=l.attachment_version_id WHERE l.parent_version_id IS NOT NULL"))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--legacy-url", required=True, help="database URL of the old zk DB (has table attachments)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    with create_engine(args.legacy_url).connect() as old:
        rows = old.execute(text("SELECT id, parent_uuid, file_name, file_url, file_uuid FROM attachments "
                                "WHERE NOT is_deleted ORDER BY id")).mappings().all()
    with fabric_engine().connect() as conn:
        parents, linked = _parents(conn, {r["parent_uuid"] for r in rows}), _linked(conn)

    counts, todo = Counter(total=len(rows)), defaultdict(list)  # source -> [(parent, attachment row, file)]
    for row in rows:
        parent = parents.get(row["parent_uuid"])
        if parent is None:
            counts["parent_not_imported"] += 1
            continue
        path = _original(row["file_uuid"], row["file_name"] or "", row["file_url"] or "")
        if path is None:
            counts["missing_file"] += 1
            continue
        rid = attachment_record_id(parent["real_record_id"], row["file_url"] or row["file_uuid"])
        if (rid, parent["real_record_id"]) in linked:
            counts["already_linked"] += 1
            continue
        counts["to_link"] += 1
        todo[parent["real_source_id"]].append((parent, row, path, rid))
    print(json.dumps(dict(counts), ensure_ascii=False), {s: len(v) for s, v in todo.items()})
    if not args.apply:
        return

    for source, items in sorted(todo.items()):
        seen = {}
        for parent, row, path, rid in items:
            seen.setdefault(rid, (parent, row, path))  # same link twice under one parent: one record
        items = sorted(seen.items())
        for start in range(0, len(items), BATCH):
            records, parent_ids = [], {}
            for rid, (parent, row, path) in items[start:start + BATCH]:
                content = path.read_bytes()
                stored = store_original(content, rid, source, path.suffix)
                records.append(_record(rid, {
                    "schema_version": ATT_SCHEMA, "role": "attachment", "site": source,
                    "url": row["file_url"] or None, "parent_record_id": parent["real_record_id"],
                    "file_name": row["file_name"] or path.name, "file_ext": path.suffix.lower(),
                    "sha256": stored["sha256"], "size": stored["size"], "store_path": stored["local_path"],
                    "discovered_from": "legacy_attachments_table", "status": "stored"}))
                parent_ids[parent["real_record_id"]] = ArchiveRecord(
                    source_record_id=parent["real_record_id"], record_kind=parent["record_kind"],
                    raw_content=parent["raw"], raw_media_type=parent["media"] or "application/json")
            batch = list(parent_ids.values()) + records
            key = "legacy-attachments:" + source + ":" + hashlib.sha256(
                "|".join(r.source_record_id + r.raw_content for r in batch).encode()).hexdigest()[:32]
            run = import_archive(ArchiveImport(
                source_id=source, is_demo=False, declared_count=len(batch), records=batch,
                scope_description="old zk attachments table: originals linked to their migrated page versions"),
                key=key)
            print(source, start, len(records), "run", run.id, run.state)
    print("done; run --plan again: to_link should be 0")


if __name__ == "__main__":
    main()
