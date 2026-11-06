"""v3 6.3: legacy documents into their real sources, through the one pipeline in import mode.

A legacy version (source legacy_documents, frozen from the old documents store) is the old crawler's markdown
of some real source's record. Here it is mapped to that real source and record and imported there as a
*migrated* version; the legacy version itself stays as it is (readable by its old id, with an alias).

- Mapping is proven, not guessed (plan()):
  · API/table sites: the record id is uuid5(URL namespace, "<site>:<uid field>") (core_engine.api_router), the
    same algorithm the documents store used; the uid field's value is read back from the old markdown and the
    uuid recomputed - it must equal the legacy uuid;
  · detail-page sites: the record id is the page URL; the legacy uuid must equal uuid5(URL namespace, URL) and
    the URL must be one of the site's own (its url_pattern);
  · ClinicalTrials.gov: the old markdown embeds the API sections ("**protocolSection**: {json}"); every embedded
    section must parse as a complete JSON object, and the record id is its identificationModule.nctId.
  Anything not proven so (a list-page URL, a uuid that does not recompute, an incomplete section, a withdrawn
  legacy record, an unknown site) is left pending with its reason, never imported on a guess.
- The imported raw is an explicit migration envelope: {"legacy_migration": {...}, <parser-readable body>}. The
  block names the converter, the legacy version and record, the immutable legacy raw hash and original raw
  hash, the mapping basis and what the old system claimed (crawl / created / updated times). The body is only
  what the real source's parser reads: the embedded API sections as they were (CT), or a legacy_capture
  (site_crawler.project_capture), never presented as a native API response or a web capture.
- Time: the migrated version's first_seen is the actual import time; no availability proof is made (the
  import carries none); the old claims stay claims, in facts.legacy_migration.
- A migrated version never takes the head from a native version of the same record (db.history._LATEST:
  native first, after the mode/cutoff filter); a record with only migrated versions says so in its facts.
- Same content is not merged: every legacy version keeps its own migrated version; a CT version whose sections
  equal those of a native version gets that version as content_equivalent, a relation, nothing more.
- legacy_aliases records, per legacy version, the outcome: imported (real source, record, version) or pending.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy import text

from biz_rules.history_models import ArchiveImport, ArchiveRecord, canonical

CONVERTER = "datafabric.legacy_migration.v1"
CAPTURE_SCHEMA = "datafabric.site.legacy_capture.v1"
WITHDRAWN = {"withdrawn", "deleted", "retracted"}  # as db.retrieval reads publication_state
SECTION_LINE = re.compile(r"^\*\*([A-Za-z]+Section|hasResults|derivedSection)\*\*: (.*)$", re.M)
NCT = re.compile(r"^NCT\d{8}$")
# scalar section values as the old API router wrote them (str() of the JSON value); recorded in the block
SCALARS = {"hasResults": {"True": True, "False": False}}


def front_matter(markdown: str) -> tuple[dict, str]:
    """The old crawler's YAML head (simple "key: value" lines) and the body after it."""
    if not markdown.startswith("---\n"):
        return {}, markdown
    end = markdown.find("\n---\n", 4)
    if end < 0:
        return {}, markdown
    meta = {}
    for line in markdown[4:end].split("\n"):
        key, sep, value = line.partition(":")
        if sep and key.strip():
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            meta[key.strip()] = value
    return meta, markdown[end + 5 :]


def of_site(url: str, pattern: str) -> bool:
    """The URL is one of the site's own pages by its parsed parts: http(s), no user info, the default
    port, a hostname that is the site's host or a subdomain of it, and a path under the site's detail path when
    the site names one ("chictr.org.cn/showproj"). A site URL carried in the query, fragment or user info of
    another page proves nothing."""
    if not url or not pattern:
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host, _, path = pattern.partition("/")
    hostname = (parts.hostname or "").lower()
    return (parts.scheme in ("http", "https") and not parts.username and not parts.password
            and port in (None, 80, 443) and (hostname == host or hostname.endswith("." + host))
            and (not path or parts.path.startswith("/" + path)))


def _uuid5(value: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


def _field(markdown: str, name: str) -> str | None:
    """A record field as the API router wrote it ("**name**: value", one line); None when absent or ambiguous."""
    found = re.findall(r"^\*\*" + re.escape(name) + r"\*\*: (.*)$", markdown, re.M)
    return found[0] if len(found) == 1 else None


def _sections(markdown: str):
    """The embedded ClinicalTrials.gov API sections, each a complete JSON value; None if any is broken."""
    out = {}
    for name, value in SECTION_LINE.findall(markdown):
        if name in out:
            return None
        if name in SCALARS:
            # the old API router wrote non-container values with Python str(): True / False exactly
            if value not in SCALARS[name]:
                return None
            out[name] = SCALARS[name][value]
            continue
        try:
            out[name] = json.loads(value)
        except ValueError:
            return None
        if not isinstance(out[name], dict):
            return None
    return out or None


def plan(payload: dict) -> dict:
    """A legacy version's payload -> {status: ready | pending, reason, basis, real_source_id, real_record_id,
    record_kind, envelope, block}. Pure: reads nothing but the version itself and the site rules."""
    from collection.catalog import SITE_SOURCES
    from collection.site_crawler import SITE_PROFILES

    doc = json.loads(payload["raw_content"])
    site = doc.get("source_site") or ""
    claimed = {
        "crawl_or_created_at": doc.get("original_created_at"),
        "updated_at": doc.get("original_updated_at"),
        "publish_date": doc.get("publish_date_raw"),
        "state_observed_at": doc.get("state_observed_at"),
    }
    block = {
        "converter": CONVERTER,
        "legacy_source_id": payload["source_id"],
        "legacy_record_id": payload["source_record_id"],
        "legacy_version_id": payload["version_id"],
        "legacy_raw_sha256": payload["raw_sha256"],
        "original_raw_sha256": doc.get("original_raw_sha256"),
        "legacy_doc_uuid": doc.get("source_doc_uuid"),
        "source_site": site,
        "claimed": claimed,
    }
    out = {"status": "pending", "reason": None, "basis": None, "real_source_id": None, "real_record_id": None,
           "record_kind": None, "envelope": None, "block": block}
    if doc.get("publication_state") == "withdrawn":
        return dict(out, reason="legacy_withdrawn")  # a deletion notice carries no content to migrate
    markdown = doc.get("original_raw_content") or ""
    meta, body = front_matter(markdown)
    if site == "clinicaltrials":
        sections = _sections(markdown)
        nct = (((sections or {}).get("protocolSection") or {}).get("identificationModule") or {}).get("nctId")
        if not sections or not isinstance(sections.get("protocolSection"), dict):
            return dict(out, reason="embedded_json_incomplete")
        if not isinstance(nct, str) or not NCT.match(nct):
            return dict(out, reason="nct_not_found")
        block = dict(block, basis="embedded_api_sections", sections=sorted(sections),
                     scalar_rendering={k: "python_str" for k in SCALARS if k in sections})
        return dict(out, status="ready", basis="embedded_api_sections", real_source_id="clinicaltrials",
                    real_record_id=nct, record_kind="trial", block=block,
                    envelope={"legacy_migration": block, **sections})
    if site not in SITE_SOURCES:
        return dict(out, reason="source_unknown")
    profile = SITE_PROFILES.get(site) or {}
    legacy_uuid = doc.get("source_doc_uuid") or ""
    url = doc.get("source_url") or ""
    if profile.get("uid_field"):
        key = _field(markdown, profile["uid_field"])
        if key is None:
            return dict(out, reason="record_key_not_found")
        if _uuid5(site + ":" + key) != legacy_uuid:
            return dict(out, reason="uuid_not_reproducible")
        record_id, basis, kind = legacy_uuid, "uuid5_record_key", "record"
    else:
        if not of_site(url, profile.get("url_pattern") or ""):
            return dict(out, reason="url_not_of_site")
        if _uuid5(url) != legacy_uuid:
            return dict(out, reason="detail_url_not_proven")
        record_id, basis, kind = url, "uuid5_detail_url", "page"
    block = dict(block, basis=basis)
    capture = {
        "schema_version": CAPTURE_SCHEMA,
        "role": "legacy_capture",
        "kind": kind,
        "site": site,
        "record_id": record_id,
        "url": url or None,
        "parsed": {"title": doc.get("title") or meta.get("title") or "", "markdown": body.strip(),
                   "publish_date": meta.get("publish_date") or doc.get("publish_date_raw")},
    }
    return dict(out, status="ready", basis=basis, real_source_id=site, real_record_id=record_id,
                record_kind="document", block=block, envelope={"legacy_migration": block, **capture})


def _equivalent_ct(conn, record_id, is_demo, sections):
    """A native CT version of the record whose embedded sections are the same JSON (content_equivalent only)."""
    want = canonical(sections)
    rows = conn.execute(text("""
        SELECT version_id, payload->>'raw_content' AS raw FROM history_versions
        WHERE source_id='clinicaltrials' AND source_record_id=:r AND is_demo=:d
          AND COALESCE(payload->'facts'->>'publication_role','source_record') <> 'migrated_record'
        ORDER BY published_seq LIMIT 50"""), {"r": record_id, "d": is_demo}).mappings()
    for row in rows:
        try:
            raw = json.loads(row["raw"])
        except ValueError:
            continue
        if canonical({k: raw.get(k) for k in sections}) == want:
            return row["version_id"]
    return None


def migrate(*, is_demo: bool, after_seq: int = 0, limit: int = 200, apply: bool = False) -> dict:
    """One page of legacy versions (by published_seq) -> plans; with apply=True each ready one is imported
    into its real source (idempotent: the same envelope is the same version) and legacy_aliases is written.
    Returns counts by outcome and reason, and the last seq read (to continue)."""
    from db import history

    counts, last = {}, after_seq
    with history.history_engine().begin() as conn:
        rows = conn.execute(text("""
            SELECT published_seq, payload FROM history_versions WHERE source_id='legacy_documents'
              AND is_demo=:d AND published_seq>:after
              AND COALESCE(payload->'facts'->>'publication_role','source_record')='source_record'
            ORDER BY published_seq LIMIT :n"""), {"d": is_demo, "after": after_seq, "n": limit}).mappings().all()
    plans = []
    for row in rows:
        last = row["published_seq"]
        p = plan(row["payload"])
        plans.append(p)
        key = p["status"] if p["status"] == "ready" else "pending:" + p["reason"]
        counts[key] = counts.get(key, 0) + 1
    if not apply:
        return {"read": len(rows), "last_seq": last, "counts": counts}
    imported = 0
    for p in plans:
        real_version, equivalent = None, None
        if p["status"] == "ready":
            request = ArchiveImport(
                source_id=p["real_source_id"],
                is_demo=is_demo,
                scope_description="v3 6.3 legacy migration of one legacy version (" + CONVERTER + ")",
                declared_count=1,
                declared_complete=False,
                records=[ArchiveRecord(source_record_id=p["real_record_id"], record_kind=p["record_kind"],
                                       raw_content=json.dumps(p["envelope"], ensure_ascii=False, sort_keys=True))],
            )
            run = history.import_archive(request, "legacy-migration:" + p["block"]["legacy_version_id"])
            real_version = run.version_ids[0]
            imported += 1
            if p["real_source_id"] == "clinicaltrials":
                with history.history_engine().begin() as conn:
                    sections = {k: v for k, v in p["envelope"].items() if k != "legacy_migration"}
                    equivalent = _equivalent_ct(conn, p["real_record_id"], is_demo, sections)
        with history.history_engine().begin() as conn:
            conn.execute(text("""
                INSERT INTO legacy_aliases(legacy_version_id,legacy_record_id,is_demo,source_site,status,reason,
                  basis,real_source_id,real_record_id,real_version_id,equivalent_version_id,claimed,converter)
                VALUES (:v,:r,:d,:site,:status,:reason,:basis,:rs,:rr,:rv,:eq,CAST(:claimed AS jsonb),:conv)
                ON CONFLICT (legacy_version_id) DO UPDATE SET status=EXCLUDED.status,reason=EXCLUDED.reason,
                  basis=EXCLUDED.basis,real_source_id=EXCLUDED.real_source_id,real_record_id=EXCLUDED.real_record_id,
                  real_version_id=EXCLUDED.real_version_id,equivalent_version_id=EXCLUDED.equivalent_version_id
                WHERE legacy_aliases.status <> 'imported'"""), {
                "v": p["block"]["legacy_version_id"], "r": p["block"]["legacy_record_id"], "d": is_demo,
                "site": p["block"]["source_site"], "status": "imported" if real_version else "pending",
                "reason": p["reason"], "basis": p["basis"], "rs": p["real_source_id"], "rr": p["real_record_id"],
                "rv": real_version, "eq": equivalent, "claimed": json.dumps(p["block"]["claimed"]),
                "conv": CONVERTER})
    return {"read": len(rows), "last_seq": last, "counts": counts, "imported": imported,
            "at": datetime.now(timezone.utc).isoformat()}


def alias(conn, version_id: str, mode: str, cutoff) -> dict | None:
    """What a legacy version became, checked now (db.history.read_version adds it to the legacy read):
    pending (with its reason; no access is widened), or imported: the real version, whether that version is
    visible at this mode and cutoff, the real record's current version there and whether it is withdrawn.
    The real source's permission is checked by the caller first (a revoked real source blocks the old id)."""
    from db import history

    row = conn.execute(text("SELECT * FROM legacy_aliases WHERE legacy_version_id=:v"), {"v": version_id}).mappings()
    row = row.first()
    if row is None:
        return None
    out = {"status": row["status"], "converter": row["converter"], "claimed": row["claimed"]}
    if row["status"] != "imported":
        return dict(out, reason=row["reason"])
    target = {"source_id": row["real_source_id"], "source_record_id": row["real_record_id"],
              "version_id": row["real_version_id"], "basis": row["basis"],
              "content_equivalent_version_id": row["equivalent_version_id"]}
    visible = conn.execute(text(
        "SELECT 1 FROM history_versions WHERE version_id=:v AND " + history._eligibility(mode)),
        {"v": row["real_version_id"], "cutoff": cutoff}).first() is not None
    head = conn.execute(text(
        history._ranked("source_id=:s AND is_demo=:d AND source_record_id=:r", mode)
        + "SELECT version_id, payload->'facts'->>'publication_state' AS state, "
        + history._ROLE + " AS role FROM ranked WHERE n=1"),
        {"s": row["real_source_id"], "d": row["is_demo"], "r": row["real_record_id"], "cutoff": cutoff}).mappings().first()
    return dict(out, target=target, target_visible=visible,
                record_current_version_id=head["version_id"] if head else None,
                record_current_is_migrated=bool(head) and head["role"] == "migrated_record",
                record_withdrawn=bool(head) and head["state"] in WITHDRAWN)
