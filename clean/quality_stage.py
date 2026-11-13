"""质量段：给画像过的版本打徐子雯的五维质量分，编号对齐、字段清洗结果和血缘一起存下。
雷达读的清洗正文、编号、画像一个字不动；分数只追加；“隔离”只是等级，不拦截，所有版本照常建索引、给雷达。

从哪读起：process()。它一批一批地取“有当前画像、还没有当前评分器版本分数”的版本（新抓到的先做），每个版本：
  document()   版本 → 一份资料 doc（键和徐子雯原来的 parsed_result 一样；markdown 就是这个版本的清洗正文）
  score_one()  记“解析”血缘 → 字段清洗（clean/fields.py，记“清洗”血缘）→ L3 规范化（记“规范化”血缘）
               → 编号对齐（clean/align.py，药名要联网，记“对齐”血缘）→ 五维打分和结构校验（clean/quality.py）
  _write()     写 derived_quality_scores（一个版本、一个评分器版本一行）和 derived_authority_resolutions
               （一个版本、一个对齐版本下，每个药名、每个编号字段一行）
联网只在对齐，放在数据库事务外面；一轮有时间上限。这个版本在当前对齐版本下已经对齐过（库里有行）就用库里的，不再联网：
只换评分口径（升 SCORER_VERSION）时不用重新查网。多个进程同时跑可能重复查网，但表里不会重复（主键）。
process_versions() 只做指定的几个版本（main_etl.py 用）；scores_for() 把一页版本的分数读给
/api/history/clean-text?quality=true。常驻：run/clean.py --loop 每轮清洗完顺带跑一次。
"""

from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from sqlalchemy import text

from clean import align, attachment_parse, clean_text, drug_names, fields, profile, quality
from store.history_models import make_uuid

BATCH = 50
CT_DRUG_TYPES = {"DRUG", "BIOLOGICAL", "COMBINATION_PRODUCT"}  # ClinicalTrials 干预里算药名的类型

_FIELDS = """
    SELECT v.version_id, v.source_id, v.is_demo, v.source_record_id, v.payload->>'title' AS title,
           v.payload->>'source_url' AS url, v.payload->'facts' AS facts, v.payload->>'parser_version' AS parser,
           v.payload->'source_published_at'->>'value' AS published,
           v.payload->'source_updated_at'->>'value' AS updated,
           CASE WHEN v.source_id = 'clinicaltrials' THEN v.payload->>'clean_text' END AS summary,
           CASE WHEN v.source_id = 'pubmed' THEN v.payload->>'raw_content' END AS raw,
           p.text_processor_version AS text_pv,
           (SELECT d.text FROM derived_clean_text d WHERE d.version_id = v.version_id
              AND d.processor_version = p.text_processor_version ORDER BY d.attempt DESC LIMIT 1) AS text
    FROM history_versions v
    JOIN derived_profiles p ON p.version_id = v.version_id AND p.processor_version = :prof
                           AND p.text_processor_version = ANY(:texts)
"""
_SCORED = "EXISTS (SELECT 1 FROM derived_quality_scores q WHERE q.version_id = v.version_id AND q.scorer_version = :sv)"
# 领活分两步：先只挑版本号（不碰 payload，排序时不用把每个版本的原件都读一遍），再按版本号取字段
_TODO_SQL = """
    SELECT v.version_id FROM history_versions v
    JOIN derived_profiles p ON p.version_id = v.version_id AND p.processor_version = :prof
                           AND p.text_processor_version = ANY(:texts)
    WHERE NOT """ + _SCORED + """ AND NOT (v.version_id = ANY(:skip))
    ORDER BY v.first_seen_at DESC, v.version_id LIMIT :n"""
_BY_IDS_SQL = _FIELDS + " WHERE v.version_id = ANY(:ids) AND NOT " + _SCORED


def _params(**more):
    return {"prof": profile.PROCESSOR_VERSION, "texts": [clean_text.PROCESSOR_VERSION, attachment_parse.PROCESSOR_VERSION],
            "sv": quality.SCORER_VERSION, **more}


# ───────────── 版本 → 一份资料 ─────────────

def document(row) -> dict:
    """一个版本 → 评分要的一份资料。uuid 和徐子雯旧库同一算法（网页 uuid5(网址)、接口记录 uuid5(站点:记录键)），
    分数能和她旧库的文档对上。字段从投影里取（facts 里站点抽取器给的 extra / structured），不改写。"""
    facts = row["facts"] or {}
    source, role, title = row["source_id"], facts.get("role"), row["title"] or ""
    published = row["published"] or ""
    doc = {"uuid": row["source_record_id"], "source_url": row["url"] or "", "title": title,
           "source_site": (facts.get("source_site") or source) if source == "legacy_documents" else source,
           "publish_date": published, "markdown": row["text"] or "",
           "parse_method": facts.get("parse_method") or row["parser"] or "history_projection",
           "structured_data": {"title": title, "publish_date": published, "category": "", "summary": ""},
           "extra": dict(facts.get("extra") or {}), "sniffed_meta": {}}
    if role == "web_capture":
        doc["uuid"] = make_uuid(row["source_record_id"])  # 网页版本的记录号就是网址
    elif role == "site_record":
        doc["structured_data"] = dict(facts.get("structured") or {})
    elif role == "attachment":
        _attachment(doc, row)
    elif source == "clinicaltrials":
        _clinicaltrials(doc, row, facts)
    elif source in ("europe_pmc", "pubmed"):
        _literature(doc, row, facts)
    elif source == "fda_crl":
        _fda_crl(doc, facts)
    return doc


def _fda_crl(doc, facts):
    """FDA 完整回应函：字段照她 schemas 里的 FdaCrlExtra。"""
    apps = facts.get("application_numbers")
    doc["extra"] = {"application_number": "; ".join(apps) if isinstance(apps, list) else str(apps or ""),
                    **{k: str(facts.get(k) or "") for k in ("letter_date", "letter_type", "file_name")}}


def _attachment(doc, row):
    """附件（PDF 等）：标题取正文第一个 # 标题（没有就空），DOI、期刊名从正文头部嗅（徐子雯 route_pdf 的做法）。"""
    meta = fields.sniff_pdf_meta(doc["markdown"])
    doc.update(title=meta["sniffed_title"], publish_date="", parse_method=row["text_pv"] or "attachment_parse",
               sniffed_meta={"doi": meta["doi"], "journal_name": meta["journal_name"]},
               structured_data={"title": meta["sniffed_title"], "publish_date": "", "category": "", "summary": ""})


def _names(items, key):
    return [str(item.get(key, "")).strip() for item in items or [] if isinstance(item, dict) and item.get(key)]


def _clinicaltrials(doc, row, facts):
    """ClinicalTrials：照她 config/sites/ClinicalTrials.py 的字段映射。发布日期用最近更新公布日，药名取药物类干预。"""
    interventions = facts.get("interventions") or []
    drugs = [i["name"].strip() for i in interventions
             if isinstance(i, dict) and i.get("type") in CT_DRUG_TYPES and i.get("name")]
    updated = row["updated"] or ""
    summary = row["summary"] if row["summary"] and row["summary"] != doc["title"] else ""
    doc.update(uuid=make_uuid("clinicaltrials:" + row["source_record_id"]), publish_date=updated)
    doc["structured_data"] = {"title": doc["title"], "publish_date": updated, "category": "临床试验",
                              "summary": summary[:500], "drug_name": "; ".join(drugs), "manufacturer": ""}
    doc["extra"] = {
        "nct_id": row["source_record_id"], "study_type": facts.get("study_type") or "",
        "phase": list(facts.get("phase") or []), "study_status": facts.get("status") or "",
        "enrollment": facts.get("enrollment"), "study_completion_date": facts.get("completion_date") or "",
        "primary_completion_date": facts.get("primary_completion_date") or "",
        "sponsor": (facts.get("lead_sponsor") or {}).get("name") or "",
        "collaborators": _names(facts.get("collaborators"), "name"), "conditions": list(facts.get("conditions") or []),
        "interventions": _names(interventions, "name"), "arm_groups": _names(facts.get("arm_groups"), "label")}


def _literature(doc, row, facts):
    """Europe PMC、PubMed：编号和期刊名放进 extra（她的 EuropePmcExtra / PubmedExtra），DOI 和期刊名也给权威分用；
    类别照她 PubMed 抓取器写“医学文献”。主线清洗正文只有标题和摘要（她原版正文里还有一行行元数据）。"""
    journal = ((facts.get("journal") or {}).get("journal") or {}).get("title") or ""
    if row.get("raw"):  # PubMed 的期刊名只在原件 XML 里
        try:
            journal = ET.fromstring(row["raw"]).findtext("./MedlineCitation/Article/Journal/Title") or ""
        except ET.ParseError:
            journal = ""
    doi = facts.get("doi") or ""
    doc["structured_data"].update(category="医学文献", summary=str(facts.get("abstract") or "")[:500])
    doc["extra"] = {"pmid": facts.get("pmid") or "", "pmcid": facts.get("pmcid") or "", "doi": doi,
                    "journal_name": journal, "fulltext_status": "abstract_only"}
    doc["sniffed_meta"] = {"doi": doi, "journal_name": journal}


# ───────────── 一个版本：洗字段、对齐、打分 ─────────────

def _step(lineage, doc, step, op, before):
    """这一步对每个追踪字段记一条 {step, op, input, output}（她 lineage_tracer.record_step 的结构）。"""
    for field, value in fields.traced(doc).items():
        lineage.setdefault(field, []).append({"step": step, "op": op, "input": before.get(field, ""), "output": value})


def _note(lineage, field, step, op, old, new):
    lineage.setdefault(field, []).append({"step": step, "op": op, "input": old, "output": new})


def score_one(row, today, stored=None, ask=None):
    """(这份资料洗完、对齐完的 doc, 分数)。stored：这个版本库里已有的 (药名行, 编号行)，给了就不再联网；
    ask：查药名用的权威库（测试、离线重算时传）。"""
    doc = document(row)
    lineage = {}
    _step(lineage, doc, "parse", doc["parse_method"], {})
    before = fields.traced(doc)
    doc = fields.clean_fields(doc)
    _step(lineage, doc, "clean", "clean", before)
    before = fields.traced(doc)
    doc, changed = fields.normalize_l3(doc)
    for field, old, new in changed:
        _note(lineage, field, "normalize", "l3_normalize", old, new)
    _step(lineage, doc, "normalize", "l3_normalize", before)
    before = fields.traced(doc)
    names, identifiers = stored if stored is not None else align.align(doc, ask)
    for field, found in identifiers.items():
        _note(lineage, f"extra.{field}", "align", "identifier_format_check", found["raw_value"], found.get("std_id") or "")
    for name, found in names.items():
        op = "authority_align_skip" if drug_names.is_placeholder(name) else "authority_align"
        _note(lineage, "drug_name", "align", op, name, found.get("std_id") or "")
    _step(lineage, doc, "align", "authority_align", before)
    doc.update(authority_resolutions=names, identifier_resolutions=identifiers, lineage=lineage)
    return doc, quality.score(doc, today)


# ───────────── 存 ─────────────

def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str)


_KEPT_FIELDS = ("uuid", "title", "publish_date", "doc_type", "language", "structured_data", "extra", "sniffed_meta")


def _write(conn, row, doc, result, scored_at, fresh):
    """一个版本的分数行；fresh（这次新对齐的）时再写对齐行。已有同一主键的行就不写（表上有禁止改删的规则，
    PostgreSQL 不许 ON CONFLICT，所以用 WHERE NOT EXISTS）：重跑不重复。"""
    if fresh:
        lines = [("drug_name", name, found) for name, found in doc["authority_resolutions"].items()]
        lines += [(found["id_type"], field, found) for field, found in doc["identifier_resolutions"].items()]
        if lines:
            conn.execute(text("""
                INSERT INTO derived_authority_resolutions
                  (version_id, aligner_version, id_type, resolution_key, raw_value, candidate, rule, candidates_tried,
                   found, degraded, skipped, std_id, authority, resolved_value, confidence, strategy, sources)
                SELECT :v, :av, :kind, :key, :raw, :candidate, :rule, :tried, :found, :degraded, :skipped, :std,
                       :authority, :resolved, :confidence, :strategy, CAST(:sources AS jsonb)
                WHERE NOT EXISTS (SELECT 1 FROM derived_authority_resolutions WHERE version_id = :v
                                  AND aligner_version = :av AND id_type = :kind AND resolution_key = :key)"""), [
                {"v": row["version_id"], "av": align.ALIGNER_VERSION, "kind": kind, "key": key,
                 "raw": found.get("raw_value"), "candidate": found.get("candidate"), "rule": found.get("rule"),
                 "tried": found.get("candidates_tried"), "found": bool(found.get("found")),
                 "degraded": bool(found.get("degraded")), "skipped": bool(found.get("skipped")),
                 "std": found.get("std_id"), "authority": found.get("authority"),
                 "resolved": found.get("resolved_value"), "confidence": found.get("confidence"),
                 "strategy": found.get("strategy"), "sources": _json(found.get("sources") or [])}
                for kind, key, found in lines])
    conn.execute(text("""
        INSERT INTO derived_quality_scores
          (version_id, scorer_version, text_processor_version, source_id, is_demo, score, level, dimensions, issues,
           violations, confidence, fields, lineage, scored_at)
        SELECT :v, :sv, :tpv, :source, :demo, :score, :level, CAST(:dims AS jsonb), CAST(:issues AS jsonb),
               CAST(:violations AS jsonb), CAST(:confidence AS jsonb), CAST(:fields AS jsonb), CAST(:lineage AS jsonb),
               :at
        WHERE NOT EXISTS (SELECT 1 FROM derived_quality_scores WHERE version_id = :v AND scorer_version = :sv)"""), {
        "v": row["version_id"], "sv": quality.SCORER_VERSION, "tpv": row["text_pv"], "source": row["source_id"],
        "demo": row["is_demo"], "score": result["score"], "level": result["level"],
        "dims": _json(result["dimensions"]), "issues": _json(result["issues"]), "violations": _json(result["violations"]),
        "confidence": _json(result["confidence"]), "fields": _json({k: doc.get(k) for k in _KEPT_FIELDS}),
        "lineage": _json(doc["lineage"]), "at": scored_at})


def _stored(conn, version_ids):
    """这些版本在当前对齐版本下已有的对齐结果：{version_id: (药名 → 行, 编号字段 → 行)}。"""
    out = {}
    for r in conn.execute(text(
            "SELECT * FROM derived_authority_resolutions WHERE version_id = ANY(:ids) AND aligner_version = :av "
            "ORDER BY version_id, id_type, resolution_key"),
            {"ids": list(version_ids), "av": align.ALIGNER_VERSION}).mappings():
        names, identifiers = out.setdefault(r["version_id"], ({}, {}))
        found = {k: r[k] for k in ("found", "std_id", "authority", "raw_value", "resolved_value", "confidence",
                                   "degraded", "skipped", "candidate", "rule", "strategy", "sources")}
        found["candidates_tried"] = r["candidates_tried"]
        if r["id_type"] == "drug_name":
            names[r["resolution_key"]] = found
        else:
            identifiers[r["resolution_key"]] = {**found, "id_type": r["id_type"]}
    return out


def _lookup_failed(names):
    """有药名问了联网的库、却一个都没查成（超时、限流）。这样的版本这次不存，下一轮再查：表只追加，存下了就改不掉，
    一次断网不该永久算成“没对上”。不出网的实例只问本地白名单，照常存（记降级）。"""
    return any(found.get("degraded") and any(answer["authority"] != "local" for tried in found.get("sources") or []
                                             for answer in tried["results"])
               for found in names.values())


def _score_rows(engine, rows, counts, ask, began=None, budget=None):
    """逐个版本：算（对齐可能联网，事务外）、写（一个版本一个短事务）。做不成的记进 failed_versions，本轮不再取它。"""
    with engine.begin() as conn:
        stored = _stored(conn, [r["version_id"] for r in rows])
    for row in rows:
        if began is not None and time.monotonic() - began >= budget:
            break
        try:
            scored_at = datetime.now(timezone.utc)
            done = stored.get(row["version_id"])
            doc, result = score_one(row, scored_at.date(), stored=done, ask=ask)
            if done is None and _lookup_failed(doc["authority_resolutions"]):
                counts["failed_versions"].append({"version_id": row["version_id"],
                                                  "error": "lookup_failed: 联网的权威库都没查成，下一轮再查"})
                continue
            with engine.begin() as conn:
                _write(conn, row, doc, result, scored_at, fresh=done is None)
        except Exception as exc:  # noqa: BLE001 -- 一个版本做不成不挡别的版本
            counts["failed_versions"].append({"version_id": row["version_id"],
                                              "error": (type(exc).__name__ + ": " + str(exc))[:300]})
            continue
        counts["scored"] += 1
        counts["alignment_reused" if done is not None else "aligned"] += 1
        counts["levels"][result["level"]] = counts["levels"].get(result["level"], 0) + 1


def process(*, batch=BATCH, time_budget_seconds=60, ask=None):
    """一批一批地给还没有当前评分器版本分数的版本打分，直到没有活或到时间；返回计数。"""
    from store.history import history_engine

    engine = history_engine()
    began = time.monotonic()
    counts = {"scored": 0, "aligned": 0, "alignment_reused": 0, "levels": {}, "failed_versions": []}
    while time.monotonic() - began < time_budget_seconds:
        skip = [f["version_id"] for f in counts["failed_versions"]]
        with engine.begin() as conn:
            ids = conn.execute(text(_TODO_SQL), _params(skip=skip, n=batch)).scalars().all()
            rows = [dict(r) for r in conn.execute(text(_BY_IDS_SQL), _params(ids=list(ids))).mappings()] if ids else []
        if not rows:
            break
        _score_rows(engine, rows, counts, ask, began, time_budget_seconds)
    return counts


def process_versions(version_ids, ask=None):
    """只给指定的这几个版本打分（main_etl.py 单次调试用）；已经有当前评分器版本分数的跳过。返回计数。"""
    from store.history import history_engine

    engine = history_engine()
    counts = {"scored": 0, "aligned": 0, "alignment_reused": 0, "levels": {}, "failed_versions": []}
    with engine.begin() as conn:
        rows = [dict(r) for r in conn.execute(text(_BY_IDS_SQL), _params(ids=list(version_ids))).mappings()]
    if rows:
        _score_rows(engine, rows, counts, ask)
    return counts


def scores_for(version_ids):
    """{version_id: 当前评分器版本的分数和对齐结果}；还没打分的版本不出现。给 /api/history/clean-text?quality=true。"""
    from store.history import history_engine

    params = {"ids": list(version_ids), "sv": quality.SCORER_VERSION, "av": align.ALIGNER_VERSION}
    with history_engine().begin() as conn:
        scores = conn.execute(text(
            "SELECT version_id, scorer_version, text_processor_version, score, level, dimensions, issues, violations, "
            "confidence, scored_at FROM derived_quality_scores WHERE version_id = ANY(:ids) AND scorer_version = :sv"),
            params).mappings().all()
        lines = conn.execute(text(
            "SELECT version_id, id_type, resolution_key, found, std_id, authority, resolved_value, degraded, skipped "
            "FROM derived_authority_resolutions WHERE version_id = ANY(:ids) AND aligner_version = :av "
            "ORDER BY version_id, id_type, resolution_key"), params).mappings().all()
    out = {r["version_id"]: {**{k: r[k] for k in r if k != "version_id"}, "scored_at": r["scored_at"].isoformat(),
                             "aligner_version": align.ALIGNER_VERSION, "alignments": []} for r in scores}
    for line in lines:
        if line["version_id"] in out:
            out[line["version_id"]]["alignments"].append({k: line[k] for k in line if k != "version_id"})
    return out
