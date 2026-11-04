"""v3 3.3 清洗段的运行入口：每个版本一次，确定性、可重跑、只追加；按块认领，可多进程并行。

规模（2026-11-04 现役约 70 万版本 / 13GB，之后到千万级）决定了运行方式：
- 工作队列：按（阶段键, 来源, published_seq 分块）认领，带租约；完成的块标 done，中断的块租约过期后重领，
  失败的块进死信（state=failed + 原因，最多重试 MAX_CHUNK_ATTEMPTS 次）。不再每批反查整张版本表。
- 按输入复用：清洗器的输入算 input_sha256，同一清洗器版本下见过的输入直接复用结果（EPMC 平均每篇 8 个版本、
  大多正文未变）。版本级结果行照常写，接口与契约不变。
- 一块一次读（只取所需字段，原件只在 EPMC 和网页版本上读）、一次事务批量写。
- 附件没解析完记 derived_stage_waiting，不卡住块；之后每次运行先补做。
- stage_status() 给出每个来源的水位、落后数、在处理/失败的块、吞吐。
以下为原说明：

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
from uuid import uuid4

from sqlalchemy import text

from biz_rules.history_models import HistoryError
from processing import attachment_parse, clean_text, identifiers, profile

SKIP_SOURCES = ("reviewed_graph", "primekg_graph")
from collection.catalog import SITE_SOURCES  # noqa: E402  站点插件采集的来源
REGISTRY_IDS = {"nct", "ctr", "chictr"}
REGISTRY_SOURCES = {"clinicaltrials", "chictr", "cde_ctr"}


CHUNK_SEQS = 1000
LEASE_SECONDS = 600
MAX_CHUNK_ATTEMPTS = 3


def stage_key() -> str:
    """阶段键 = 本阶段用到的所有处理器版本。任何一个升级，就是一套新的分块，从头重算（旧结果保留）。"""
    parts = [clean_text.PROCESSOR_VERSION, attachment_parse.PROCESSOR_VERSION, identifiers.PROCESSOR_VERSION,
             profile.PROCESSOR_VERSION]
    return "clean_stage:" + hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:24]


def _cleaner_input(row):
    """(清洗器的输入, 定位基准)。输入决定结果：同样的输入不重算（v3 3.3“原文没变就复用”）。"""
    source, facts = row["source_id"], row["facts"] or {}
    if source == "clinicaltrials":
        return {"facts": facts, "locators": row["locators"] or {}, "summary": row["clean_text"]}, "registry_fields"
    if source == "europe_pmc":
        # 只取清洗器用到的字段：只改引用数等元数据的新版本（EPMC 平均每篇 8 个版本）输入相同，直接复用。
        # 定位用 JSON 路径（/title、/abstractText），不随其他字段变化移动，在每个版本上都能回指。
        try:
            data = json.loads(row["raw"] or "{}")
        except ValueError:
            return {"invalid_raw_sha256": hashlib.sha256((row["raw"] or "").encode()).hexdigest(),
                    "raw": row["raw"] or ""}, "raw_json"
        return {"title": data.get("title"), "abstractText": data.get("abstractText")}, "literature_fields"
    if source in SITE_SOURCES and facts.get("role") == "web_capture":
        try:
            markdown = (json.loads(row["raw"] or "{}").get("parsed") or {}).get("markdown") or ""
        except ValueError:
            markdown = ""
        return {"markdown": markdown, "source_id": source}, "raw_json:/parsed/markdown"
    return {"markdown": row["clean_text"] or "", "source_id": source}, "projection_clean_text"


def _run_cleaner(source, inp, basis):
    if basis == "registry_fields":
        return clean_text.clean_registry(inp["facts"], inp["locators"], summary=inp["summary"])
    if basis == "raw_json":
        return clean_text.clean_literature(inp["raw"])
    if basis == "literature_fields":
        out = clean_text.clean_literature(json.dumps({k: v for k, v in inp.items() if v is not None},
                                                     ensure_ascii=False))
        for seg in out["segments"]:
            seg["source"] = {"json_pointer": (seg.get("source") or {}).get("json_pointer")
                             or ("/title" if seg.get("kind") == "title" else "/abstractText")}
        return out
    return clean_text.clean_markdown(inp["markdown"], source_id=source)


def _final_attachment_texts(conn, version_ids):
    """附件当前解析器版本下的最终结果：{version_id: row}；还在重试中的不出现（等）。"""
    from db.derived_text import MAX_ATTEMPTS

    if not version_ids:
        return {}
    rows = conn.execute(text(
        "SELECT DISTINCT ON (version_id) version_id,status,text,attempt FROM derived_clean_text "
        "WHERE version_id=ANY(:ids) AND processor_version=:pv ORDER BY version_id, attempt DESC"
    ), {"ids": list(version_ids), "pv": attachment_parse.PROCESSOR_VERSION}).mappings().all()
    return {r["version_id"]: dict(r) for r in rows
            if r["status"] in ("parsed", "unsupported") or r["attempt"] >= MAX_ATTEMPTS}


# 只取清洗需要的字段；原件只在 EPMC 和站点网页版本上取（其余来源的原件不读，省 IO）
_CHUNK_SQL = """
    SELECT version_id, source_id, is_demo, source_record_id, record_kind, published_seq,
           payload->'facts' AS facts, payload->'field_locators' AS locators, payload->>'clean_text' AS clean_text,
           CASE WHEN source_id='europe_pmc' OR payload->'facts'->>'role'='web_capture'
                THEN payload->>'raw_content' END AS raw
    FROM history_versions
    WHERE source_id=:source AND published_seq BETWEEN :start AND :end
    ORDER BY published_seq
"""


def _claim(engine, key, chunk_seqs, lease_seconds):
    """认领一块：先接过期/可重试的块，再在某个来源的末尾开新块。返回块或 None（没有活了）。"""
    token = uuid4().hex
    with engine.begin() as conn:
        again = conn.execute(text("""
            UPDATE derived_stage_chunks SET state='leased', lease_token=:t, attempts=attempts+1,
                   lease_until=clock_timestamp()+make_interval(secs => :s), updated_at=clock_timestamp()
            WHERE (stage_key, source_id, chunk_start) = (
              SELECT stage_key, source_id, chunk_start FROM derived_stage_chunks
              WHERE stage_key=:k AND attempts < :max
                AND ((state='leased' AND lease_until < clock_timestamp()) OR state='failed')
              ORDER BY chunk_start LIMIT 1 FOR UPDATE SKIP LOCKED)
            RETURNING source_id, chunk_start, chunk_end, lease_token
        """), {"t": token, "s": lease_seconds, "k": key, "max": MAX_CHUNK_ATTEMPTS}).mappings().first()
        if again:
            return dict(again)
        sources = conn.execute(text("""
            SELECT s.source_id, s.published_seq,
                   COALESCE((SELECT max(chunk_end) FROM derived_stage_chunks c
                             WHERE c.stage_key=:k AND c.source_id=s.source_id), 0) AS covered
            FROM history_sources s WHERE s.published_seq > 0 AND NOT (s.source_id = ANY(:skip))
            ORDER BY s.source_id
        """), {"k": key, "skip": list(SKIP_SOURCES)}).mappings().all()
    for src in sources:
        if src["covered"] >= src["published_seq"]:
            continue
        start = src["covered"] + 1
        end = min(src["published_seq"], start + chunk_seqs - 1)  # 不越过当前已发布的位置，后来发布的进下一块
        with engine.begin() as conn:
            got = conn.execute(text("""
                INSERT INTO derived_stage_chunks
                  (stage_key, source_id, chunk_start, chunk_end, state, lease_token, lease_until, attempts)
                VALUES (:k, :source, :start, :end, 'leased', :t, clock_timestamp()+make_interval(secs => :s), 1)
                ON CONFLICT DO NOTHING RETURNING source_id, chunk_start, chunk_end, lease_token
            """), {"k": key, "source": src["source_id"], "start": start, "end": end, "t": token,
                   "s": lease_seconds}).mappings().first()
        if got:
            return dict(got)
        return _claim(engine, key, chunk_seqs, lease_seconds)  # 别的进程刚开了这一块，重新看
    return None


def _process_rows(conn, key, rows, counts):
    """一块版本：清洗（按输入复用）、编号、画像，批量写入。附件没解析完的记等待表。"""
    work = [r for r in rows if r["record_kind"] != "graph" and r["source_id"] not in SKIP_SOURCES]
    attachments = [r["version_id"] for r in work if (r["facts"] or {}).get("role") == "attachment"]
    finals = _final_attachment_texts(conn, attachments)
    prepared, inputs = [], {}
    for r in work:
        if (r["facts"] or {}).get("role") == "attachment":
            done = finals.get(r["version_id"])
            if done is None:
                conn.execute(text(
                    "INSERT INTO derived_stage_waiting (stage_key, version_id, reason) VALUES (:k,:v,'attachment_text') "
                    "ON CONFLICT DO NOTHING"), {"k": key, "v": r["version_id"]})
                counts["waiting_for_attachment_text"] += 1
                continue
            prepared.append((r, None, None, done))
            continue
        inp, basis = _cleaner_input(r)
        sha = hashlib.sha256(json.dumps(inp, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        inputs[sha] = None
        prepared.append((r, basis, sha, inp))
    # 已有结果（同样输入、同一清洗器版本）直接复用
    if inputs:
        for hit in conn.execute(text(
            "SELECT DISTINCT ON (input_sha256) input_sha256,text,segments,language,warnings FROM derived_clean_text "
            "WHERE input_sha256=ANY(:shas) AND processor_version=:pv"
        ), {"shas": list(inputs), "pv": clean_text.PROCESSOR_VERSION}).mappings():
            inputs[hit["input_sha256"]] = {"text": hit["text"], "segments": hit["segments"], "language": hit["language"],
                                           "warnings": hit["warnings"], "processor_version": clean_text.PROCESSOR_VERSION}
    clean_rows, id_rows, profile_rows = [], [], []
    for r, basis, sha, extra in prepared:
        facts = r["facts"] or {}
        if basis is None:  # 附件：正文来自加工层
            done = extra
            body = done["text"] if done["status"] == "parsed" else ""
            text_pv, warnings, parse_status = attachment_parse.PROCESSOR_VERSION, [], done["status"]
        else:
            result = inputs.get(sha)
            if result is None:
                result = _run_cleaner(r["source_id"], extra, basis)
                inputs[sha] = result
                counts["cleaned"] += 1
            else:
                counts["reused"] += 1
            body, text_pv = result["text"], clean_text.PROCESSOR_VERSION
            warnings, parse_status = list(result.get("warnings") or []), None
            clean_rows.append({
                "v": r["version_id"], "pv": text_pv, "source": r["source_id"], "demo": r["is_demo"],
                "native": r["source_record_id"], "status": "parsed" if body else "failed", "method": basis,
                "error": None if body else ",".join(warnings or ["no_body"]), "text": body, "chars": len(body),
                "sha": hashlib.sha256(body.encode()).hexdigest(),
                "kind": "json_pointer" if basis in ("registry_fields", "literature_fields") else "char_span",
                "segments": json.dumps(result.get("segments") or [], ensure_ascii=False),
                "lang": result.get("language"), "warnings": json.dumps(warnings), "input": sha,
            })
        found = identifiers.extract(body) if body else []
        for hit in found:
            id_rows.append({"v": r["version_id"], "pv": identifiers.PROCESSOR_VERSION, "tpv": text_pv,
                            "kind": hit["kind"], "value": hit["value"], "s": hit["start"], "e": hit["end"]})
        pub = facts.get("publication_types")
        pub_types = pub.get("pubType") if isinstance(pub, dict) else pub
        prof = profile.profile(source_id=r["source_id"], role=facts.get("role"), chars=len(body), warnings=warnings,
                               identifiers=found, pub_types=pub_types, parse_status=parse_status)
        profile_rows.append({"v": r["version_id"], "pv": prof["processor_version"], "tpv": text_pv,
                             "source": r["source_id"], "demo": r["is_demo"], "doc": prof["doc_type"],
                             "ev": json.dumps(prof["evidence_types"]), "dec": json.dumps(prof["decisions"]),
                             "q": json.dumps(prof["quality"], ensure_ascii=False), "route": prof["route"],
                             "reasons": json.dumps(prof["route_reasons"], ensure_ascii=False)})
        counts[prof["route"]] = counts.get(prof["route"], 0) + 1
    if clean_rows:
        conn.execute(text("""
            INSERT INTO derived_clean_text
              (version_id,processor_version,attempt,source_id,is_demo,source_record_id,status,parse_method,
               parse_attempts,error,file_ext,text,chars,text_sha256,locator_kind,segments,language,warnings,input_sha256)
            SELECT :v,:pv,1,:source,:demo,:native,:status,:method,'[]'::jsonb,:error,NULL,:text,:chars,:sha,:kind,
                   CAST(:segments AS jsonb),:lang,CAST(:warnings AS jsonb),:input
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
              (version_id,processor_version,text_processor_version,source_id,is_demo,doc_type,evidence_types,
               decisions,quality,route,route_reasons)
            SELECT :v,:pv,:tpv,:source,:demo,:doc,CAST(:ev AS jsonb),CAST(:dec AS jsonb),CAST(:q AS jsonb),
                   :route,CAST(:reasons AS jsonb)
            WHERE NOT EXISTS (SELECT 1 FROM derived_profiles WHERE version_id=:v AND processor_version=:pv
                              AND text_processor_version=:tpv)
        """), profile_rows)
        counts["profiled"] += len(profile_rows)


def _process_waiting(engine, key, counts, limit=500):
    """等待表里的附件：解析完的补做画像并移出等待表。"""
    with engine.begin() as conn:
        waiting = conn.execute(text(
            "SELECT w.version_id FROM derived_stage_waiting w WHERE w.stage_key=:k LIMIT :n"
        ), {"k": key, "n": limit}).scalars().all()
        finals = _final_attachment_texts(conn, waiting)
        if not finals:
            return
        rows = conn.execute(text(_CHUNK_SQL.replace(
            "WHERE source_id=:source AND published_seq BETWEEN :start AND :end", "WHERE version_id=ANY(:ids)")),
            {"ids": list(finals)}).mappings().all()
        _process_rows(conn, key, [dict(r) for r in rows], counts)
        conn.execute(text("DELETE FROM derived_stage_waiting WHERE stage_key=:k AND version_id=ANY(:ids)"),
                     {"k": key, "ids": list(finals)})


def process(*, batch=None, time_budget_seconds=600, chunk_seqs=CHUNK_SEQS, lease_seconds=LEASE_SECONDS,
            max_chunks=None):
    """认领并处理块，直到没有活或到时间。可多进程/多主机同时跑：块认领带租约，互不重复。
    batch 参数保留兼容（旧调用按版本数），不再使用。"""
    from db.history import history_engine

    engine = history_engine()
    key = stage_key()
    began = time.monotonic()
    counts = {"profiled": 0, "cleaned": 0, "reused": 0, "waiting_for_attachment_text": 0, "chunks": 0,
              "failed_chunks": 0, "quarantine": 0, "weak_signal": 0, "normal": 0}
    _process_waiting(engine, key, counts)
    while time.monotonic() - began < time_budget_seconds and (max_chunks is None or counts["chunks"] < max_chunks):
        chunk = _claim(engine, key, chunk_seqs, lease_seconds)
        if chunk is None:
            break
        started = time.monotonic()
        before = dict(counts)
        try:
            with engine.begin() as conn:
                conn.execute(text("SET LOCAL statement_timeout='120000ms'"))
                held = conn.execute(text(
                    "SELECT lease_token FROM derived_stage_chunks WHERE stage_key=:k AND source_id=:s "
                    "AND chunk_start=:c FOR UPDATE"), {"k": key, "s": chunk["source_id"], "c": chunk["chunk_start"]}
                ).scalar()
                if held != chunk["lease_token"]:
                    continue  # 租约已被别人接走：这块不写
                rows = conn.execute(text(_CHUNK_SQL), {"source": chunk["source_id"], "start": chunk["chunk_start"],
                                                       "end": chunk["chunk_end"]}).mappings().all()
                _process_rows(conn, key, [dict(r) for r in rows], counts)
                stats = {k: counts[k] - before.get(k, 0) for k in counts if counts[k] != before.get(k, 0)}
                stats["seconds"] = round(time.monotonic() - started, 3)
                stats["versions"] = len(rows)
                conn.execute(text(
                    "UPDATE derived_stage_chunks SET state='done', stats=CAST(:st AS jsonb), lease_until=NULL, "
                    "error=NULL, updated_at=clock_timestamp() WHERE stage_key=:k AND source_id=:s AND chunk_start=:c"),
                    {"st": json.dumps(stats), "k": key, "s": chunk["source_id"], "c": chunk["chunk_start"]})
            counts["chunks"] += 1
        except Exception as exc:  # noqa: BLE001 -- 块失败进死信（state=failed，带原因），可重试有上限
            counts = {**before, "chunks": counts["chunks"], "failed_chunks": counts["failed_chunks"] + 1}
            with engine.begin() as conn:
                conn.execute(text(
                    "UPDATE derived_stage_chunks SET state='failed', error=CAST(:e AS jsonb), lease_until=NULL, "
                    "updated_at=clock_timestamp() WHERE stage_key=:k AND source_id=:s AND chunk_start=:c "
                    "AND lease_token=:t"),
                    {"e": json.dumps({"type": type(exc).__name__, "message": str(exc)[:300]}), "k": key,
                     "s": chunk["source_id"], "c": chunk["chunk_start"], "t": chunk["lease_token"]})
    return counts


def stage_status():
    """每个来源的进度：已发布到哪、连续处理到哪（水位）、落后多少、在处理/失败的块、等待解析的附件、
    最近块的吞吐。"""
    from db.history import history_engine

    key = stage_key()
    with history_engine().begin() as conn:
        rows = conn.execute(text("""
            SELECT s.source_id, s.published_seq,
              (SELECT count(*) FROM derived_stage_chunks c WHERE c.stage_key=:k AND c.source_id=s.source_id
                 AND c.state='leased') AS leased,
              (SELECT count(*) FROM derived_stage_chunks c WHERE c.stage_key=:k AND c.source_id=s.source_id
                 AND c.state='failed') AS failed,
              (SELECT COALESCE(sum((stats->>'versions')::int),0) FROM derived_stage_chunks c WHERE c.stage_key=:k
                 AND c.source_id=s.source_id AND c.state='done') AS done_versions,
              (SELECT COALESCE(sum((stats->>'seconds')::float),0) FROM derived_stage_chunks c WHERE c.stage_key=:k
                 AND c.source_id=s.source_id AND c.state='done') AS done_seconds
            FROM history_sources s WHERE s.published_seq > 0 AND NOT (s.source_id = ANY(:skip)) ORDER BY 1
        """), {"k": key, "skip": list(SKIP_SOURCES)}).mappings().all()
        done = conn.execute(text(
            "SELECT source_id, chunk_start, chunk_end FROM derived_stage_chunks WHERE stage_key=:k AND state='done' "
            "ORDER BY source_id, chunk_start"), {"k": key}).mappings().all()
        waiting = conn.execute(text("SELECT count(*) FROM derived_stage_waiting WHERE stage_key=:k"),
                               {"k": key}).scalar()
    marks = {}
    for d in done:  # 连续完成的最高位置
        mark = marks.get(d["source_id"], 0)
        if d["chunk_start"] == mark + 1:
            marks[d["source_id"]] = d["chunk_end"]
    out = []
    for r in rows:
        mark = marks.get(r["source_id"], 0)
        rate = r["done_versions"] / r["done_seconds"] if r["done_seconds"] else None
        out.append({**{k: r[k] for k in ("source_id", "published_seq", "leased", "failed")}, "watermark": mark,
                    "lag": r["published_seq"] - mark, "versions_per_second": round(rate, 1) if rate else None})
    return {"stage_key": key, "sources": out, "waiting_attachments": waiting}


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

