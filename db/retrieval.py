"""检索入口（v3 3.6）：WeKnora 召回候选 → Fabric 逐条校验 → 只有通过校验的片段才重排、才交给模型读。

- 召回：经桥接服务在各索引目标的知识库里整库检索（关键词 + 向量，RRF 合并），不重排、不扩上下文。
- 校验（逐条，全部用 Fabric 自己的记录，命中里的 metadata 只当指针）：
  1. 来源当前可读（history 的许可与开关；不可读的只计数，不回任何内容）；
  2. 该记录在本次模式和截止时间下可见的版本（history.visible_versions，与 list_records 同一规则）；
  3. 那个可见版本的当前清洗产物就是被索引的这份正文（处理器版本 + 正文摘要一致）；
  4. 命中块确实是这份固定正文里的一段（定位到偏移，再经段落定位回到原件）。
  返回的时间、原件摘要、画像、定位都取实际可见的版本；被索引的代表版本另外写明（indexed_as）。
- 重排：只把通过校验的片段交给桥接 /rerank（同一套模型配置，报告用量）。
- 如实：候选有上限，过滤后不足 top_k 就写明不足和各原因的计数；没被索引或没搜到不等于不存在。
"""

from __future__ import annotations

import re
from collections import Counter

from sqlalchemy import text

from biz_rules.history_models import HistoryError
from db import history
from db import index_publication as index

SCHEMA = "datafabric.retrieval.v1"
MODES = ("current", "operational_replay", "public_history_reconstruction")
CANDIDATES = 60  # 每个知识库召回的候选上限（桥接服务的上限）
MAX_EXCLUDED_ITEMS = 50
_FIXED_LINE = re.compile(r"\A\s*<!-- Fabric fixed version [0-9a-f]{64} -->\s*")
_WITHDRAWN = {"withdrawn", "deleted", "retracted"}


def locate(fixed, chunk):
    """命中块在固定正文里的位置 (start, end)；找不到返回 None。先按原样找；分块时空白可能被改写，
    再按“词序一致、空白任意”找。桥接上传时加的固定版本注释行先去掉。"""
    chunk = _FIXED_LINE.sub("", chunk or "").strip()
    if not chunk:
        return None
    at = fixed.find(chunk)
    if at >= 0:
        return at, at + len(chunk)
    words = chunk.split()
    if len(words) > 400:
        return None
    match = re.search(r"\s+".join(re.escape(w) for w in words), fixed)
    return (match.start(), match.end()) if match else None


def _overlapping(segments, start, end):
    return [s for s in segments or [] if s.get("start", 0) < end and s.get("end", 0) > start]


def _versions(conn, version_ids):
    """可见版本的事实（全部取这个版本自己的）：原件摘要、时间、证明、撤回标记、标题。"""
    rows = conn.execute(text("""
        SELECT version_id,payload->>'raw_sha256' AS raw_sha256,first_seen_at,proof_available_at,
               payload->>'title' AS title,payload->'source_published_at' AS published_at,
               payload->'availability_proof' AS proof,payload->'facts'->>'publication_state' AS publication_state
        FROM history_versions WHERE version_id=ANY(:ids)
    """), {"ids": list(version_ids)}).mappings()
    return {row["version_id"]: dict(row) for row in rows}


def _artifact_rows(conn, pairs):
    """(版本, 处理器版本, 摘要) → 那一份清洗产物的正文和段落定位（同一处理器版本下最新一次成功结果）。"""
    out = {}
    for version_id, processor, digest in pairs:
        row = conn.execute(text(
            "SELECT text,segments,locator_kind FROM derived_clean_text WHERE version_id=:v AND processor_version=:pv "
            "AND status='parsed' AND text_sha256=:sha ORDER BY attempt DESC LIMIT 1"
        ), {"v": version_id, "pv": processor, "sha": digest}).mappings().first()
        if row:
            out[version_id] = dict(row)
    return out


def verify(candidates, mode, cutoff, *, doc_categories=(), evidence_types=()):
    """逐条校验候选 → (通过的条目, 剔除计数, 可读来源里剔除的条目)。"""
    verified, counts, items = [], Counter(), []

    def exclude(hit, reason, readable=True):
        counts[reason] += 1
        if readable and len(items) < MAX_EXCLUDED_ITEMS:
            ident = hit["identity"]
            items.append({"source_id": ident["source_id"], "source_record_id": ident["source_record_id"],
                          "indexed_version_id": ident["version_id"], "reason": reason})

    groups = {}
    for hit in candidates:
        groups.setdefault((hit["identity"]["source_id"], bool(hit.get("is_demo"))), []).append(hit)
    for (source_id, is_demo), hits in groups.items():
        records = sorted({h["identity"]["source_record_id"] for h in hits})
        try:
            visible = {}
            for i in range(0, len(records), 200):
                visible.update(history.visible_versions(source_id, is_demo, mode, cutoff, records[i:i + 200]))
        except HistoryError as exc:
            if exc.status == 403:  # 来源当前不可读：只计数，不回标题、正文、定位
                for hit in hits:
                    exclude(hit, "not_readable", readable=False)
                continue
            if exc.code == "graph_history_unsupported":
                for hit in hits:
                    exclude(hit, "mode_unsupported_for_source")
                continue
            raise
        with history.history_engine().begin() as conn:
            facts = _versions(conn, set(visible.values()))
            current = index.current_artifacts(conn, set(visible.values()))
            needed = set()
            for hit in hits:
                vid = visible.get(hit["identity"]["source_record_id"])
                key = hit["identity"].get("artifact_key")
                if vid and key and vid in current and index.artifact_key(current[vid]) == key:
                    needed.add((vid, current[vid]["processor_version"], current[vid]["text_sha256"]))
            artifacts = _artifact_rows(conn, needed)
        for hit in hits:
            ident = hit["identity"]
            vid = visible.get(ident["source_record_id"])
            if vid is None:
                exclude(hit, "not_visible_at_cutoff")
                continue
            fact = facts[vid]
            if fact["publication_state"] in _WITHDRAWN:
                exclude(hit, "withdrawn")
                continue
            key = ident.get("artifact_key")
            art = current.get(vid)
            if not key or art is None or index.artifact_key(art) != key or vid not in artifacts:
                # 此刻可见的版本，正文已不是被索引的这一份（更正后的版本，或改造前的投影待办）
                exclude(hit, "changed_at_cutoff")
                continue
            if doc_categories and art["doc_category"] not in doc_categories:
                exclude(hit, "filtered_doc_category")
                continue
            if evidence_types and not set(art["evidence_types"] or []) & set(evidence_types):
                exclude(hit, "filtered_evidence_type")
                continue
            fixed = artifacts[vid]
            span = locate(fixed["text"], hit.get("content"))
            if span is None:
                exclude(hit, "chunk_not_in_fixed_text")
                continue
            start, end = span
            available = fact["proof_available_at"] if mode == "public_history_reconstruction" else fact["first_seen_at"]
            verified.append({
                "source_id": source_id, "is_demo": is_demo, "source_record_id": ident["source_record_id"],
                "version_id": vid, "raw_sha256": fact["raw_sha256"], "title": fact["title"],
                "first_seen_at": fact["first_seen_at"].isoformat(),
                "proof_available_at": fact["proof_available_at"].isoformat() if fact["proof_available_at"] else None,
                "published_at": fact["published_at"],
                "available_at": available.isoformat() if available else None,
                "proof": fact["proof"] if mode == "public_history_reconstruction" else None,
                "text": fixed["text"][start:end], "start": start, "end": end,
                "locator_kind": fixed["locator_kind"], "locators": _overlapping(fixed["segments"], start, end),
                "artifact": {"artifact_key": key, "processor_version": art["processor_version"],
                             "text_sha256": art["text_sha256"]},
                "profile": {"doc_type": art["doc_type"], "doc_category": art["doc_category"],
                            "evidence_types": list(art["evidence_types"] or [])},
                "indexed_as": {"publication_id": hit.get("publication_id"), "version_id": ident["version_id"],
                               "artifact_key": key, "chunk_id": hit.get("chunk_id")},
                "retrieval_modes": hit.get("retrieval_modes") or [], "recall_score": hit.get("score"),
            })
    return verified, counts, items


def _coverage(source_ids):
    """各来源的索引覆盖：物理条目（一条记录的一份正文）按状态计，以及覆盖的记录数。"""
    with history.history_engine().begin() as conn:
        rows = conn.execute(text("""
            SELECT t.config->>'source_id' AS source_id,p.state,count(*) AS entries,
                   count(DISTINCT p.source_record_id) AS records
            FROM index_publications p JOIN index_targets t ON t.id=p.target_id
            WHERE t.enabled AND t.config->>'source_id'=ANY(:sources) GROUP BY 1,2
        """), {"sources": list(source_ids)}).mappings()
        out = {}
        for row in rows:
            entry = out.setdefault(row["source_id"], {"entries_by_state": {}, "ready_records": 0})
            entry["entries_by_state"][row["state"]] = row["entries"]
            if row["state"] == "ready":
                entry["ready_records"] = row["records"]
        return out


def search(query, mode, cutoff, *, sources=(), scopes=(), is_demo=None, doc_categories=(), evidence_types=(),
           top_k=10, client=None):
    """sources / scopes / is_demo 选择检索哪些索引目标（来源、业务范围、演示与否）；其余见模块说明。"""
    if mode not in MODES:
        raise HistoryError("unsupported_mode", "不支持该检索模式", 422)
    if not 1 <= top_k <= 20 or not 1 <= len(query) <= 2000:
        raise HistoryError("query_budget", "检索词须为1至2000字符，top_k须为1至20", 422)
    configured = [t for t in index.targets() if (not sources or t.source_id in sources)
                  and (not scopes or t.source_scope in scopes) and (is_demo is None or t.is_demo == is_demo)]
    if not configured:
        raise HistoryError("search_scope_empty", "所选来源没有配置索引目标", 422)
    groups = {}
    for target in configured:
        groups.setdefault((target.knowledge_base_id, target.source_scope, target.is_demo), set()).add(
            (target.source_instance, target.source_id))
    owned = client is None
    client = client or index.WeKnoraIndexClient()
    try:
        candidates, recall = [], []
        for (kb, scope, is_demo), allowed in groups.items():
            answer = client.search({"knowledge_base_id": kb, "source_scope": scope, "is_demo": is_demo,
                                    "query_text": query, "top_k": CANDIDATES})
            recall.append({"knowledge_base_id": kb, "source_scope": scope, "is_demo": is_demo,
                           "returned": len(answer.get("results") or []), "execution": answer.get("execution")})
            for hit in answer.get("results") or []:
                ident = hit.get("identity") or {}
                if (ident.get("source_instance"), ident.get("source_id")) in allowed:
                    candidates.append(hit)
        verified, counts, items = verify(candidates, mode, cutoff, doc_categories=doc_categories,
                                         evidence_types=evidence_types)
        # 去重：同一可见版本的同一段只留召回分数最高的一条
        best = {}
        for item in verified:
            key = (item["version_id"], item["start"], item["end"])
            if key not in best or (item["recall_score"] or 0) > (best[key]["recall_score"] or 0):
                best[key] = item
        verified = list(best.values())
        rerank = {"state": "not_run"}
        if verified:
            by_group = {}
            for item in verified:
                target = next(t for t in configured if t.source_id == item["source_id"] and t.is_demo == item["is_demo"])
                by_group.setdefault((target.knowledge_base_id, target.source_scope, target.is_demo), []).append(item)
            try:
                usage = []
                for (kb, scope, is_demo), items_ in by_group.items():
                    answer = client.rerank({"knowledge_base_id": kb, "source_scope": scope, "is_demo": is_demo,
                                            "query_text": query, "documents": [i["text"][:8000] for i in items_]})
                    for score in answer["scores"]:
                        items_[score["index"]]["rerank_score"] = score["relevance_score"]
                    usage.append({"rerank_model": answer.get("rerank_model"), "usage": answer.get("usage"),
                                  "usage_status": answer.get("usage_status")})
                verified.sort(key=lambda i: (-i["rerank_score"], i["version_id"], i["start"]))
                rerank = {"state": "succeeded", "calls": usage}
            except HistoryError as exc:
                verified.sort(key=lambda i: (-(i["recall_score"] or 0), i["version_id"], i["start"]))
                rerank = {"state": "failed", "code": exc.code, "fallback": "reciprocal_rank_fusion"}
        results = verified[:top_k]
    finally:
        if owned:
            client.close()
    return {
        "schema_version": SCHEMA, "query": query, "mode": mode, "cutoff": history._cutoff(cutoff).isoformat(),
        "top_k": top_k, "results": results,
        "excluded": {"counts": dict(counts), "items": items},
        "execution": {"candidates": len(candidates), "verified": len(verified), "returned": len(results),
                      "insufficient": len(results) < top_k, "candidate_budget_per_knowledge_base": CANDIDATES,
                      "recall": recall, "rerank": rerank},
        "coverage": _coverage({t.source_id for t in configured}),
        "limitations": [
            "命中只是指针：每条都按本次模式和截止时间回 Fabric 校验了来源许可、可见版本和固定正文",
            "候选有上限；过滤后不足 top_k 是这次召回的结果，不代表库里没有更多相关材料",
            "没被索引或没搜到不等于不存在；覆盖见 coverage（ready 的才可检索）",
        ],
    }
