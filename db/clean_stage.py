"""v3 3.3 清洗段的运行入口：每个版本一次，确定性、可重跑、只追加。

每个还没有画像（derived_profiles，当前画像处理器版本）的版本，在一个事务里写：
1. 清洗正文（derived_clean_text，processor_version=datafabric.clean_text.v1）：
   - clinicaltrials：登记字段逐字段成段（JSON 路径定位），简介在前；
   - europe_pmc：原件 JSON 的标题 + 摘要（偏移回到原件 JSON）；
   - nmpa / cde / chictr 的网页版本：旧提取器解析出的 Markdown（偏移回到原件 JSON 里的 /parsed/markdown）；
   - 附件：不另清洗，正文用加工层 attachment_parse 的最新结果；还没解析完（没有成功/不支持、失败未满次数）
     就先不画像，等下一轮；
   - 其他来源：投影里的 clean_text 按 Markdown 规则清洗（偏移回到投影正文，写明定位基准）。
2. 编号（derived_identifiers）：在这份正文上抽取，偏移指这份正文。
3. 画像（derived_profiles）：分类、四维质量、分流（processing.profile）。
图谱等派生来源不在这一层处理。
"""

from __future__ import annotations

import hashlib
import json
import re
import time

from sqlalchemy import text

from biz_rules.history_models import HistoryError
from processing import attachment_parse, clean_text, identifiers, profile

SKIP_SOURCES = ("reviewed_graph", "primekg_graph")
SITE_SOURCES = ("nmpa", "cde", "chictr")
REGISTRY_IDS = {"nct", "ctr", "chictr"}
REGISTRY_SOURCES = {"clinicaltrials", "chictr", "cde_ctr"}


def _attachment_text(conn, version_id):
    """附件当前处理器版本下的最终结果；还在重试中返回 None（等）。"""
    from db.derived_text import MAX_ATTEMPTS

    rows = conn.execute(text(
        "SELECT status,text,attempt,locator_kind,segments,error FROM derived_clean_text WHERE version_id=:v "
        "AND processor_version=:pv ORDER BY attempt DESC"
    ), {"v": version_id, "pv": attachment_parse.PROCESSOR_VERSION}).mappings().all()
    if not rows:
        return None
    latest = rows[0]
    if latest["status"] in ("parsed", "unsupported") or latest["attempt"] >= MAX_ATTEMPTS:
        return dict(latest)
    return None


def _clean(payload):
    """(清洗结果, 定位基准)；定位基准写明 segments 的 source 偏移相对于什么。"""
    source = payload["source_id"]
    facts = payload.get("facts") or {}
    if source == "clinicaltrials":
        return clean_text.clean_registry(facts, payload.get("field_locators") or {},
                                         summary=payload.get("clean_text")), "registry_fields"
    if source == "europe_pmc":
        return clean_text.clean_literature(payload.get("raw_content") or ""), "raw_json"
    if source in SITE_SOURCES and facts.get("role") == "web_capture":
        try:
            markdown = (json.loads(payload["raw_content"]).get("parsed") or {}).get("markdown") or ""
        except ValueError:
            markdown = ""
        return clean_text.clean_markdown(markdown, source_id=source), "raw_json:/parsed/markdown"
    return clean_text.clean_markdown(payload.get("clean_text") or "", source_id=source), "projection_clean_text"


def _pending(conn, batch):
    return conn.execute(text("""
        SELECT v.version_id, v.source_id, v.is_demo, v.source_record_id, v.payload
        FROM history_versions v
        WHERE v.record_kind <> 'graph' AND NOT (v.source_id = ANY(:skip))
          AND NOT EXISTS (SELECT 1 FROM derived_profiles p WHERE p.version_id=v.version_id AND p.processor_version=:pv
                          AND p.text_processor_version = CASE WHEN v.payload->'facts'->>'role' = 'attachment'
                                                              THEN :att ELSE :clean END)
        ORDER BY v.published_seq LIMIT :batch
    """), {"skip": list(SKIP_SOURCES), "pv": profile.PROCESSOR_VERSION, "batch": batch,
           "att": attachment_parse.PROCESSOR_VERSION, "clean": clean_text.PROCESSOR_VERSION}).mappings().all()


def _insert_clean(conn, row, result, basis):
    body = result["text"]
    conn.execute(text("""
        INSERT INTO derived_clean_text
          (version_id,processor_version,attempt,source_id,is_demo,source_record_id,status,parse_method,parse_attempts,
           error,file_ext,text,chars,text_sha256,locator_kind,segments,language,warnings)
        SELECT :v,:pv,1,:source,:demo,:native,:status,:method,'[]'::jsonb,:error,NULL,:text,:chars,:sha,:kind,
               CAST(:segments AS jsonb),:lang,CAST(:warnings AS jsonb)
        WHERE NOT EXISTS (SELECT 1 FROM derived_clean_text WHERE version_id=:v AND processor_version=:pv)
    """), {
        "v": row["version_id"], "pv": result["processor_version"], "source": row["source_id"],
        "demo": row["is_demo"], "native": row["source_record_id"],
        "status": "parsed" if body else "failed", "method": basis,
        "error": None if body else ",".join(result.get("warnings") or ["no_body"]),
        "text": body, "chars": len(body), "sha": hashlib.sha256(body.encode()).hexdigest(),
        "kind": "json_pointer" if basis == "registry_fields" else "char_span",
        "segments": json.dumps(result.get("segments") or [], ensure_ascii=False),
        "lang": result.get("language"), "warnings": json.dumps(result.get("warnings") or []),
    })


def process(*, batch=200, time_budget_seconds=600):
    """清洗一批版本。返回计数：profiled（画像写入数）、waiting_for_attachment_text（附件还没解析完）。"""
    from db.history import history_engine

    engine = history_engine()
    began = time.monotonic()
    counts = {"profiled": 0, "waiting_for_attachment_text": 0, "quarantine": 0, "weak_signal": 0}
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout='30000ms'"))
        todo = _pending(conn, batch)
    for row in todo:
        if time.monotonic() - began >= time_budget_seconds:
            break
        payload = row["payload"]
        facts = payload.get("facts") or {}
        role = facts.get("role")
        with engine.begin() as conn:
            if role == "attachment":
                done = _attachment_text(conn, row["version_id"])
                if done is None:
                    counts["waiting_for_attachment_text"] += 1
                    continue
                body = done["text"] if done["status"] == "parsed" else ""
                text_pv, warnings, parse_status = attachment_parse.PROCESSOR_VERSION, [], done["status"]
            else:
                result, basis = _clean(payload)
                _insert_clean(conn, row, result, basis)
                body, text_pv, warnings, parse_status = (result["text"], result["processor_version"],
                                                         result.get("warnings") or [], None)
            found = identifiers.extract(body) if body else []
            for hit in found:
                conn.execute(text("""
                    INSERT INTO derived_identifiers
                      (version_id,processor_version,text_processor_version,kind,value,start_offset,end_offset)
                    SELECT :v,:pv,:tpv,:kind,:value,:s,:e
                    WHERE NOT EXISTS (SELECT 1 FROM derived_identifiers WHERE version_id=:v AND processor_version=:pv
                                      AND text_processor_version=:tpv AND kind=:kind AND value=:value
                                      AND start_offset=:s)
                """), {"v": row["version_id"], "pv": identifiers.PROCESSOR_VERSION, "tpv": text_pv,
                       "kind": hit["kind"], "value": hit["value"], "s": hit["start"], "e": hit["end"]})
            pub = facts.get("publication_types")
            pub_types = pub.get("pubType") if isinstance(pub, dict) else pub
            prof = profile.profile(source_id=row["source_id"], role=role, chars=len(body), warnings=warnings,
                                   identifiers=found, pub_types=pub_types, parse_status=parse_status)
            inserted = conn.execute(text("""
                INSERT INTO derived_profiles
                  (version_id,processor_version,text_processor_version,source_id,is_demo,doc_type,evidence_types,
                   decisions,quality,route,route_reasons)
                SELECT :v,:pv,:tpv,:source,:demo,:doc,CAST(:ev AS jsonb),CAST(:dec AS jsonb),CAST(:q AS jsonb),
                       :route,CAST(:reasons AS jsonb)
                WHERE NOT EXISTS (SELECT 1 FROM derived_profiles WHERE version_id=:v AND processor_version=:pv
                                  AND text_processor_version=:tpv)
            """), {"v": row["version_id"], "pv": prof["processor_version"], "tpv": text_pv,
                   "source": row["source_id"], "demo": row["is_demo"], "doc": prof["doc_type"],
                   "ev": json.dumps(prof["evidence_types"]), "dec": json.dumps(prof["decisions"]),
                   "q": json.dumps(prof["quality"], ensure_ascii=False), "route": prof["route"],
                   "reasons": json.dumps(prof["route_reasons"], ensure_ascii=False)}).rowcount
        if inserted:
            counts["profiled"] += 1
            if prof["route"] in counts:
                counts[prof["route"]] += 1
    return counts


def for_versions(source_id, is_demo, version_ids, mode, cutoff):
    """一页版本的清洗正文、编号和画像。版本须在本次模式和截止时间下可见。"""
    from db import history

    if not 1 <= len(version_ids) <= 100 or any(not re.fullmatch(r"[0-9a-f]{64}", v) for v in version_ids):
        raise HistoryError("query_budget", "一次最多 100 个合法版本号", 422)
    eligible = history._eligibility(mode).replace("first_seen_at", "v.first_seen_at").replace(
        "proof_available_at", "v.proof_available_at")
    with history.history_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        history._source(conn, source_id)
        cutoff = history._cutoff(cutoff)
        if cutoff > history._now(conn):
            raise HistoryError("future_cutoff", "证据截止时间不能晚于服务器当前时间")
        params = {"source": source_id, "demo": is_demo, "ids": list(version_ids), "cutoff": cutoff,
                  "pvs": [clean_text.PROCESSOR_VERSION, attachment_parse.PROCESSOR_VERSION],
                  "prof": profile.PROCESSOR_VERSION, "idpv": identifiers.PROCESSOR_VERSION}
        meta = {r["version_id"]: dict(r) for r in conn.execute(text(
            "SELECT v.version_id,v.source_record_id,v.first_seen_at,v.proof_available_at FROM history_versions v "
            "WHERE v.source_id=:source AND v.is_demo=:demo AND v.version_id=ANY(:ids) AND " + eligible),
            params).mappings()}
        visible = set(meta)
        cleans = conn.execute(text(
            "SELECT DISTINCT ON (version_id) version_id,processor_version,text,text_sha256,segments,language,warnings,"
            "locator_kind,status FROM derived_clean_text WHERE version_id=ANY(:ids) AND processor_version=ANY(:pvs) "
            "ORDER BY version_id, attempt DESC"), params).mappings().all()  # 只有当前清洗/附件解析版本
        ids = conn.execute(text(
            "SELECT version_id,text_processor_version,kind,value,start_offset,end_offset FROM derived_identifiers "
            "WHERE version_id=ANY(:ids) AND processor_version=:idpv ORDER BY version_id,start_offset"),
            params).mappings().all()
        profiles = conn.execute(text(
            "SELECT version_id,doc_type,evidence_types,decisions,quality,route,route_reasons,text_processor_version "
            "FROM derived_profiles WHERE version_id=ANY(:ids) AND processor_version=:prof"), params).mappings().all()
    out = {v: {"state": "not_visible_or_not_processed"} for v in version_ids}
    for v in visible:
        out[v] = {"state": "not_processed", **meta[v]}
    for c in cleans:
        if c["version_id"] in visible:
            out[c["version_id"]] = {"state": "processed", **meta[c["version_id"]],
                                    "clean": {k: c[k] for k in c if k != "version_id"},
                                    "identifiers": [], "profile": None}
    # 编号和画像只取在“这份”清洗正文上算出来的（同一版本可能有旧处理器版本留下的行）
    for i in ids:
        entry = out.get(i["version_id"])
        if entry and entry.get("state") == "processed" and i["text_processor_version"] == entry["clean"][
                "processor_version"]:
            entry["identifiers"].append({k: i[k] for k in i if k not in ("version_id", "text_processor_version")})
    for p in profiles:
        entry = out.get(p["version_id"])
        if entry and entry.get("state") == "processed" and p["text_processor_version"] == entry["clean"][
                "processor_version"]:
            entry["profile"] = {k: p[k] for k in p if k != "version_id"}
    return {"schema_version": "datafabric.clean_stage.v1", "source_id": source_id, "mode": mode,
            "as_of": cutoff.isoformat(), "versions": out,
            "limitations": ["编号是文本抽取，宁漏勿误；结构化来源的原生编号以原件字段为准",
                            "entity_match / fact_check 在这一层未评估，不代表没有问题"]}


def identifier_links(version_id):
    """和这个版本共享编号的其他版本。relation（从这个版本的角度）：
    - mentions_record：我提到了对方的编号，对方就是这个编号的记录本身（如论文摘要写了 CT 的 NCT 号）；
    - mentioned_by：对方提到了我的编号；
    - identity_candidate：两个不同登记平台的记录都写着同一个登记号（CT 的 secondary_ids 写了 ChiCTR 号，
      ChiCTR 页面也写着它）——只是身份候选，审核后才能建立关联；
    - same_identifier：其他共享编号的情况（如同一 DOI 出现在两份材料里），用于同源去重，不删任何一份。"""
    from db.history import history_engine

    with history_engine().begin() as conn:
        mine = conn.execute(text("SELECT source_id, source_record_id FROM history_versions WHERE version_id=:v"),
                            {"v": version_id}).mappings().one()
        rows = conn.execute(text("""
            SELECT DISTINCT o.version_id, h.source_id, h.source_record_id, o.kind, o.value
            FROM derived_identifiers m JOIN derived_identifiers o ON o.kind=m.kind AND o.value=m.value
              AND o.version_id<>m.version_id AND o.processor_version=m.processor_version
            JOIN history_versions h ON h.version_id=o.version_id
            WHERE m.version_id=:v AND m.processor_version=:pv
        """), {"v": version_id, "pv": identifiers.PROCESSOR_VERSION}).mappings().all()
    out = []
    for r in rows:
        value = r["value"].upper()
        mine_is = mine["source_record_id"].upper() == value
        other_is = r["source_record_id"].upper() == value
        if other_is and not mine_is:
            relation = "mentions_record"
        elif mine_is and not other_is:
            relation = "mentioned_by"
        elif (r["kind"] in REGISTRY_IDS and mine["source_id"] in REGISTRY_SOURCES
              and r["source_id"] in REGISTRY_SOURCES and r["source_id"] != mine["source_id"]):
            relation = "identity_candidate"
        else:
            relation = "same_identifier"
        out.append({"version_id": r["version_id"], "source_id": r["source_id"], "kind": r["kind"],
                    "value": r["value"], "relation": relation})
    return out


def to_contract(entry, *, source_id):
    """for_versions 的一项 → v3 第二部分契约对象（contracts.data_objects.CleanText）。"""
    from contracts import data_objects as do

    if entry.get("state") != "processed":
        raise ValueError("没有清洗结果，不能构成契约对象")
    clean = entry["clean"]
    version = entry["version_id"]
    segments = clean.get("segments") or []
    first = segments[0] if segments else {}
    src = first.get("source") or {}
    if src.get("json_pointer"):
        locator = do.Locator(kind="json_pointer", version_id=version, json_pointer=src["json_pointer"])
    elif "start" in src and "end" in src:
        locator = do.Locator(kind="char_span", version_id=version, start=src["start"], end=src["end"])
    elif first.get("page") and clean.get("locator_kind") in ("pdf_page", "converted_page"):
        locator = do.Locator(kind=clean["locator_kind"], version_id=version, page=first["page"],
                             derived_text_sha256=clean["text_sha256"])
    elif "block" in first:
        locator = do.Locator(kind="block", version_id=version, block=first["block"],
                             derived_text_sha256=clean["text_sha256"])
    elif "sheet" in first:
        locator = do.Locator(kind="sheet", version_id=version, sheet=first["sheet"],
                             derived_text_sha256=clean["text_sha256"])
    else:
        locator = do.Locator(kind="unavailable", version_id=version)
    ok = clean.get("status") == "parsed"
    return do.CleanText(
        inputs=[do.VersionRef(source_id=source_id, record_id=entry["source_record_id"], version_id=version)],
        processing=do.Processing(processor_version=clean["processor_version"]),
        locators=[locator],
        permission_sources=[source_id],
        visibility=do.Visibility(first_seen_at=entry["first_seen_at"],
                                 proof_available_at=entry.get("proof_available_at")),
        status=do.Status(state="ok") if ok else do.Status(state="failed", reason="没有可用正文"),
        text_sha256=clean["text_sha256"],
        chars=len(clean.get("text") or ""),
        segments=len(segments),
        language=clean.get("language") if clean.get("language") in ("zh", "en", "mixed") else "unknown",
    )
