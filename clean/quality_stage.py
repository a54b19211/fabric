"""质量段：给画像过的版本打徐子雯的五维质量分，编号对齐、字段清洗结果和血缘一起存下。
雷达读的清洗正文、编号、画像一个字不动；分数只追加；“隔离”只是等级，不拦截，所有版本照常建索引、给雷达。

从哪读起：process()。一轮按“新抓到的先做”翻“有当前画像、还没有当前评分器版本分数”的版本号，一页 TODO_LIMIT 个，
做完一页还有时间就接着翻下一页；再 BATCH 个一组取字段，每个版本：
  document()   版本 → 一份资料 doc（键和徐子雯原来的 parsed_result 一样；markdown 就是这个版本的清洗正文）
  score_doc()  记“解析”血缘 → 字段清洗（clean/fields.py，记“清洗”血缘）→ L3 规范化（记“规范化”血缘）
               → 编号对齐（clean/align.py，药名要联网，记“对齐”血缘）→ 五维打分和结构校验（clean/quality.py）
  _write()     写 derived_quality_scores（一个版本、一个评分器版本一行）和 derived_authority_resolutions
               （一个版本、一个对齐版本下，每个药名、每个编号一行）
领活：一个版本一个短事务，先用 FOR UPDATE SKIP LOCKED 锁住它的画像行（别的进程正在做就跳过，不重复联网），
再对齐、打分、写，提交时放锁。联网时只占着这一行锁，不挡清洗和抓取。
这个版本在当前对齐版本下已经对齐过（库里有行）就用库里的，不再联网：只换评分口径（升 SCORER_VERSION）时不用重新查网。
药名没查成（降级）的版本不存，下一轮再查（表只追加，一次断网不该永久记成“没对上”）。要是问的库全都没查成（断网，
或不出网的实例），这一轮之后只做不用联网的版本（没有药名、或已经对齐过的）。一轮有时间上限（run/clean.py --quality-seconds）。
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

BATCH = 50  # 一次取几个版本的字段
TODO_LIMIT = 2000  # 一页几个版本号
FIRST_PAGE = ("infinity", "")  # 第一页从最新的开始：哪个时间都早于 infinity
CT_DRUG_TYPES = {"DRUG", "BIOLOGICAL", "COMBINATION_PRODUCT"}  # ClinicalTrials 干预里算药名的类型

# fulltext：PubMed 记录的全文附件（附件的记录号是 “PMID#attachment=网址”），给 fulltext_status 用
_FIELDS = """
    SELECT v.version_id, v.source_id, v.is_demo, v.source_record_id, v.payload->>'title' AS title,
           v.payload->>'source_url' AS url, v.payload->'facts' AS facts, v.payload->>'parser_version' AS parser,
           v.payload->'source_published_at'->>'value' AS published,
           v.payload->'source_updated_at'->>'value' AS updated,
           CASE WHEN v.source_id = 'clinicaltrials' THEN v.payload->>'clean_text' END AS summary,
           CASE WHEN v.source_id = 'pubmed' THEN v.payload->>'raw_content' END AS raw,
           CASE WHEN v.source_id = 'pubmed' THEN (
             SELECT jsonb_agg(jsonb_build_object('from', a.payload->'facts'->>'discovered_from',
                                                 'url', a.payload->>'source_url'))
             FROM history_versions a WHERE a.source_id = 'pubmed' AND a.is_demo = v.is_demo
               AND a.source_record_id LIKE v.source_record_id || '#attachment%') END AS fulltext,
           p.text_processor_version AS text_pv,
           (SELECT d.text FROM derived_clean_text d WHERE d.version_id = v.version_id
              AND d.processor_version = p.text_processor_version ORDER BY d.attempt DESC LIMIT 1) AS text
    FROM history_versions v
    JOIN derived_profiles p ON p.version_id = v.version_id AND p.processor_version = :prof
                           AND p.text_processor_version = ANY(:texts)
"""
_SCORED = "EXISTS (SELECT 1 FROM derived_quality_scores q WHERE q.version_id = v.version_id AND q.scorer_version = :sv)"
# 领活分两步：先只挑版本号（不碰 payload，排序时不用把每个版本的原件都读一遍），再按版本号取字段。
# 翻页按 (first_seen_at, version_id) 从新到旧：下一页从上一页最后一个版本往后接
_TODO_SQL = """
    SELECT v.version_id, v.first_seen_at FROM history_versions v
    JOIN derived_profiles p ON p.version_id = v.version_id AND p.processor_version = :prof
                           AND p.text_processor_version = ANY(:texts)
    WHERE NOT """ + _SCORED + """
      AND (v.first_seen_at, v.version_id) < (CAST(:at AS timestamptz), CAST(:vid AS char(64)))
    ORDER BY v.first_seen_at DESC, v.version_id DESC LIMIT :n"""
_BY_IDS_SQL = (_FIELDS + " WHERE v.version_id = ANY(:ids) AND NOT " + _SCORED
               + " ORDER BY v.first_seen_at DESC, v.version_id DESC")
# 锁住一个版本的画像行：别的进程锁着（正在打分）就拿不到；锁到之后再看一眼还没分数（别人可能刚写完）
_CLAIM_SQL = """
    SELECT 1 FROM derived_profiles p
    WHERE p.version_id = :v AND p.processor_version = :prof AND p.text_processor_version = :tpv
      AND NOT EXISTS (SELECT 1 FROM derived_quality_scores q WHERE q.version_id = p.version_id AND q.scorer_version = :sv)
    FOR UPDATE OF p SKIP LOCKED"""


def _params(**more):
    return {"prof": profile.PROCESSOR_VERSION, "texts": [clean_text.PROCESSOR_VERSION, attachment_parse.PROCESSOR_VERSION],
            "sv": quality.SCORER_VERSION, **more}


# ───────────── 版本 → 一份资料 ─────────────

def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    """原件里该是列表的字段：是列表就用它，单个值包成一项，空的是 []。"""
    return value if isinstance(value, list) else [] if value in (None, "", {}) else [value]


def document(row) -> dict:
    """一个版本 → 评分要的一份资料。uuid 和徐子雯旧库同一算法（网页 uuid5(网址)、接口记录 uuid5(站点:记录键)），
    分数能和她旧库的文档对上。字段从投影里取（facts 里站点抽取器给的 extra / structured），不改写；形状不对的当空。"""
    facts = _dict(row["facts"])
    source, role, title = row["source_id"], facts.get("role"), row["title"] or ""
    published = row["published"] or ""
    doc = {"uuid": row["source_record_id"], "source_url": row["url"] or "", "title": title,
           "source_site": (facts.get("source_site") or source) if source == "legacy_documents" else source,
           "publish_date": published, "markdown": row["text"] or "",
           "parse_method": facts.get("parse_method") or row["parser"] or "history_projection",
           "structured_data": {"title": title, "publish_date": published, "category": "", "summary": ""},
           "extra": dict(_dict(facts.get("extra"))), "sniffed_meta": {}}
    if role == "web_capture":
        doc["uuid"] = make_uuid(row["source_record_id"])  # 网页版本的记录号就是网址
    elif role == "site_record":
        doc["structured_data"] = dict(_dict(facts.get("structured")))
    elif role == "attachment":
        _attachment(doc, row)
    elif source in _RECORDS:
        _RECORDS[source](doc, row, facts)
    return doc


def _attachment(doc, row):
    """附件（PDF 等）：标题取正文第一个 # 标题，没有就用版本标题（附件的文件名）；DOI、期刊名从正文头部嗅
    （徐子雯 route_pdf 的做法）。"""
    meta = fields.sniff_pdf_meta(doc["markdown"])
    title = meta["sniffed_title"] or doc["title"]
    doc.update(title=title, publish_date="", parse_method=row["text_pv"] or "attachment_parse",
               sniffed_meta={"doi": meta["doi"], "journal_name": meta["journal_name"]},
               structured_data={"title": title, "publish_date": "", "category": "", "summary": ""})


def _names(items, key):
    return [str(item.get(key, "")).strip() for item in _list(items) if isinstance(item, dict) and item.get(key)]


def _clinicaltrials(doc, row, facts):
    """ClinicalTrials：照她 config/sites/ClinicalTrials.py 的字段映射。发布日期用最近更新公布日，药名取药物类干预。"""
    interventions = [i for i in _list(facts.get("interventions")) if isinstance(i, dict)]
    drugs = [str(i["name"]).strip() for i in interventions if i.get("type") in CT_DRUG_TYPES and i.get("name")]
    updated = row["updated"] or ""
    summary = row["summary"] if row["summary"] and row["summary"] != doc["title"] else ""
    doc.update(uuid=make_uuid("clinicaltrials:" + row["source_record_id"]), publish_date=updated)
    doc["structured_data"] = {"title": doc["title"], "publish_date": updated, "category": "临床试验",
                              "summary": summary[:500], "drug_name": "; ".join(drugs), "manufacturer": ""}
    doc["extra"] = {
        "nct_id": row["source_record_id"], "study_type": facts.get("study_type") or "",
        "phase": _list(facts.get("phase")), "study_status": facts.get("status") or "",
        "enrollment": facts.get("enrollment"), "study_completion_date": facts.get("completion_date") or "",
        "primary_completion_date": facts.get("primary_completion_date") or "",
        "sponsor": _dict(facts.get("lead_sponsor")).get("name") or "",
        "collaborators": _names(facts.get("collaborators"), "name"), "conditions": _list(facts.get("conditions")),
        "interventions": _names(interventions, "name"), "arm_groups": _names(facts.get("arm_groups"), "label")}


def _author(node):
    """一个作者写成她 esummary 里的样子：“姓 名缩写”；团体作者用团体名。"""
    return node.findtext("CollectiveName") or " ".join(
        part for part in (node.findtext("LastName"), node.findtext("Initials")) if part)


def _pubmed(doc, row, facts):
    """PubMed：照她 pubmed_fetcher._build_result 拼，structured_data 是她那 15 个键（药名、生产企业她也留空）；
    extra 是她落库时按 PubmedExtra 从 structured_data 补的那几个。期刊名、作者只在原件 XML 里。
    全文状态看这条记录有没有全文附件版本：PMC 全文 XML → pmc_fulltext；Unpaywall 的 OA PDF → unpaywall_pdf，
    oa_pdf_url 是它的地址；都没有 → abstract_only。主线清洗正文只有标题和摘要（她原版正文里还有一行行元数据）。"""
    journal, authors = "", []
    try:
        root = ET.fromstring(row.get("raw") or "")
        journal = root.findtext("./MedlineCitation/Article/Journal/Title") or ""
        authors = [name for name in map(_author, root.findall("./MedlineCitation/Article/AuthorList/Author")) if name]
    except ET.ParseError:
        pass
    routes = {a.get("from"): a.get("url") or "" for a in _list(row.get("fulltext")) if isinstance(a, dict)}
    status = "pmc_fulltext" if "pmc_efetch" in routes else "unpaywall_pdf" if "unpaywall" in routes else "abstract_only"
    pmid, pmcid, doi = (str(facts.get(key) or "") for key in ("pmid", "pmcid", "doi"))
    abstract = str(facts.get("abstract") or "")
    doc["structured_data"] = {
        "title": doc["title"], "publish_date": doc["publish_date"], "category": "医学文献", "summary": abstract[:500],
        "drug_name": "", "manufacturer": "", "fulltext_status": status, "pmcid": pmcid,
        "oa_pdf_url": routes["unpaywall"] if status == "unpaywall_pdf" else "", "pmid": pmid, "doi": doi,
        "journal_name": journal, "authors": authors, "publication_types": _list(facts.get("publication_types")),
        "correction_links": _list(facts.get("correction_links"))}
    doc["extra"] = {key: doc["structured_data"][key] for key in fields.SITE_FIELDS["pubmed"]}
    doc["sniffed_meta"] = {"doi": doi, "journal_name": journal}
    doc["parse_method"] = "pubmed_api+" + status


def _europe_pmc(doc, row, facts):
    """Europe PMC：她没有这个来源的抓取器，structured_data 用通用 4 个键，类别照她 PubMed 写“医学文献”；编号和期刊名
    放进 extra（她的 EuropePmcExtra），DOI 和期刊名也给权威分用。主线不抓它的全文，fulltext_status 就是 abstract_only。"""
    journal = str(_dict(_dict(facts.get("journal")).get("journal")).get("title") or "")
    doi = str(facts.get("doi") or "")
    doc["structured_data"].update(category="医学文献", summary=str(facts.get("abstract") or "")[:500])
    doc["extra"] = {"pmid": str(facts.get("pmid") or ""), "pmcid": str(facts.get("pmcid") or ""), "doi": doi,
                    "journal_name": journal, "fulltext_status": "abstract_only"}
    doc["sniffed_meta"] = {"doi": doi, "journal_name": journal}


def _fda_crl(doc, row, facts):
    """FDA 完整回应函：字段照她 schemas 里的 FdaCrlExtra。"""
    apps = facts.get("application_numbers")
    doc["extra"] = {"application_number": "; ".join(map(str, apps)) if isinstance(apps, list) else str(apps or ""),
                    **{k: str(facts.get(k) or "") for k in ("letter_date", "letter_type", "file_name")}}


# 接口记录（不是网页、站点记录、附件）里字段要另外拼的来源
_RECORDS = {"clinicaltrials": _clinicaltrials, "pubmed": _pubmed, "europe_pmc": _europe_pmc, "fda_crl": _fda_crl}


# ───────────── 一个版本：洗字段、对齐、打分 ─────────────

def _step(lineage, doc, step, op, before):
    """这一步对每个追踪字段记一条 {step, op, input, output}（她 lineage_tracer.record_step 的结构）。"""
    for field, value in fields.traced(doc).items():
        lineage.setdefault(field, []).append({"step": step, "op": op, "input": before.get(field, ""), "output": value})


def _note(lineage, field, step, op, old, new):
    lineage.setdefault(field, []).append({"step": step, "op": op, "input": old, "output": new})


def score_one(row, today, stored=None, ask=None):
    """一个版本 → (这份资料洗完、对齐完的 doc, 分数)。stored、ask 见 score_doc。"""
    return score_doc(document(row), today, stored, ask)


def score_doc(doc, today, stored=None, ask=None):
    """(这份资料洗完、对齐完的 doc, 分数)。stored：这个版本库里已有的 (药名行, 编号行)，给了就不再联网；
    ask：查药名用的权威库（测试、离线重算时传）。"""
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
    for key, found in identifiers.items():  # 键是字段名，拆开的第 2 个起是 字段#2
        _note(lineage, "extra." + key.split("#")[0], "align", "identifier_format_check", found["raw_value"],
              found.get("std_id") or "")
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


# ───────────── 跑批 ─────────────

def _lookup_failed(names):
    """有药名没查成（降级：超时、限流、不出网）。这样的版本这次不存，等查得成的时候再来：表只追加，存下了就改不掉，
    一次断网不该永久算成“没对上”。"""
    return any(found.get("degraded") for found in names.values())


def _needs_network(doc):
    """这份资料对齐时要不要联网：有不是占位名的药名。"""
    return any(not drug_names.is_placeholder(name) for name in drug_names.names_of(doc))


def _counts():
    # deferred：这一轮断网后，因为要联网留到下一轮的版本数；offline：这一轮断网了没有
    return {"scored": 0, "aligned": 0, "alignment_reused": 0, "levels": {}, "failed_versions": [], "deferred": 0,
            "offline": False}


def _score_one_claimed(engine, row, doc, done, ask):
    """一个版本一个事务：锁住它的画像行，算（对齐可能联网）、写。返回 (结果, 内容)：
    ("busy", None) 别的进程正在做；("lookup_failed", 药名行) 有药名没查成，不写；("scored", 分数)。"""
    with engine.begin() as conn:
        claim = {"v": row["version_id"], "tpv": row["text_pv"], **_params()}
        if conn.execute(text(_CLAIM_SQL), claim).first() is None:
            return "busy", None
        scored_at = datetime.now(timezone.utc)
        doc, result = score_doc(doc, scored_at.date(), stored=done, ask=ask)
        if done is None and _lookup_failed(doc["authority_resolutions"]):
            return "lookup_failed", doc["authority_resolutions"]
        _write(conn, row, doc, result, scored_at, fresh=done is None)
        return "scored", result


def _score_rows(engine, rows, counts, ask, offline=False, deadline=None):
    """逐个版本打分，返回这一轮是不是断网了。断网以后，要联网的版本（有药名、还没对齐过）留到下一轮；
    做不成的记进 failed_versions。"""
    with engine.begin() as conn:
        stored = _stored(conn, [r["version_id"] for r in rows])
    for row in rows:
        if deadline is not None and time.monotonic() >= deadline:
            break
        done = stored.get(row["version_id"])
        try:
            doc = document(row)
            if offline and done is None and _needs_network(doc):
                counts["deferred"] += 1
                continue
            outcome, value = _score_one_claimed(engine, row, doc, done, ask)
        except Exception as exc:  # noqa: BLE001 -- 一个版本做不成不挡别的版本
            counts["failed_versions"].append({"version_id": row["version_id"],
                                              "error": (type(exc).__name__ + ": " + str(exc))[:300]})
            continue
        if outcome == "lookup_failed":
            offline = offline or any(align.all_down(found) for found in value.values())
            counts["failed_versions"].append({"version_id": row["version_id"],
                                              "error": "lookup_failed: 药名在权威库没查成，下一轮再查"})
        elif outcome == "scored":
            counts["scored"] += 1
            counts["alignment_reused" if done is not None else "aligned"] += 1
            counts["levels"][value["level"]] = counts["levels"].get(value["level"], 0) + 1
    return offline


def _todo_batches(engine, batch):
    """还没打分的版本号，一次给 batch 个。一页 TODO_LIMIT 个，新抓到的先做；一页给完接着往下翻。
    没打成的（药名没查成）不落表，留在原处：不翻页的话，攒满一页这样的版本，后面的就永远轮不到。"""
    after = FIRST_PAGE
    while True:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout='120000ms'"))
            page = conn.execute(text(_TODO_SQL), _params(n=TODO_LIMIT, at=after[0], vid=after[1])).all()
        for start in range(0, len(page), batch):
            yield [r.version_id for r in page[start:start + batch]]
        if len(page) < TODO_LIMIT:
            return
        after = (page[-1].first_seen_at, page[-1].version_id)


def process(*, batch=BATCH, time_budget_seconds=60, ask=None):
    """给还没有当前评分器版本分数的版本打分，直到没有活或到时间（到点做完手上那个版本才停）；返回计数。"""
    from store.history import history_engine

    engine = history_engine()
    deadline = time.monotonic() + time_budget_seconds
    counts, offline = _counts(), False
    for ids in _todo_batches(engine, batch):
        if time.monotonic() >= deadline:
            break
        with engine.begin() as conn:
            rows = [dict(r) for r in conn.execute(text(_BY_IDS_SQL), _params(ids=ids)).mappings()]
        offline = _score_rows(engine, rows, counts, ask, offline, deadline)
    counts["offline"] = offline
    return counts


def process_versions(version_ids, ask=None):
    """只给指定的这几个版本打分（main_etl.py 单次调试用）；已经有当前评分器版本分数的跳过。返回计数。"""
    from store.history import history_engine

    engine = history_engine()
    counts = _counts()
    with engine.begin() as conn:
        rows = [dict(r) for r in conn.execute(text(_BY_IDS_SQL), _params(ids=list(version_ids))).mappings()]
    if rows:
        counts["offline"] = _score_rows(engine, rows, counts, ask)
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
