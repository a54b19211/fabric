"""
多源标识符解析与跨源冲突消解（论文辅创新 T3 · 第二阶段）。

内容二(2)：「统一的标识符解析层」——接入权威数据源（GSRS/UNII、PubChem、
UniProt、OLS4、ICD-11 等），对齐输出五元组，并对多源结果做冲突消解。
K3：比较「多数投票 / 证据加权 / 来源可信度分层 / 结构判据优先」四类裁定策略，
并用 Platt scaling 校准融合置信度。

与 authority_client.py 的分工：
    - authority_client.py   单源解析（PubChem 药名 + 五族格式校验层）
    - 本模块                 多源聚合 + 冲突消解 + 置信度校准 + 权威源适配器

设计原则：
    - 每个权威源适配器失败时返回 degraded=True 的「降级」五元组，绝不抛异常；
    - 未部署的本地源（GSRS/ICD-11）默认跳过并标记降级，不参与裁定；
    - 消解策略均为纯函数，便于单测与论文「可解释聚合」论述。
"""

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from typing import Callable, Dict, List, Optional

from core_engine.authority_client import resolve_identifier

# ───────────────────── 权威源分层 ─────────────────────
# 来源可信度分层策略用：数值越小越权威（监管/官方 > 人工策展 > 聚合）。
# 依据开题报告内容二(4) 来源可信度维度与 K3。
SOURCE_TIERS: Dict[str, int] = {
    "gsrs": 0,      # GSRS/UNII —— FDA/官方，ISO 11238 官方可执行实现
    "icd11": 0,     # WHO ICD-11 —— 官方
    "pubchem": 1,   # PubChem —— 人工策展/聚合
    "uniprot": 1,   # UniProt —— 人工策展
    "ols4": 1,      # OLS4 —— 本体策展
    "format": 1,    # 格式校验层（结构判据）
    "local": 2,     # 本地兜底白名单
    "none": 3,      # 未命中
}

# 各权威源默认权重（证据加权策略用）；未列出的源权重为 1.0
SOURCE_WEIGHTS: Dict[str, float] = {
    "gsrs": 1.2,
    "icd11": 1.2,
    "pubchem": 1.0,
    "uniprot": 0.9,
    "ols4": 0.8,
    "format": 0.7,
    "local": 0.5,
}

# ───────────────────── 权威源端点 ─────────────────────
UNIPROT_BASE = "https://rest.uniprot.org"
OLS4_BASE = "https://www.ebi.ac.uk/ols4/api"
GSRS_BASE = ""   # 本地部署（docker-compose.authority.yml），留空表示未部署
ICD11_BASE = ""  # 本地部署，留空表示未部署

DEFAULT_TIMEOUT = 5.0

_STRUCTURED_PREFIXES = (
    "NCT:", "PMID:", "DOI:", "NDA:", "ANDA:", "BLA:",
    "UNII:", "PUBCHEM:", "UNIPROT:", "OLS4:",
)


# ───────────────────── 五元组工具 ─────────────────────
def _miss(raw_value: str) -> dict:
    return {
        "found": False, "std_id": None, "source_id": "none",
        "raw_value": raw_value, "resolved_value": None,
        "confidence": 0.0, "candidates": [], "degraded": False,
    }


def _degraded(raw_value: str, source_id: str) -> dict:
    """接口异常/限流/未部署 → 降级五元组（degraded=True，未命中≠不存在）。"""
    return {
        "found": False, "std_id": None, "source_id": source_id,
        "raw_value": raw_value, "resolved_value": None,
        "confidence": 0.0, "candidates": [], "degraded": True,
    }


def _http_get_json(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "ZK-DataFabric/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ───────────────────── 权威源适配器 ─────────────────────
def _query_uniprot(raw_value: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """UniProt 名称 → UniProtKB 主登录号（公开 REST，size=1 取最佳命中）。"""
    q = urllib.parse.quote(raw_value.strip())
    url = f"{UNIPROT_BASE}/uniprotkb/search?query={q}&format=json&size=1"
    try:
        data = _http_get_json(url, timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return _degraded(raw_value, "uniprot")
    results = data.get("results") or []
    if not results:
        return _miss(raw_value)
    entry = results[0]
    acc = entry.get("primaryAccession")
    desc = (entry.get("proteinDescription") or {}).get("recommendedName") or {}
    name = (desc.get("fullName") or {}).get("value")
    return {
        "found": True, "std_id": f"UNIPROT:{acc}", "source_id": "uniprot",
        "raw_value": raw_value, "resolved_value": name or acc,
        "confidence": 0.9, "candidates": [], "degraded": False,
    }


def _query_ols4(raw_value: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """OLS4 术语 → 本体类（obo_id / iri）。"""
    q = urllib.parse.quote(raw_value.strip())
    url = f"{OLS4_BASE}/search?q={q}&exact=true&size=1"
    try:
        data = _http_get_json(url, timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return _degraded(raw_value, "ols4")
    docs = ((data.get("response") or {}).get("docs")) or []
    if not docs:
        return _miss(raw_value)
    doc = docs[0]
    obo_id = doc.get("obo_id") or doc.get("shortForm")
    label = doc.get("label")
    return {
        "found": True, "std_id": f"OLS4:{obo_id}", "source_id": "ols4",
        "raw_value": raw_value, "resolved_value": label or obo_id,
        "confidence": 0.8, "candidates": [], "degraded": False,
    }


def _query_gsrs(raw_value: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """GSRS/UNII 本地部署（ISO 11238 官方可执行实现）。未部署即降级。"""
    if not GSRS_BASE:
        return _degraded(raw_value, "gsrs")
    q = urllib.parse.quote(raw_value.strip())
    url = f"{GSRS_BASE}/api/v1/substances/search?q={q}&size=1"
    try:
        data = _http_get_json(url, timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return _degraded(raw_value, "gsrs")
    content = data.get("content") or []
    if not content:
        return _miss(raw_value)
    sub = content[0]
    unii = sub.get("approvalID") or sub.get("_approvalID")
    name = sub.get("_name")
    return {
        "found": True, "std_id": f"UNII:{unii}", "source_id": "gsrs",
        "raw_value": raw_value, "resolved_value": name or unii,
        "confidence": 0.95, "candidates": [], "degraded": False,
    }


def _query_icd11(raw_value: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """ICD-11 官方容器本地部署。未部署即降级。"""
    if not ICD11_BASE:
        return _degraded(raw_value, "icd11")
    q = urllib.parse.quote(raw_value.strip())
    url = f"{ICD11_BASE}/icd/release/11/2023-01/mms/search?q={q}"
    try:
        data = _http_get_json(url, timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return _degraded(raw_value, "icd11")
    entities = data.get("destinationEntities") or []
    if not entities:
        return _miss(raw_value)
    ent = entities[0]
    code = ent.get("theCode")
    title = ent.get("title")
    return {
        "found": True, "std_id": f"ICD11:{code}", "source_id": "icd11",
        "raw_value": raw_value, "resolved_value": title or code,
        "confidence": 0.95, "candidates": [], "degraded": False,
    }


# ───────────────────── 冲突消解四策略 ─────────────────────
def _found(resolutions: List[dict]) -> List[dict]:
    return [r for r in resolutions if r.get("found") and r.get("std_id")]


def _no_consensus(resolutions: List[dict]) -> dict:
    """所有源均未命中 → 未命中五元组；若全因降级则标记 degraded=True。"""
    raw = resolutions[0].get("raw_value", "") if resolutions else ""
    base = _miss(raw)
    if resolutions and all(r.get("degraded") for r in resolutions):
        base["degraded"] = True
        base["source_id"] = "+".join(sorted({r["source_id"] for r in resolutions}))
    return base


def _vote_breakdown(counter: Counter) -> List[dict]:
    return [{"std_id": k, "votes": v} for k, v in counter.most_common()]


def majority_vote(resolutions: List[dict]) -> dict:
    """多数投票：得票最多的 std_id 胜出；置信度 = 得票占比 × 该组平均置信度。"""
    found = _found(resolutions)
    if not found:
        return _no_consensus(resolutions)
    counts = Counter(r["std_id"] for r in found)
    top_id, top_count = counts.most_common(1)[0]
    peers = [r for r in found if r["std_id"] == top_id]
    best = max(peers, key=lambda r: r.get("confidence") or 0)
    vote_share = top_count / len(found)
    avg_conf = sum(r.get("confidence") or 0 for r in peers) / len(peers)
    return {
        "found": True, "std_id": top_id,
        "source_id": "+".join(sorted({r["source_id"] for r in peers})),
        "raw_value": best.get("raw_value", ""),
        "resolved_value": best.get("resolved_value") or top_id,
        "confidence": round(vote_share * avg_conf, 4),
        "candidates": _vote_breakdown(counts),
        "degraded": False,
    }


def evidence_weighted(resolutions: List[dict]) -> dict:
    """证据加权：按 置信度×来源权重 累加每个 std_id 的证据，取权重和最大者。"""
    found = _found(resolutions)
    if not found:
        return _no_consensus(resolutions)
    weighted: Dict[str, float] = {}
    peers_by_id: Dict[str, List[dict]] = {}
    for r in found:
        sid = r["std_id"]
        w = (r.get("confidence") or 0) * SOURCE_WEIGHTS.get(r.get("source_id"), 1.0)
        weighted[sid] = weighted.get(sid, 0.0) + w
        peers_by_id.setdefault(sid, []).append(r)
    top_id = max(weighted, key=weighted.get)
    total = sum(weighted.values()) or 1.0
    peers = peers_by_id[top_id]
    best = max(peers, key=lambda r: r.get("confidence") or 0)
    return {
        "found": True, "std_id": top_id,
        "source_id": "+".join(sorted({r["source_id"] for r in peers})),
        "raw_value": best.get("raw_value", ""),
        "resolved_value": best.get("resolved_value") or top_id,
        "confidence": round(weighted[top_id] / total, 4),
        "candidates": sorted(
            [{"std_id": k, "evidence": round(v, 4)} for k, v in weighted.items()],
            key=lambda x: -x["evidence"],
        ),
        "degraded": False,
    }


def source_hierarchy(resolutions: List[dict]) -> dict:
    """来源可信度分层：最高层权威源优先，同层内置信度高者胜出。"""
    found = _found(resolutions)
    if not found:
        return _no_consensus(resolutions)
    best = min(
        found,
        key=lambda r: (
            SOURCE_TIERS.get(r.get("source_id"), 99),
            -(r.get("confidence") or 0),
        ),
    )
    top_tier = SOURCE_TIERS.get(best["source_id"], 99)
    peers = [
        r for r in found
        if r["std_id"] == best["std_id"]
        and SOURCE_TIERS.get(r.get("source_id"), 99) == top_tier
    ]
    return {
        "found": True, "std_id": best["std_id"],
        "source_id": "+".join(sorted({r["source_id"] for r in peers})),
        "raw_value": best.get("raw_value", ""),
        "resolved_value": best.get("resolved_value") or best["std_id"],
        "confidence": round(best.get("confidence") or 0, 4),
        "candidates": [{"std_id": r["std_id"], "tier": SOURCE_TIERS.get(r.get("source_id"), 99)} for r in found],
        "degraded": False,
    }


def _structural_score(std_id: Optional[str]) -> int:
    """结构判据：自带校验位的结构最强，其次为结构化标识符，本地兜底最弱。"""
    s = str(std_id or "").upper()
    if s.startswith("CAS:"):
        return 3
    if any(s.startswith(p) for p in _STRUCTURED_PREFIXES):
        return 2
    if s.startswith("LOCAL:"):
        return 1
    return 0


def structural_criterion(resolutions: List[dict]) -> dict:
    """结构判据优先：结构最强（如 CAS 校验位）的 std_id 优先，同结构按置信度。"""
    found = _found(resolutions)
    if not found:
        return _no_consensus(resolutions)
    best = max(
        found,
        key=lambda r: (_structural_score(r.get("std_id")), r.get("confidence") or 0),
    )
    peers = [r for r in found if r["std_id"] == best["std_id"]]
    return {
        "found": True, "std_id": best["std_id"],
        "source_id": "+".join(sorted({r["source_id"] for r in peers})),
        "raw_value": best.get("raw_value", ""),
        "resolved_value": best.get("resolved_value") or best["std_id"],
        "confidence": round(best.get("confidence") or 0, 4),
        "candidates": [{"std_id": r["std_id"], "structural": _structural_score(r.get("std_id"))} for r in found],
        "degraded": False,
    }


STRATEGIES: Dict[str, Callable[[List[dict]], dict]] = {
    "majority_vote": majority_vote,
    "evidence_weighted": evidence_weighted,
    "source_hierarchy": source_hierarchy,
    "structural_criterion": structural_criterion,
}


def resolve_conflict(resolutions: List[dict], strategy: str = "source_hierarchy") -> dict:
    """按指定策略对多源五元组列表做冲突消解；未知策略抛 ValueError。"""
    fn = STRATEGIES.get(strategy)
    if fn is None:
        raise ValueError(f"未知消解策略: {strategy!r}")
    return fn(resolutions)


# ───────────────────── 置信度校准（Platt scaling）─────────────────────
def platt_scale(scores: List[float], labels: List[int], max_iter: int = 5000,
                lr: float = 0.1, tol: float = 1e-6) -> tuple:
    """Platt scaling：解 2 参数逻辑回归，把原始分数校准为概率。

    纯 Python 梯度下降（无 scipy 依赖）。返回 (A, B)，校准概率
    P(y=1 | s) = 1 / (1 + exp(A*s + B))。
    """
    if len(scores) != len(labels) or not scores:
        raise ValueError("scores 与 labels 长度必须一致且非空")
    a, b = 0.0, 0.0
    n = len(scores)
    for _ in range(max_iter):
        ga = gb = 0.0
        for s, y in zip(scores, labels):
            p = 1.0 / (1.0 + math.exp(-(a * s + b)))
            ga += (p - y) * s
            gb += (p - y)
        ga /= n
        gb /= n
        a -= lr * ga
        b -= lr * gb
        if abs(ga) < tol and abs(gb) < tol:
            break
    return a, b


def platt_predict(scores: List[float], a: float, b: float) -> List[float]:
    """用已拟合的 (A, B) 输出校准概率。"""
    return [1.0 / (1.0 + math.exp(-(a * s + b))) for s in scores]


# ───────────────────── 多源解析入口 ─────────────────────
# id_type → 参与裁决的权威源列表（drug_name 才有跨源冲突；其余族走格式校验层）
_SOURCE_QUERIES: Dict[str, Callable[..., dict]] = {
    "pubchem": lambda raw, t: resolve_identifier(raw, timeout=t, id_type="drug_name"),
    "uniprot": _query_uniprot,
    "ols4": _query_ols4,
    "gsrs": _query_gsrs,
    "icd11": _query_icd11,
}

_MULTI_SOURCES = {
    "drug_name": ["pubchem", "uniprot", "ols4", "gsrs", "icd11"],
}


def resolve_identifier_multi(raw_value: str, id_type: str = "drug_name",
                            strategy: str = "source_hierarchy",
                            timeout: float = DEFAULT_TIMEOUT) -> dict:
    """多源解析 + 冲突消解的统一入口。

    drug_name 走多源裁决（PubChem/UniProt/OLS4/GSRS/ICD-11）；
    其余标识符族回退到 authority_client 的格式校验层（单源，无跨源冲突）。
    """
    sources = _MULTI_SOURCES.get(id_type)
    if not sources:
        return resolve_identifier(raw_value, timeout=timeout, id_type=id_type)
    resolutions = [_SOURCE_QUERIES[src](raw_value, timeout) for src in sources]
    return resolve_conflict(resolutions, strategy)
