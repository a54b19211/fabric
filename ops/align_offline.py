"""药名对齐的离线评估（论文评估用）：另外三种多来源取舍策略、Platt 校准、命中率。主线不用（主线只用
clean/align.py 的来源分层），也不联网：拿 derived_authority_resolutions 里存下的 sources（每个候选名问到的各库结果）重算。

    from ops.align_offline import STRATEGIES, summarize
    for tried in row["sources"]:                          # 一行药名对齐结果里，每个候选名一项
        chosen = STRATEGIES["majority_vote"](tried["results"])

注意：主线 GSRS/PubChem 查到了就不再问后面的库（clean/align.py · DECISIVE），所以存下的结果里不一定每个库都有。
"""

from __future__ import annotations

import math
from collections import Counter

from clean.align import hits, none_found, pick, source_hierarchy

# 证据加权用的来源权重
WEIGHTS = {"gsrs": 1.2, "pubchem": 1.0, "chembl": 1.0, "uniprot": 0.9, "ols4": 0.8, "format": 0.7, "local": 0.5}
_STRUCTURED = ("NCT:", "PMID:", "DOI:", "NDA:", "ANDA:", "BLA:", "NMPA:", "UNII:", "PUBCHEM:", "CHEMBL:", "UNIPROT:",
               "OLS4:")


def majority_vote(results):
    """多数投票：得票最多的编号胜出；置信度 = 得票占比 × 这组的平均置信度。"""
    found = hits(results)
    if not found:
        return none_found(results)
    counts = Counter(r["std_id"] for r in found)
    top, votes = counts.most_common(1)[0]
    peers = [r for r in found if r["std_id"] == top]
    best = max(peers, key=lambda r: r.get("confidence") or 0)
    average = sum(r.get("confidence") or 0 for r in peers) / len(peers)
    return pick(best, peers, votes / len(found) * average,
                 [{"std_id": k, "votes": v} for k, v in counts.most_common()])


def evidence_weighted(results):
    """证据加权：每个编号累加 置信度 × 来源权重，取最大；置信度 = 它占总证据的比例。"""
    found = hits(results)
    if not found:
        return none_found(results)
    weight = {}
    for r in found:
        weight[r["std_id"]] = weight.get(r["std_id"], 0.0) + (r.get("confidence") or 0) * WEIGHTS.get(r["authority"], 1.0)
    top = max(weight, key=weight.get)
    peers = [r for r in found if r["std_id"] == top]
    best = max(peers, key=lambda r: r.get("confidence") or 0)
    ranked = sorted(({"std_id": k, "evidence": round(v, 4)} for k, v in weight.items()), key=lambda x: -x["evidence"])
    return pick(best, peers, weight[top] / (sum(weight.values()) or 1.0), ranked)


def _structural(std_id):
    s = str(std_id or "").upper()
    return 3 if s.startswith("CAS:") else 2 if s.startswith(_STRUCTURED) else 1 if s.startswith("LOCAL:") else 0


def structural_criterion(results):
    """结构判据优先：自带校验位的（CAS）最强，其次结构化编号，本地兜底最弱；同结构看置信度。"""
    found = hits(results)
    if not found:
        return none_found(results)
    best = max(found, key=lambda r: (_structural(r["std_id"]), r.get("confidence") or 0))
    peers = [r for r in found if r["std_id"] == best["std_id"]]
    return pick(best, peers, best.get("confidence") or 0,
                 [{"std_id": r["std_id"], "structural": _structural(r["std_id"])} for r in found])


STRATEGIES = {"majority_vote": majority_vote, "evidence_weighted": evidence_weighted,
              "source_hierarchy": source_hierarchy, "structural_criterion": structural_criterion}


def platt_scale(scores, labels, max_iter=5000, lr=0.1, tol=1e-6):
    """Platt 校准：用梯度下降拟合 P(对 | 分数) = 1 / (1 + exp(-(a·分数 + b)))，返回 (a, b)。"""
    if len(scores) != len(labels) or not scores:
        raise ValueError("scores 和 labels 要一样长且不为空")
    a = b = 0.0
    for _ in range(max_iter):
        ga = gb = 0.0
        for s, y in zip(scores, labels):
            p = 1.0 / (1.0 + math.exp(-(a * s + b)))
            ga += (p - y) * s
            gb += p - y
        ga, gb = ga / len(scores), gb / len(scores)
        a, b = a - lr * ga, b - lr * gb
        if abs(ga) < tol and abs(gb) < tol:
            break
    return a, b


def platt_predict(scores, a, b):
    return [1.0 / (1.0 + math.exp(-(a * s + b))) for s in scores]


def summarize(rows: dict) -> dict:
    """命中率：matched = found 且 std_id 非空；skipped（占位名）不进分母。rows：药名 → 对齐行。"""
    rows = [r for r in (rows or {}).values() if isinstance(r, dict)]
    skipped = sum(1 for r in rows if r.get("skipped"))
    matched = sum(1 for r in rows if not r.get("skipped") and r.get("found") and str(r.get("std_id") or "").strip())
    eligible = len(rows) - skipped
    return {"total": len(rows), "eligible": eligible, "skipped": skipped, "matched": matched,
            "match_rate": round(matched / eligible, 4) if eligible else 0.0}
