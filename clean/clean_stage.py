"""清洗段：每个版本只处理一次，写出干净正文、编号和画像（临床登记另写和上一版的逐字段差分）。结果只追加、可重跑，
同样的输入得到同样的结果。
领活用 SELECT ... FOR NO KEY UPDATE SKIP LOCKED，可多进程并行：别的进程正在处理的版本直接跳过。

从哪读起：process()。它一批一批地领还没画像的版本（_process_batch），一批的活在 _process_rows 里：_prepare_rows
选出清洗的输入，_version_text 清洗（这个版本已经洗过就用那份正文），_write_rows 批量写入。process_versions() 只处理指定的几个版本，
给 main_etl.py 单次调试用；stage_status() 给出各来源的进度；for_versions() 把一页版本的产物读给 API。命令行入口是
run/clean.py。

读写的表：读 fabric_versions 和 fabric_sources；derived_stage_waiting 记附件还没解析完的版本；写 derived_clean_text
（清洗正文）、derived_identifiers（编号）、derived_profiles（画像）、derived_registry_diff_runs / derived_registry_changes
（临床登记差分）。写画像的同一个事务里还会给 index_publications（建索引待办）排活。

运行方式（2026-11-04 现役约 70 万版本 / 13GB，之后到千万级）：
- 领活：一批 = 还没有当前画像、也不在等附件正文的版本，按（来源, published_seq）先后；领到的版本行锁到这批写完。
  没活时这条查询是一次全表的反连接（隔离库实测 70 万个已画像版本约 0.8 秒），每轮只查一次，不分来源。
  整批写不成时逐个重做，做不成的版本记在返回的 failed_versions 里，本轮跳过，下一轮再试。
- 已经洗过的不重洗：这个版本在当前清洗器版本下已有正文（derived_clean_text 有 (version_id, processor_version)
  这一行），就用那份正文重算编号和画像（例如只有画像处理器升级时）。每份正文只留一个摘要 text_sha256。
- 一批一次读（只取所需字段，原件只在 EPMC 和网页版本上读）、一次事务批量写。
- 附件没解析完记 derived_stage_waiting，不卡住别的版本；之后每次运行先补做。
- stage_status() 给出每个来源的版本数、已画像数、等附件的数和还没做的数。

每个还没有画像（derived_profiles，当前画像处理器版本）的版本，在一个事务里写：
1. 清洗正文（derived_clean_text，processor_version 是 clean/clean_text.py 的 PROCESSOR_VERSION）：
   - clinicaltrials：登记字段逐字段成段（JSON 路径定位），简介在前；
   - europe_pmc：原件 JSON 的标题 + 摘要（偏移回到原件 JSON）；
   - nmpa / cde / chictr 的网页版本：旧提取器解析出的 Markdown（偏移回到原件 JSON 里的 /parsed/markdown）；
   - 附件：不另清洗，正文用加工层 attachment_parse 的最新结果；还没解析完（没有成功/不支持、失败未满次数）
     就先不画像，等下一轮；
   - 其他来源：投影里的 clean_text 按 Markdown 规则清洗（偏移回到投影正文，写明定位基准）。
2. 编号（derived_identifiers）：在这份正文上抽取，偏移指这份正文。
3. 画像（derived_profiles）：分类、四项质量状态（格式、来源可信度、实体匹配、事实核验，不算分）、分流（clean.profile）。
   五维质量分不在这里，是之后的质量段（clean/quality_stage.py）。
4. 临床登记差分（clean/registry_changes.py · record_versions）：和这个版本取代的上一版逐字段比。
5. 索引待办（store.index_publication.enqueue_versions）：画像写下的同一事务里，按画像所基于的那份正文入队。
图谱等派生来源不在这一层处理。
"""

from __future__ import annotations

import hashlib
import json
import re
import time

from sqlalchemy import text

from clean import attachment_parse, clean_text, identifiers, profile, registry_changes
from clean.processors import UNCLEANED_SOURCES
from store.record_models import FabricError

SKIP_SOURCES = UNCLEANED_SOURCES
from fetch.catalog import SITE_SOURCES  # noqa: E402  站点插件采集的来源

BATCH = 200  # 一批（一个事务）处理几个版本


def stage_key() -> str:
    """阶段键 = 本阶段用到的所有处理器版本（等附件正文的记录按它分开）。"""
    return "clean_stage:" + "+".join([clean_text.PROCESSOR_VERSION, attachment_parse.PROCESSOR_VERSION,
                                      identifiers.PROCESSOR_VERSION, profile.PROCESSOR_VERSION])


def _cleaner_input(row):
    """(清洗器的输入, 定位基准)。"""
    source, facts = row["source_id"], row["facts"] or {}
    if source == "clinicaltrials":
        # 摘要直接取原件 briefSummary：投影在没有摘要时用标题顶替，拿它当摘要会把标题定位到摘要路径上
        return {"facts": facts, "locators": row["locators"] or {}, "summary": row.get("brief_summary")}, \
            "registry_fields"
    if source == "europe_pmc":
        # 只取清洗器用到的字段。定位用 JSON 路径（/title、/abstractText），不随其他字段变化移动，在每个版本上都能回指。
        try:
            data = json.loads(row["raw"] or "{}")
        except ValueError:
            return {"raw": row["raw"] or ""}, "raw_json"
        return {"title": data.get("title"), "abstractText": data.get("abstractText")}, "literature_fields"
    if source == "who_don":
        # from the raw JSON: the projection's clean_text flattened <table> and has no title heading
        return {"raw": row["raw"] or ""}, "who_raw_json"
    if source in SITE_SOURCES and facts.get("role") in ("web_capture", "site_record"):
        try:
            markdown = (json.loads(row["raw"] or "{}").get("parsed") or {}).get("markdown") or ""
        except ValueError:
            markdown = ""
        return {"markdown": markdown, "source_id": source}, "raw_json:/parsed/markdown"
    # documents 库存按原站套站点模板
    site = facts.get("source_site") if source == "legacy_documents" else source
    return {"markdown": row["clean_text"] or "", "source_id": site}, "projection_clean_text"


# 定位基准：段落 source 的偏移相对什么。网页/站点记录是原件 JSON 里 /parsed/markdown 字段解码后的值；
# 投影正文是这个版本记录本身的 /clean_text 字段（不在原件里）。存进每段，API 和契约照原样带出去。
_ANCHORS = {
    "raw_json:/parsed/markdown": {"json_pointer": "/parsed/markdown", "within": "raw"},
    "projection_clean_text": {"json_pointer": "/clean_text", "within": "record"},
}


def _anchored(segments, basis):
    """这一版本自己的定位基准写进每段（返回新列表，不改传入的段）。同样的正文可以来自不同基准（站点原件的
    /parsed/markdown 与库存冻结件的投影 /clean_text）：定位按当前版本写。"""
    anchor = _ANCHORS.get(basis)
    if not anchor:
        return segments or []
    out = []
    for seg in segments or []:
        src = seg.get("source") or {}
        if "start" in src and "end" in src:
            seg = {**seg, "source": {**anchor, "start": src["start"], "end": src["end"]}}
        out.append(seg)
    return out


def _clean_by_basis(source, inp, basis):
    if basis == "registry_fields":
        return clean_text.clean_registry(inp["facts"], inp["locators"], summary=inp["summary"])
    if basis == "raw_json":
        return clean_text.clean_literature(inp["raw"])
    if basis == "who_raw_json":
        return clean_text.clean_who(inp["raw"])
    if basis == "literature_fields":
        out = clean_text.clean_literature(json.dumps({k: v for k, v in inp.items() if v is not None},
                                                     ensure_ascii=False))
        for seg in out["segments"]:
            seg["source"] = {"json_pointer": (seg.get("source") or {}).get("json_pointer")
                             or ("/title" if seg.get("kind") == "title" else "/abstractText")}
        return out
    return clean_text.clean_markdown(inp["markdown"], source_id=inp.get("source_id") or source)


def _final_attachment_texts(conn, version_ids):
    """附件当前解析器版本下的最终结果：{version_id: row}；还在重试中的不出现（等）。"""
    from clean.derived_text import MAX_ATTEMPTS

    if not version_ids:
        return {}
    rows = conn.execute(text(
        "SELECT DISTINCT ON (version_id) version_id,status,text,attempt FROM derived_clean_text "
        "WHERE version_id=ANY(:ids) AND processor_version=:pv ORDER BY version_id, attempt DESC"
    ), {"ids": list(version_ids), "pv": attachment_parse.PROCESSOR_VERSION}).mappings().all()
    return {r["version_id"]: dict(r) for r in rows
            if r["status"] in ("parsed", "unsupported") or r["attempt"] >= MAX_ATTEMPTS}


# 只取清洗需要的字段；原件只在 EPMC、WHO 和站点页面/记录版本上取，CT 只取原件里的 briefSummary（省 IO）
_FIELDS = """
    SELECT version_id, source_id, is_demo, source_record_id, record_kind, published_seq,
           payload->'facts' AS facts, payload->'field_locators' AS locators, payload->>'clean_text' AS clean_text,
           payload->>'title' AS title,
           CASE WHEN source_id IN ('europe_pmc', 'who_don')
                     OR payload->'facts'->>'role' IN ('web_capture','site_record')
                THEN payload->>'raw_content' END AS raw,
           CASE WHEN source_id='clinicaltrials'
                THEN (payload->>'raw_content')::jsonb #>> '{protocolSection,descriptionModule,briefSummary}'
           END AS brief_summary
    FROM fabric_versions v
"""
_PROFILED = """EXISTS (SELECT 1 FROM derived_profiles p WHERE p.version_id=v.version_id
                       AND p.processor_version=:prof AND p.text_processor_version=ANY(:texts))"""
# 领一批：还没有当前画像、也不在等附件正文的版本，每个来源按发布先后；别的进程锁着的跳过
_TODO_SQL = _FIELDS + """
    WHERE NOT (v.source_id = ANY(:skip_sources)) AND v.record_kind<>'graph' AND NOT (v.version_id = ANY(:skip))
      AND NOT """ + _PROFILED + """
      AND NOT EXISTS (SELECT 1 FROM derived_stage_waiting w WHERE w.stage_key=:k AND w.version_id=v.version_id)
    ORDER BY v.source_id, v.published_seq LIMIT :n
    FOR NO KEY UPDATE OF v SKIP LOCKED
"""
# 同一组字段，按版本号取（等待表补做、main_etl.py 单次调试都用它）
_BY_VERSION_IDS_SQL = _FIELDS + " WHERE version_id=ANY(:ids)"


def _process_rows(conn, key, rows, counts):
    """一批版本：清洗（已经洗过的用那份正文）、编号、画像，批量写入。附件没解析完的记等待表。"""
    work = [r for r in rows if r["record_kind"] != "graph" and r["source_id"] not in SKIP_SOURCES]
    prepared = _prepare_rows(conn, key, work, counts)
    cleaned = _cleaned(conn, [r["version_id"] for r, basis, _ in prepared if basis is not None])
    clean_rows, id_rows, profile_rows = [], [], []
    for r, basis, extra in prepared:
        body, text_pv, warnings, parse_status = _version_text(r, basis, extra, cleaned, counts, clean_rows)
        found = identifiers.extract(body) if body else []
        for hit in found:
            id_rows.append({"v": r["version_id"], "pv": identifiers.PROCESSOR_VERSION, "tpv": text_pv,
                            "kind": hit["kind"], "value": hit["value"], "s": hit["start"], "e": hit["end"]})
        prof = _profile_of(r, body, warnings, found, parse_status)
        profile_rows.append(_profile_row(r, prof, text_pv))
        counts[prof["route"]] = counts.get(prof["route"], 0) + 1
    _write_rows(conn, clean_rows, id_rows, profile_rows, counts)
    registry_changes.record_versions(conn, work)


def _prepare_rows(conn, key, work, counts):
    """Per version (row, basis, cleaner input), or for an attachment (row, None, its final parse result). An
    attachment whose parse is not final yet goes into the waiting table instead, in row order."""
    attachments = [r["version_id"] for r in work if (r["facts"] or {}).get("role") == "attachment"]
    finals = _final_attachment_texts(conn, attachments)
    prepared = []
    for r in work:
        if (r["facts"] or {}).get("role") == "attachment":
            done = finals.get(r["version_id"])
            if done is None:
                conn.execute(text(
                    "INSERT INTO derived_stage_waiting (stage_key, version_id, reason) VALUES (:k,:v,'attachment_text') "
                    "ON CONFLICT DO NOTHING"), {"k": key, "v": r["version_id"]})
                counts["waiting_for_attachment_text"] += 1
                continue
            prepared.append((r, None, done))
            continue
        inp, basis = _cleaner_input(r)
        prepared.append((r, basis, inp))
    return prepared


def _cleaned(conn, version_ids):
    """这些版本在当前清洗器版本下已有的正文 {version_id: 行}：“已经洗过”按 (version_id, processor_version) 判断。"""
    if not version_ids:
        return {}
    return {row["version_id"]: dict(row) for row in conn.execute(text(
        "SELECT DISTINCT ON (version_id) version_id,text,warnings FROM derived_clean_text "
        "WHERE version_id=ANY(:ids) AND processor_version=:pv ORDER BY version_id, attempt DESC"
    ), {"ids": list(version_ids), "pv": clean_text.PROCESSOR_VERSION}).mappings()}


def _version_text(r, basis, extra, cleaned, counts, clean_rows):
    """(body, text processor version, warnings, parse status) of one prepared version. A version this cleaner
    version already cleaned keeps that text; otherwise it is cleaned and its derived_clean_text row is appended to
    clean_rows. An attachment's text comes from the processing layer."""
    if basis is None:  # 附件：正文来自加工层
        body = extra["text"] if extra["status"] == "parsed" else ""
        return body, attachment_parse.PROCESSOR_VERSION, [], extra["status"]
    done = cleaned.get(r["version_id"])
    if done is not None:
        counts["reused"] += 1
        return done["text"], clean_text.PROCESSOR_VERSION, list(done["warnings"] or []), None
    result = _clean_by_basis(r["source_id"], extra, basis)
    counts["cleaned"] += 1
    body, text_pv = result["text"], clean_text.PROCESSOR_VERSION
    warnings = list(result.get("warnings") or [])
    clean_rows.append({
        "v": r["version_id"], "pv": text_pv, "source": r["source_id"], "demo": r["is_demo"],
        "native": r["source_record_id"], "status": "parsed" if body else "failed", "method": basis,
        "error": None if body else ",".join(warnings or ["no_body"]), "text": body, "chars": len(body),
        "sha": hashlib.sha256(body.encode()).hexdigest(),
        "kind": "json_pointer" if basis in ("registry_fields", "literature_fields") else "char_span",
        "segments": json.dumps(_anchored(result.get("segments"), basis), ensure_ascii=False),
        "lang": result.get("language"), "warnings": json.dumps(warnings),
    })
    return body, text_pv, warnings, None


def _profile_of(r, body, warnings, found, parse_status):
    facts = r["facts"] or {}
    pub = facts.get("publication_types")
    pub_types = pub.get("pubType") if isinstance(pub, dict) else pub
    # 按内容分大类只看标题、摘要和来源给的类别字段（如 openFDA 召回记录的“药品召回”），不看正文
    structured = facts.get("structured") or {}
    lead = " ".join(str(x) for x in (r.get("title"), structured.get("summary"), structured.get("category")) if x)
    return profile.profile(source_id=r["source_id"], role=facts.get("role"), chars=len(body), warnings=warnings,
                           identifiers=found, pub_types=pub_types, parse_status=parse_status, lead_text=lead,
                           text=body)


def _profile_row(r, prof, text_pv):
    return {"v": r["version_id"], "pv": prof["processor_version"], "tpv": text_pv,
            "source": r["source_id"], "demo": r["is_demo"], "doc": prof["doc_type"],
            "cat": prof.get("doc_category"),
            "cats": json.dumps(prof.get("evidence_categories") or []),
            "ev": json.dumps(prof["evidence_types"]), "dec": json.dumps(prof["decisions"]),
            "q": json.dumps(prof["quality"], ensure_ascii=False), "route": prof["route"],
            "reasons": json.dumps(prof["route_reasons"], ensure_ascii=False),
            "exp": json.dumps(prof["experiment_types"], ensure_ascii=False),
            "level": json.dumps(prof["reading_level"])}


def _write_rows(conn, clean_rows, id_rows, profile_rows, counts):
    """The batch's writes, in this order: clean texts, identifiers, profiles, then (with the profiles,
    in the same transaction) the index and extraction-check work they make due."""
    if clean_rows:
        conn.execute(text("""
            INSERT INTO derived_clean_text
              (version_id,processor_version,attempt,source_id,is_demo,source_record_id,status,parse_method,
               parse_attempts,error,file_ext,text,chars,text_sha256,locator_kind,segments,language,warnings)
            SELECT :v,:pv,1,:source,:demo,:native,:status,:method,'[]'::jsonb,:error,NULL,:text,:chars,:sha,:kind,
                   CAST(:segments AS jsonb),:lang,CAST(:warnings AS jsonb)
            WHERE NOT EXISTS (SELECT 1 FROM derived_clean_text WHERE version_id=:v AND processor_version=:pv)
        """), clean_rows)
    if id_rows:
        conn.execute(text("""
            INSERT INTO derived_identifiers
              (version_id,processor_version,text_processor_version,kind,value,start_offset,end_offset)
            SELECT :v,:pv,:tpv,:kind,:value,:s,:e
            WHERE NOT EXISTS (SELECT 1 FROM derived_identifiers WHERE version_id=:v AND processor_version=:pv
                              AND text_processor_version=:tpv AND kind=:kind AND value=:value AND start_offset=:s)
        """), id_rows)
    if profile_rows:
        conn.execute(text("""
            INSERT INTO derived_profiles
              (version_id,processor_version,text_processor_version,source_id,is_demo,doc_type,doc_category,
               evidence_categories,evidence_types,
               decisions,quality,route,route_reasons,experiment_types,reading_level)
            SELECT :v,:pv,:tpv,:source,:demo,:doc,:cat,CAST(:cats AS jsonb),CAST(:ev AS jsonb),CAST(:dec AS jsonb),CAST(:q AS jsonb),
                   :route,CAST(:reasons AS jsonb),CAST(:exp AS jsonb),CAST(:level AS jsonb)
            WHERE NOT EXISTS (SELECT 1 FROM derived_profiles WHERE version_id=:v AND processor_version=:pv
                              AND text_processor_version=:tpv)
        """), profile_rows)
        counts["profiled"] += len(profile_rows)
        # 清洗正文合格就能建索引（v3 3.6）：同一事务放进索引待办；没配置索引目标时什么也不做
        from store.index_publication import enqueue_versions

        counts["index_enqueued"] = counts.get("index_enqueued", 0) + enqueue_versions(
            conn, [row["v"] for row in profile_rows])


def _process_waiting(engine, key, counts, limit=500):
    """等待表里已经有最终解析结果的附件：补做画像并移出等待表，返回补做了几条。
    先筛“已就绪”再分页（前面一直没解析完的不会挡住后面已解析的），行锁 SKIP LOCKED 认领，多进程不重复。"""
    from clean.derived_text import MAX_ATTEMPTS

    with engine.begin() as conn:
        ready = conn.execute(text("""
            SELECT w.version_id FROM derived_stage_waiting w
            WHERE w.stage_key=:k AND EXISTS (
              SELECT 1 FROM derived_clean_text d WHERE d.version_id=w.version_id AND d.processor_version=:pv
                AND (d.status IN ('parsed','unsupported') OR d.attempt >= :max))
            ORDER BY w.created_at, w.version_id LIMIT :n FOR UPDATE OF w SKIP LOCKED
        """), {"k": key, "n": limit, "pv": attachment_parse.PROCESSOR_VERSION, "max": MAX_ATTEMPTS}).scalars().all()
        finals = _final_attachment_texts(conn, ready)
        if not finals:
            return 0
        rows = conn.execute(text(_BY_VERSION_IDS_SQL), {"ids": list(finals)}).mappings().all()
        _process_rows(conn, key, [dict(r) for r in rows], counts)
        conn.execute(text("DELETE FROM derived_stage_waiting WHERE stage_key=:k AND version_id=ANY(:ids)"),
                     {"k": key, "ids": list(finals)})
        return len(finals)


def process_versions(version_ids):
    """只处理指定的这几个版本（main_etl.py 单次调试用）：每个版本做的事和 process() 一样，同样可重跑，
    返回计数。"""
    from store.records import fabric_engine

    counts = {"profiled": 0, "cleaned": 0, "reused": 0, "waiting_for_attachment_text": 0}
    with fabric_engine().begin() as conn:
        rows = conn.execute(text(_BY_VERSION_IDS_SQL), {"ids": list(version_ids)}).mappings().all()
        _process_rows(conn, stage_key(), [dict(r) for r in rows], counts)
    return counts


def process(*, batch=BATCH, time_budget_seconds=600):
    """一批一批地领还没画像的版本处理，直到没有活或到时间；返回计数。"""
    from store.records import fabric_engine

    engine = fabric_engine()
    key = stage_key()
    began = time.monotonic()
    counts = {"profiled": 0, "cleaned": 0, "reused": 0, "waiting_for_attachment_text": 0, "batches": 0,
              "quarantine": 0, "weak_signal": 0, "normal": 0, "failed_versions": []}
    while _process_waiting(engine, key, counts) and time.monotonic() - began < time_budget_seconds:
        pass
    while time.monotonic() - began < time_budget_seconds and _process_batch(engine, key, batch, counts):
        pass
    return counts


def _process_batch(engine, key, batch, counts):
    """领一批版本处理掉，返回领到几个（0：没活了）。整批做不成时在同一事务里逐个重做，
    做不成的版本记进 counts["failed_versions"]，本轮不再领它。"""
    params = {"skip_sources": list(SKIP_SOURCES), "n": batch, "k": key,
              "skip": [f["version_id"] for f in counts["failed_versions"]],
              "prof": profile.PROCESSOR_VERSION, "texts": [clean_text.PROCESSOR_VERSION, attachment_parse.PROCESSOR_VERSION]}
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout='120000ms'"))
        rows = [dict(r) for r in conn.execute(text(_TODO_SQL), params).mappings()]
        if not rows:
            return 0
        # 锁到之后再看一眼：别的进程可能刚在放锁之前写完了其中几个
        done = set(conn.execute(text("SELECT v.version_id FROM fabric_versions v WHERE v.version_id=ANY(:ids) AND "
                                     + _PROFILED), {**params, "ids": [r["version_id"] for r in rows]}).scalars())
        work = [r for r in rows if r["version_id"] not in done]
        counts["batches"] += 1
        if work and _in_savepoint(conn, key, work, counts):
            for row in work:
                error = _in_savepoint(conn, key, [row], counts)
                if error:
                    counts["failed_versions"].append({"version_id": row["version_id"], "error": error})
    return len(rows)


def _in_savepoint(conn, key, rows, counts):
    """在一个保存点里处理这些版本：成功返回 None；出错就回到保存点、计数退回，返回错误说明。"""
    before = {k: (list(v) if isinstance(v, list) else v) for k, v in counts.items()}
    try:
        with conn.begin_nested():
            _process_rows(conn, key, rows, counts)
        return None
    except Exception as exc:  # noqa: BLE001 -- 一个版本做不成不挡别的版本
        counts.clear()
        counts.update(before)
        return (type(exc).__name__ + ": " + str(exc))[:300]


def stage_status():
    """每个来源：已发布到哪、版本数、已有当前画像的、在等附件正文的、还没做的。"""
    from store.records import fabric_engine

    params = {"k": stage_key(), "skip": list(SKIP_SOURCES), "prof": profile.PROCESSOR_VERSION,
              "texts": [clean_text.PROCESSOR_VERSION, attachment_parse.PROCESSOR_VERSION]}
    with fabric_engine().begin() as conn:
        rows = conn.execute(text("""
            SELECT source_id, max(published_seq) AS published_seq, count(*) AS versions,
                   count(*) FILTER (WHERE profiled) AS profiled, count(*) FILTER (WHERE waiting) AS waiting
            FROM (SELECT v.source_id, v.published_seq, """ + _PROFILED + """ AS profiled,
                         EXISTS (SELECT 1 FROM derived_stage_waiting w WHERE w.stage_key=:k
                                 AND w.version_id=v.version_id) AS waiting
                  FROM fabric_versions v WHERE v.record_kind<>'graph' AND NOT (v.source_id = ANY(:skip))) s
            GROUP BY source_id ORDER BY source_id
        """), params).mappings().all()
    return {"stage_key": params["k"], "sources": [
        {**r, "todo": r["versions"] - r["profiled"] - r["waiting"]} for r in rows]}


def for_versions(source_id, is_demo, version_ids, mode, cutoff, processor_version=None):
    """一页版本的清洗正文、编号和画像。版本须在本次模式和截止时间下可见。
    processor_version 不给：当前清洗/附件解析处理器的产物；给了：只取这个处理器版本留下的产物（v3 6.1：
    清洗器升级后，按引用里绑定的处理器版本回读模型当时读到的原文；那一版的产物不在就是没有，不拿新版冒充）。"""
    from store import records

    if not 1 <= len(version_ids) <= 100 or any(not re.fullmatch(r"[0-9a-f]{64}", v) for v in version_ids):
        raise FabricError("query_budget", "一次最多 100 个合法版本号", 422)
    if processor_version is not None and not re.fullmatch(r"datafabric\.[a-z_]+\.v[0-9]+", processor_version):
        raise FabricError("invalid_processor_version", "处理器版本格式不对", 422)
    eligible = records._eligibility(mode).replace("first_seen_at", "v.first_seen_at").replace(
        "proof_available_at", "v.proof_available_at")
    with records.fabric_engine().begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        records._source(conn, source_id)
        cutoff = records._cutoff(cutoff)
        if cutoff > records._now(conn):
            raise FabricError("future_cutoff", "证据截止时间不能晚于服务器当前时间")
        params = {"source": source_id, "demo": is_demo, "ids": list(version_ids), "cutoff": cutoff,
                  "pvs": [processor_version] if processor_version else [clean_text.PROCESSOR_VERSION,
                                                                        attachment_parse.PROCESSOR_VERSION],
                  "prof": profile.PROCESSOR_VERSION, "idpv": identifiers.PROCESSOR_VERSION}
        meta, cleans, ids, profiles = _read_products(conn, eligible, params)
    return {"schema_version": "datafabric.clean_stage.v1", "source_id": source_id, "mode": mode,
            "as_of": cutoff.isoformat(), "versions": _version_products(version_ids, meta, cleans, ids, profiles),
            "limitations": ["编号是文本抽取，宁漏勿误；结构化来源的原生编号以原件字段为准",
                            "entity_match / fact_check 在这一层未评估，不代表没有问题"]}


def _read_products(conn, eligible, params):
    """(visible versions' metadata by id, clean texts, identifiers, profiles) of one page, in one transaction."""
    meta = {r["version_id"]: dict(r) for r in conn.execute(text(
        "SELECT v.version_id,v.source_record_id,v.first_seen_at,v.proof_available_at FROM fabric_versions v "
        "WHERE v.source_id=:source AND v.is_demo=:demo AND v.version_id=ANY(:ids) AND " + eligible),
        params).mappings()}
    cleans = conn.execute(text(
        "SELECT DISTINCT ON (version_id) version_id,processor_version,parse_method AS basis,text,text_sha256,"
        "segments,language,warnings,"
        "locator_kind,status FROM derived_clean_text WHERE version_id=ANY(:ids) AND processor_version=ANY(:pvs) "
        "ORDER BY version_id, attempt DESC"), params).mappings().all()  # 当前处理器（或指定的那一版）
    ids = conn.execute(text(
        "SELECT version_id,text_processor_version,kind,value,start_offset,end_offset FROM derived_identifiers "
        "WHERE version_id=ANY(:ids) AND processor_version=:idpv ORDER BY version_id,start_offset"),
        params).mappings().all()
    profiles = conn.execute(text(
        "SELECT version_id,doc_type,doc_category,evidence_categories,evidence_types,decisions,quality,route,"
        "route_reasons,experiment_types,reading_level,text_processor_version "
        "FROM derived_profiles WHERE version_id=ANY(:ids) AND processor_version=:prof"), params).mappings().all()
    return meta, cleans, ids, profiles


def _version_products(version_ids, meta, cleans, ids, profiles):
    """{version_id: state and products} of the requested page."""
    visible = set(meta)
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
        entry = _processed_on(out, i)
        if entry:
            entry["identifiers"].append({k: i[k] for k in i if k not in ("version_id", "text_processor_version")})
    for p in profiles:
        entry = _processed_on(out, p)
        if entry:
            entry["profile"] = {k: p[k] for k in p if k != "version_id"}
    return out


def _processed_on(out, row):
    """The processed entry a derived row belongs to when it was computed on that entry's clean text, else None."""
    entry = out.get(row["version_id"])
    if entry and entry.get("state") == "processed" and row["text_processor_version"] == entry["clean"][
            "processor_version"]:
        return entry
    return None
