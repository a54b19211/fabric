"""检索入口（v3 3.6）：WeKnora 召回候选 → Fabric 逐条校验 → 只有通过校验的片段才重排、才交给模型读。

- 召回：经桥接服务在各索引目标的知识库里整库检索（关键词 + 向量，RRF 合并），不重排、不扩上下文。
- 校验（逐条，全部用 Fabric 自己的记录，命中里的 metadata 只当指针）：
  1. 来源当前可读（资料库的许可与开关；不可读的只计数，不回任何内容）；
  2. 该记录在本次模式和截止时间下可见的版本（records.visible_versions，与 list_records 同一规则）；
  3. 那个可见版本的当前清洗产物就是被索引的这份正文（处理器版本 + 正文摘要一致）；
  4. 命中块确实是这份固定正文里的一段（定位到偏移，再经段落定位回到原件）。
  返回的时间、原件摘要、画像、定位都取实际可见的版本；被索引的代表版本另外写明（indexed_as）。
- 重排：只把通过校验的片段交给桥接 /rerank（同一套模型配置，报告用量）。
- 如实：候选有上限，过滤后不足 top_k 就写明不足和各剔除原因的条数；没被索引或没搜到不等于不存在。
"""

from __future__ import annotations

import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text

from fetch.catalog import RETIRED
from store import index_pieces, records
from store import index_publication as index
from store.record_models import FabricError

SCHEMA = "datafabric.retrieval.v1"
MODES = ("current", "operational_replay", "public_history_reconstruction")
CANDIDATES = 60  # 每个知识库一次召回的候选上限（桥接服务的上限）
RERANK_PASSAGE_CHARS = 8000  # one passage is cut to this many characters before it is sent to the rerank model
_FIXED_LINE = re.compile(r"\A\s*<!-- Fabric fixed version [0-9a-f]{64} -->\s*")
_WITHDRAWN = {"withdrawn", "deleted", "retracted"}


def _find(fixed, chunk):
    at = fixed.find(chunk)
    if at >= 0:
        return at, at + len(chunk)
    words = chunk.split()
    if not words or len(words) > 400:
        return None
    match = re.search(r"\s+".join(re.escape(w) for w in words), fixed)
    return (match.start(), match.end()) if match else None


_TABLE_RULE = re.compile(r"^\|?\s*:?-{3,}")


def locate(fixed, chunk):
    """命中块在固定正文里的位置 (start, end)；找不到返回 None。先按原样找；分块时空白可能被改写，
    再按“词序一致、空白任意”找。桥接上传时加的固定版本注释行先去掉。
    长表格分块时 WeKnora 会在每块前面重复表头（表头行 + 分隔行）：表头确实在正文里时，定位表头之后的行。"""
    chunk = _FIXED_LINE.sub("", chunk or "").strip()
    if not chunk:
        return None
    span = _find(fixed, chunk)
    if span is None:
        lines = chunk.split("\n")
        if len(lines) > 2 and _TABLE_RULE.match(lines[1].strip()) and _find(fixed, "\n".join(lines[:2])):
            span = _find(fixed, "\n".join(lines[2:]).strip())
    return span


def _overlapping(segments, start, end):
    return [s for s in segments or [] if s.get("start", 0) < end and s.get("end", 0) > start]


def _versions(conn, version_ids):
    """可见版本的事实（全部取这个版本自己的）：原件摘要、时间、证明、撤回标记、标题。"""
    rows = conn.execute(text("""
        SELECT version_id,payload->>'raw_sha256' AS raw_sha256,first_seen_at,proof_available_at,
               payload->>'title' AS title,payload->'source_published_at' AS published_at,
               payload->'availability_proof' AS proof,payload->'facts'->>'publication_state' AS publication_state
        FROM fabric_versions WHERE version_id=ANY(:ids)
    """), {"ids": list(version_ids)}).mappings()
    return {row["version_id"]: dict(row) for row in rows}


def _artifact_rows(conn, triples):
    """(版本, 处理器版本, 摘要) → 那一份清洗产物的正文和段落定位（同一处理器版本下最新一次成功结果）。"""
    out = {}
    for version_id, processor, digest in triples:
        row = index.artifact_row(conn, version_id, processor + ":" + digest)
        if row:
            out[(version_id, processor, digest)] = row
    return out


def _serving_old(conn, target_id, record_id, current, hit_key, hit_piece_key):
    """处理器交接期（与 worker 的 retiring 同一判定 index.artifact_ready）：当前产物还没整份 ready（当前分片计划
    的每一片都 ready 才算），而被命中的旧条目那一片仍 ready，旧条目继续可检索；当前产物一旦整份 ready，只认它。"""
    hit_state = conn.execute(text(
        "SELECT state FROM index_publications WHERE target_id=:t AND source_record_id=:n AND artifact_key=:k "
        "AND piece_key=:p"
    ), {"t": target_id, "n": record_id, "k": hit_key, "p": hit_piece_key}).scalar()
    return hit_state == "ready" and not index.artifact_ready(conn, target_id, record_id, current)


def _locate_in(full, chunk, piece):
    """命中块 → 全篇偏移。分片的命中先在本片的全篇范围里找，找不到再连同上下文前缀（表头）一起找，
    落在前缀里的部分不算（前缀不是这一段原件内容）。"""
    if not piece:
        return locate(full, chunk)
    start, end = piece["start"], piece["end"]
    span = locate(full[start:end], chunk)
    if span:
        return span[0] + start, span[1] + start
    ctx = index_pieces.context(full, start)
    span = locate(ctx + full[start:end], chunk) if ctx else None
    if span and span[1] > len(ctx):
        return max(span[0], len(ctx)) - len(ctx) + start, span[1] - len(ctx) + start
    return None


def _by_source(candidates):
    """(source, is_demo) -> its candidates, in candidate order."""
    groups = {}
    for hit in candidates:
        groups.setdefault((hit["identity"]["source_id"], bool(hit.get("is_demo"))), []).append(hit)
    return groups


def _visible_records(source_id, is_demo, mode, cutoff, hits, excluded):
    """record -> its version visible in this mode at the cutoff; None when the whole group is excluded (the source
    is not readable now, or the mode is not supported for it; excluded counts them by reason)."""
    record_ids = sorted({h["identity"]["source_record_id"] for h in hits})
    try:
        visible = {}
        for i in range(0, len(record_ids), 200):
            visible.update(records.visible_versions(source_id, is_demo, mode, cutoff, record_ids[i:i + 200]))
    except FabricError as exc:
        if exc.status == 403:  # 来源当前不可读：只计数，不回标题、正文、定位
            excluded["not_readable"] += len(hits)
            return None
        if exc.code == "graph_history_unsupported":
            excluded["mode_unsupported_for_source"] += len(hits)
            return None
        raise
    return visible


def _needed_artifacts(conn, source_id, is_demo, hits, visible, current, target_ids):
    """(the (version, processor, digest) artifacts the hits are read from, the (version, key) hits served by an
    older processor's entry during a handover)."""
    needed, serving = set(), set()
    for hit in hits:
        record_id = hit["identity"]["source_record_id"]
        vid, key = visible.get(record_id), hit["identity"].get("artifact_key")
        if not (vid and key and vid in current):
            continue
        current_key = index.artifact_key(current[vid])
        hit_piece = index_pieces.key(hit["identity"].get("piece"))
        if key == current_key or (target_ids.get((source_id, is_demo)) and _serving_old(
                conn, target_ids[(source_id, is_demo)], record_id, current[vid], key, hit_piece)):
            # 旧条目只有在它的正文也是这个可见版本自己产出的（同一版本、旧处理器）时才认；
            # 读取的正文取那一份产物本身，确认不了就剔除
            needed.add((vid, *key.rsplit(":", 1)))
            if key != current_key:
                serving.add((vid, key))
    return needed, serving


def _group_state(source_id, is_demo, hits, visible, target_ids):
    """One transaction: the visible versions' facts, their current artifacts, and the artifacts the hits name."""
    with records.fabric_engine().begin() as conn:
        facts = _versions(conn, set(visible.values()))
        current = index.current_artifacts(conn, set(visible.values()))
        needed, serving = _needed_artifacts(conn, source_id, is_demo, hits, visible, current, target_ids)
        artifacts = _artifact_rows(conn, needed)
    return facts, current, artifacts, serving


def _verify_hit(hit, visible, state, doc_categories, evidence_types):
    """The reason the hit is excluded, or (version, fact, key, artifact, fixed text row, span)."""
    facts, current, artifacts, _ = state
    ident = hit["identity"]
    vid = visible.get(ident["source_record_id"])
    if vid is None:
        return "not_visible_at_cutoff"
    fact = facts[vid]
    if fact["publication_state"] in _WITHDRAWN:
        return "withdrawn"
    key = ident.get("artifact_key")
    art = current.get(vid)
    fixed = artifacts.get((vid, *key.rsplit(":", 1))) if key else None
    if art is None or fixed is None:
        # 此刻可见的版本，正文已不是被索引的这一份（更正后的版本，或改造前的投影待办）
        return "changed_at_cutoff"
    if doc_categories and art["doc_category"] not in doc_categories:
        return "filtered_doc_category"
    if evidence_types and not set(art["evidence_types"] or []) & set(evidence_types):
        return "filtered_evidence_type"
    span = _locate_in(fixed["text"], hit.get("content"), ident.get("piece"))
    if span is None:
        return "chunk_not_in_fixed_text"
    return vid, fact, key, art, fixed, span


def _verified_item(hit, source_id, is_demo, mode, checked, serving):
    vid, fact, key, art, fixed, (start, end) = checked
    ident = hit["identity"]
    processor, digest = key.rsplit(":", 1)
    available = fact["proof_available_at"] if mode == "public_history_reconstruction" else fact["first_seen_at"]
    return {
        "source_id": source_id, "is_demo": is_demo, "source_record_id": ident["source_record_id"],
        "version_id": vid, "raw_sha256": fact["raw_sha256"], "title": fact["title"],
        "first_seen_at": fact["first_seen_at"].isoformat(),
        "proof_available_at": fact["proof_available_at"].isoformat() if fact["proof_available_at"] else None,
        "published_at": fact["published_at"],
        "available_at": available.isoformat() if available else None,
        "proof": fact["proof"] if mode == "public_history_reconstruction" else None,
        # 复现引用（v3 6.1）：可见版本、加工产物（处理器版本 + 正文摘要）、读到的片段、偏移与定位
        "text": fixed["text"][start:end], "start": start, "end": end,
        "locator_kind": fixed["locator_kind"], "locators": _overlapping(fixed["segments"], start, end),
        "artifact": {"artifact_key": key, "processor_version": processor, "text_sha256": digest,
                     # 处理器交接期：新产物的条目还没 ready，读的是这个版本在旧处理器下的产物
                     "handover": (vid, key) in serving},
        "profile": {"doc_type": art["doc_type"], "doc_category": art["doc_category"],
                    "evidence_types": list(art["evidence_types"] or [])},
        "indexed_as": {"publication_id": hit.get("publication_id"), "version_id": ident["version_id"],
                       "artifact_key": key, "piece": ident.get("piece"), "chunk_id": hit.get("chunk_id")},
        "retrieval_modes": hit.get("retrieval_modes") or [], "recall_score": hit.get("score"),
    }


def verify(candidates, mode, cutoff, *, doc_categories=(), evidence_types=(), target_ids=None):
    """逐条校验候选 → (通过的条目, 各剔除原因的条数)。target_ids：(来源, is_demo) → 索引目标，
    处理器交接期判断旧条目是否仍在服务要用。"""
    target_ids = target_ids or {}
    verified, excluded = [], Counter()
    for (source_id, is_demo), hits in _by_source(candidates).items():
        visible = _visible_records(source_id, is_demo, mode, cutoff, hits, excluded)
        if visible is None:
            continue
        state = _group_state(source_id, is_demo, hits, visible, target_ids)
        for hit in hits:
            checked = _verify_hit(hit, visible, state, doc_categories, evidence_types)
            if isinstance(checked, str):
                excluded[checked] += 1
                continue
            verified.append(_verified_item(hit, source_id, is_demo, mode, checked, state[3]))
    return verified, excluded


def _without_retired(sources):
    """v3 6.3: a retired source is not recalled and is said so: it is named in the receipt's excluded_sources, none of its
    text reaches rerank, and the other sources of a mixed query stay. Only retired sources chosen: 410. Old ids still
    read through the record API."""
    wanted = set(sources)
    excluded = [{"source_id": s, "reason": "retired"} for s in sorted(wanted & set(RETIRED))]
    if wanted and len(excluded) == len(wanted):
        raise FabricError(*records.LEGACY_RETIRED, 410)
    return tuple(s for s in sources if s not in RETIRED), excluded


def _configured_targets(sources, scopes, is_demo):
    """The enabled index targets the search selects by source, business scope and demo flag."""
    configured = [t for t in index.targets() if (not sources or t.source_id in sources)
                  and (not scopes or t.source_scope in scopes) and (is_demo is None or t.is_demo == is_demo)]
    if not configured:
        raise FabricError("search_scope_empty", "所选来源没有配置索引目标", 422)
    return configured


def _knowledge_bases(configured):
    """(knowledge base, is_demo) -> its allowed (source instance, source) pairs, and -> its business scopes."""
    groups, scopes_of = {}, {}
    for target in configured:
        groups.setdefault((target.knowledge_base_id, target.is_demo), set()).add(
            (target.source_instance, target.source_id))
        scopes_of.setdefault((target.knowledge_base_id, target.is_demo), set()).add(target.source_scope)
    return groups, scopes_of


def _recall(client, query, groups, scopes_of):
    """(the candidates of the selected sources, the recall receipt per knowledge base)."""
    candidates, recall = [], []
    # 每个知识库一次召回，几个知识库之间并行发出
    with ThreadPoolExecutor(max_workers=min(4, len(groups))) as pool:
        answers = list(pool.map(lambda group: client.search({
            "knowledge_base_id": group[0], "source_scopes": sorted(scopes_of[group]), "is_demo": group[1],
            "query_text": query, "top_k": CANDIDATES}), list(groups)))
    for ((kb, is_demo), allowed), answer in zip(groups.items(), answers):
        recall.append({"knowledge_base_id": kb, "source_scopes": sorted(scopes_of[(kb, is_demo)]),
                       "is_demo": is_demo, "returned": len(answer.get("results") or []),
                       "execution": answer.get("execution")})
        for hit in answer.get("results") or []:
            ident = hit.get("identity") or {}
            if (ident.get("source_instance"), ident.get("source_id")) in allowed:
                candidates.append(hit)
    return candidates, recall


def _best_per_passage(verified):
    """去重：同一可见版本的同一段只留召回分数最高的一条"""
    best = {}
    for item in verified:
        key = (item["version_id"], item["start"], item["end"])
        if key not in best or (item["recall_score"] or 0) > (best[key]["recall_score"] or 0):
            best[key] = item
    return list(best.values())


def _rerank(client, query, configured, verified):
    """通过校验的片段按（知识库, 业务范围）分组送桥接 /rerank，按分数排序；重排失败就按召回分数排。返回重排回执：
    state，以及每次调用送了几段、多少字（截到 RERANK_PASSAGE_CHARS 之后）、模型和用量。"""
    if not verified:
        return {"state": "not_run", "calls": []}
    groups = {}
    for item in verified:
        target = next(t for t in configured if t.source_id == item["source_id"] and t.is_demo == item["is_demo"])
        groups.setdefault((target.knowledge_base_id, target.source_scope, target.is_demo), []).append(item)
    calls = []
    try:
        for (kb, scope, is_demo), items in groups.items():
            documents = [i["text"][:RERANK_PASSAGE_CHARS] for i in items]
            calls.append({"knowledge_base_id": kb, "source_scope": scope, "documents": len(documents),
                          "input_chars": sum(map(len, documents))})
            answer = client.rerank({"knowledge_base_id": kb, "source_scope": scope, "is_demo": is_demo,
                                    "query_text": query, "documents": documents})
            for score in answer["scores"]:
                items[score["index"]]["rerank_score"] = score["relevance_score"]
            calls[-1].update(rerank_model=answer.get("rerank_model"), usage=answer.get("usage"))
        verified.sort(key=lambda i: (-i["rerank_score"], i["version_id"], i["start"]))
        return {"state": "succeeded", "calls": calls}
    except FabricError as exc:
        verified.sort(key=lambda i: (-(i["recall_score"] or 0), i["version_id"], i["start"]))
        return {"state": "failed", "code": exc.code, "fallback": "reciprocal_rank_fusion", "calls": calls}


def search(query, mode, cutoff, *, sources=(), scopes=(), is_demo=None, doc_categories=(), evidence_types=(),
           top_k=10, client=None):
    """sources / scopes / is_demo 选择检索哪些索引目标（来源、业务范围、演示与否）；其余见模块说明。"""
    if mode not in MODES:
        raise FabricError("unsupported_mode", "不支持该检索模式", 422)
    if not 1 <= top_k <= 20 or not 1 <= len(query) <= 2000:
        raise FabricError("query_budget", "检索词须为1至2000字符，top_k须为1至20", 422)
    sources, excluded = _without_retired(sources)
    configured = _configured_targets(sources, scopes, is_demo)
    # 一个知识库一次召回，覆盖它在本次检索里的所有业务范围（同库的范围不互相挤占候选）
    groups, scopes_of = _knowledge_bases(configured)
    owned = client is None
    client = client or index.WeKnoraIndexClient()
    try:
        candidates, recall = _recall(client, query, groups, scopes_of)
        verified, counts = verify(candidates, mode, cutoff, doc_categories=doc_categories,
                                         evidence_types=evidence_types,
                                         target_ids={(t.source_id, t.is_demo): t.key() for t in configured})
        verified = _best_per_passage(verified)
        rerank = _rerank(client, query, configured, verified)
        results = verified[:top_k]
    finally:
        if owned:
            client.close()
    return {
        "schema_version": SCHEMA, "query": query, "mode": mode, "cutoff": records._cutoff(cutoff).isoformat(),
        "top_k": top_k, "results": results, "excluded_sources": excluded,
        "excluded": {"counts": dict(counts)},
        "execution": {"candidates": len(candidates), "verified": len(verified), "returned": len(results),
                      "insufficient": len(results) < top_k, "candidate_budget_per_knowledge_base": CANDIDATES,
                      "recall": recall, "rerank": rerank},
        "coverage": {},  # 索引覆盖不在这里算；各状态的条数见 GET /api/fabric/index/counts
        "limitations": [
            "命中只是指针：每条都按本次模式和截止时间回 Fabric 校验了来源许可、可见版本和固定正文",
            "候选有上限；过滤后不足 top_k 是这次召回的结果，不代表库里没有更多相关材料",
            "没被索引或没搜到不等于不存在",
        ],
    }
