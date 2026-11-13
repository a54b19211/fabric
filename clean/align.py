"""编号对齐和多来源取舍（徐子雯 T3，开题内容二“统一的标识符解析层”）。

两条路，结果分开统计（口径不同，混在一起会把药名的真实命中率稀释掉）：
1. 编号字段（extra 里的 nct_id、pmid、doi、approval_number、cas_number）只做本地格式校验：CAS 核校验位、NCT 8 位、
   NDA/ANDA/BLA 6 位、PMID 1–8 位、DOI 10.xxxx/… 结构。不联网，结果稳定；合格只说明“格式对”，不说明权威库里有。
2. 药名：先切成候选名（clean/drug_names.py），一个一个查，命中就停。每个候选名按顺序问几个权威库（authorities()：
   配了 DATAFABRIC_GSRS_URL 先问 GSRS，再问 PubChem、ChEMBL、OLS4 里的 ChEBI、UniProt、本地白名单）；GSRS 或 PubChem
   查到了就不再问后面的库。问到的结果按“来源分层”选出一个（source_hierarchy）。
   同一个进程里，同一个库、同一个候选名只问一次（_ANSWERS）；没查成的不记，下次再问。
   某个库查不成（超时、429、5xx）记“降级”；所有联网的库都降级时换候选名也不会好，就停；一份资料里第一个药名就降级了，
   其余药名不再问，直接记降级。占位名（安慰剂、生理盐水）不查，记 skipped，不进命中率的分母。
每个候选名问到的各库结果都存下来（行里的 sources）。徐子雯论文里另外三种取舍策略、Platt 校准和命中率统计在
ops/align_offline.py，拿存下的结果离线重算，不用再联网。

一个结果（她叫“五元组”）是 dict：found、std_id（PUBCHEM:2244 这样带前缀）、authority（哪个库，她原来叫 source_id）、
raw_value、resolved_value（库里的规范名）、confidence、candidates、degraded。
出网只走 fetch/transport.py · get（按主机限速、超时、429 重试）；DATAFABRIC_HISTORY_FETCH_ENABLED=false 的实例不出网，
只查本地白名单，查不到的记降级。
"""

from __future__ import annotations

import os
import re
from urllib.parse import quote

from clean import drug_names

# 规则、来源清单或取舍一改就升，旧结果按版本留在表里。v2：GSRS/PubChem 命中就不问其余库
ALIGNER_VERSION = "datafabric.align.v2"
STRATEGY = "source_hierarchy"
TIMEOUT = 5.0
PUBCHEM = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
CHEMBL = "https://www.ebi.ac.uk/chembl/api/data/molecule.json"
OLS4 = "https://www.ebi.ac.uk/ols4/api/search"
UNIPROT = "https://rest.uniprot.org/uniprotkb/search"

# 编号字段 → 格式族（approval_number 是 FDA 的 NDA/ANDA/BLA 申请号）
IDENTIFIER_FIELDS = {"nct_id": "nct_id", "pmid": "pmid", "doi": "doi", "approval_number": "application_number",
                     "cas_number": "cas_number"}
# 本地白名单（原写法小写 → (规范名, 编号)），哪个库都查不到时兜底
LOCAL_NAMES = {
    "阿司匹林": ("阿司匹林", "LOCAL:aspirin"), "aspirin": ("阿司匹林", "LOCAL:aspirin"),
    "乙酰水杨酸": ("阿司匹林", "LOCAL:aspirin"), "扑热息痛": ("对乙酰氨基酚", "LOCAL:paracetamol"),
    "paracetamol": ("对乙酰氨基酚", "LOCAL:paracetamol"), "acetaminophen": ("对乙酰氨基酚", "LOCAL:paracetamol"),
    "头孢曲松": ("头孢曲松", "LOCAL:ceftriaxone"), "ceftriaxone": ("头孢曲松", "LOCAL:ceftriaxone"),
}
# 来源分层：数越小越权威（官方 > 人工策展 > 本地兜底）
TIERS = {"gsrs": 0, "pubchem": 1, "chembl": 1, "uniprot": 1, "ols4": 1, "format": 1, "local": 2, "none": 3}
DECISIVE = ("gsrs", "pubchem")  # 这两个库查到了就不再问后面的库
MAX_ANSWERS = 50000  # 进程内记住的答案最多这么多条，满了清空重来


def result(raw, authority, std_id=None, resolved=None, confidence=0.0, candidates=(), degraded=False):
    return {"found": std_id is not None, "std_id": std_id, "authority": authority, "raw_value": raw,
            "resolved_value": resolved, "confidence": confidence, "candidates": list(candidates), "degraded": degraded}


# ───────────── 1. 编号格式校验（本地） ─────────────

def _cas(raw):
    m = re.fullmatch(r"(\d{2,7})-(\d{2})-(\d)", raw)
    if not m:
        return None
    digits = m.group(1) + m.group(2)
    total = sum(int(ch) * (i + 1) for i, ch in enumerate(reversed(digits)))
    return (f"CAS:{raw}", raw) if total % 10 == int(m.group(3)) else None


def _application(raw):
    m = re.fullmatch(r"(NDA|ANDA|BLA)[\s\-]?(\d{6})", raw, re.IGNORECASE)
    return (f"{m.group(1).upper()}:{m.group(2)}", m.group(2)) if m else None


def _nct(raw):
    m = re.fullmatch(r"NCT(\d{8})", raw, re.IGNORECASE)
    return (f"NCT:{m.group(1)}", m.group(1)) if m else None


def _pmid(raw):
    return (f"PMID:{raw}", raw) if re.fullmatch(r"\d{1,8}", raw) else None


def _doi(raw):
    return (f"DOI:{raw}", raw) if re.fullmatch(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", raw) else None


FORMATS = {"application_number": _application, "nct_id": _nct, "pmid": _pmid, "doi": _doi, "cas_number": _cas}


def check_format(kind: str, raw: str) -> dict:
    """格式合格：found=True、authority="format"、confidence 0.9（只代表格式合格）；不合格：found=False。"""
    out = FORMATS[kind]((raw or "").strip())
    return result(raw, "format", out[0], out[1], 0.9) if out else result(raw, "none")


# ───────────── 2. 权威库（联网） ─────────────

def _get(url, params=None):
    """(状态码, JSON)；没发成（超时、连不上、响应太大、不是 JSON）返回 (None, None)。"""
    import httpx

    from fetch import transport

    try:
        response = transport.get(url, params=params, headers={"User-Agent": "ZK-DataFabric/0.1"}, timeout=TIMEOUT,
                                 tries=2, cap=2 * 1024 * 1024)
        return response.status_code, (response.json() if response.status_code == 200 else None)
    except (httpx.HTTPError, ValueError):
        return None, None


def pubchem(name):
    """PubChem（小分子首选）：按名字查 CID，再取 IUPAC 名；有多个 CID 时置信度 0.6。404 是“库里确认没有”。"""
    status, data = _get(f"{PUBCHEM}/compound/name/{quote(name.strip(), safe='')}/cids/JSON")
    if status == 404:
        return result(name, "pubchem")
    if status != 200:
        return result(name, "pubchem", degraded=True)
    cids = (data.get("IdentifierList") or {}).get("CID") or []
    if not cids:
        return result(name, "pubchem")
    _, props = _get(f"{PUBCHEM}/compound/cid/{cids[0]}/property/IUPACName/JSON")
    rows = ((props or {}).get("PropertyTable") or {}).get("Properties") or [{}]
    multi = len(cids) > 1
    return result(name, "pubchem", f"PUBCHEM:{cids[0]}", rows[0].get("IUPACName") or name, 0.6 if multi else 0.9,
                  [f"PUBCHEM:{c}" for c in cids] if multi else [])


def chembl(name):
    """ChEMBL（补生物制品：单抗、融合蛋白在 PubChem 常查不到）：只认名字完全相同（不用全文检索，免得假命中）。"""
    status, data = _get(CHEMBL, {"pref_name__iexact": name.strip(), "limit": 5})
    if status in (400, 404):
        return result(name, "chembl")
    if status != 200:
        return result(name, "chembl", degraded=True)
    for molecule in data.get("molecules") or []:
        if molecule.get("molecule_chembl_id"):
            preferred = str(molecule.get("pref_name") or "").strip() or name
            return result(name, "chembl", "CHEMBL:" + molecule["molecule_chembl_id"], preferred, 0.9)
    return result(name, "chembl")


def ols4(name):
    """OLS4 本体检索，只查 ChEBI（化学实体，含药物），标签或同义词完全相同（exact=true）。不限本体时药名会命中
    疾病、等位基因、护理概念（2026-11-14 样本：'Cold' 命中 SNOMED 的感冒，'Supportive care' 命中 NCIT 的支持治疗）。"""
    status, data = _get(OLS4, {"q": name.strip(), "exact": "true", "ontology": "chebi", "rows": 1})
    if status != 200:
        return result(name, "ols4", degraded=True)
    docs = (data.get("response") or {}).get("docs") or []
    if not docs:
        return result(name, "ols4")
    obo_id = docs[0].get("obo_id") or docs[0].get("short_form") or docs[0].get("shortForm")
    if not obo_id:
        return result(name, "ols4")
    return result(name, "ols4", f"OLS4:{obo_id}", docs[0].get("label") or obo_id, 0.8)


def uniprot(name):
    """UniProt（蛋白类药物）：只收人工审核条目里推荐名或别名和药名完全相同的（全文检索取第一条会命中无关蛋白）。"""
    status, data = _get(UNIPROT, {"query": f'protein_name:"{name.strip()}" AND reviewed:true', "format": "json",
                                  "size": 5})
    if status != 200:
        return result(name, "uniprot", degraded=True)
    wanted = name.strip().lower()
    for entry in data.get("results") or []:
        desc = entry.get("proteinDescription") or {}
        names = [(desc.get("recommendedName") or {}).get("fullName", {}).get("value")]
        names += [(alt.get("fullName") or {}).get("value") for alt in desc.get("alternativeNames") or []]
        if wanted in {str(n).lower() for n in names if n}:
            return result(name, "uniprot", "UNIPROT:" + entry.get("primaryAccession", ""), names[0] or name, 0.9)
    return result(name, "uniprot")


def gsrs(name):
    """GSRS/UNII（FDA 物质登记，最权威的一层）：本地部署后设 DATAFABRIC_GSRS_URL 才会问它。"""
    status, data = _get(os.environ["DATAFABRIC_GSRS_URL"].rstrip("/") + "/api/v1/substances/search",
                        {"q": name.strip(), "size": 1})
    if status != 200:
        return result(name, "gsrs", degraded=True)
    content = data.get("content") or []
    if not content:
        return result(name, "gsrs")
    unii = content[0].get("approvalID") or content[0].get("_approvalID")
    return result(name, "gsrs", f"UNII:{unii}", content[0].get("_name") or unii, 0.95) if unii else result(name, "gsrs")


def local(name):
    hit = LOCAL_NAMES.get(name.strip().lower())
    return result(name, "local", hit[1], hit[0], 0.7) if hit else result(name, "local")


def authorities():
    """这次要问的库，按问的先后（也是同分时的先后）。不出网的实例只查本地白名单。"""
    from fetch.sources import public_fetch_enabled

    if not public_fetch_enabled():
        return [local]
    return ([gsrs] if os.environ.get("DATAFABRIC_GSRS_URL") else []) + [pubchem, chembl, ols4, uniprot, local]


_ANSWERS: dict = {}  # (查询函数, 候选名) → 这个库的答案；进程内复用


def _ask(lookup, value):
    """问一个库一个候选名：问过（而且查成了）就用记下的答案，降级的不记。"""
    key = (lookup, value)
    if key not in _ANSWERS:
        answer = lookup(value)
        if answer.get("degraded"):
            return answer
        if len(_ANSWERS) >= MAX_ANSWERS:
            _ANSWERS.clear()
        _ANSWERS[key] = answer
    return _ANSWERS[key]


# ───────────── 3. 多来源取舍：来源分层 ─────────────

def _found(results):
    return [r for r in results if r.get("found") and r.get("std_id")]


def _none_found(results):
    """都没查到：未命中。联网的库一个都没问、或者全都没查成时记降级（没查成不等于没有；本地白名单不算联网的库）。"""
    out = result(results[0]["raw_value"] if results else "", "none")
    online = [r for r in results if r["authority"] != "local"]
    if not online or all(r.get("degraded") for r in online):
        out.update(degraded=True, authority="+".join(sorted({r["authority"] for r in online})) or "none")
    return out


def _pick(best, peers, confidence, candidates):
    return {**result(best.get("raw_value", ""), "+".join(sorted({r["authority"] for r in peers})), best["std_id"],
                     best.get("resolved_value") or best["std_id"], round(confidence, 4)), "candidates": candidates}


def source_hierarchy(results):
    """来源分层：最权威一层的库优先，同层看置信度，再同就按问的先后。"""
    found = _found(results)
    if not found:
        return _none_found(results)
    best = min(found, key=lambda r: (TIERS.get(r["authority"], 99), -(r.get("confidence") or 0)))
    tier = TIERS.get(best["authority"], 99)
    peers = [r for r in found if r["std_id"] == best["std_id"] and TIERS.get(r["authority"], 99) == tier]
    return _pick(best, peers, best.get("confidence") or 0,
                 [{"std_id": r["std_id"], "tier": TIERS.get(r["authority"], 99)} for r in found])


# ───────────── 4. 一份资料的对齐 ─────────────

def _unasked(name, *, skipped, degraded=False):
    """没去问的药名：占位名（skipped），或同一份资料里前一个药名已经查不成（degraded）。"""
    return {**result(name, "none", degraded=degraded), "candidate": "", "rule": "", "candidates_tried": 0,
            "skipped": skipped, "strategy": STRATEGY, "sources": []}


def align_name(name, ask):
    """一个药名：候选名逐个问 ask 里的库、按来源分层取舍，命中就停。返回一行对齐结果（带每个候选名各库的原始结果）。"""
    tried = []
    chosen = result(name, "none")
    for value, rule in drug_names.candidates(name):
        answers = []
        for lookup in ask:
            answers.append(_ask(lookup, value))
            if answers[-1]["found"] and answers[-1]["authority"] in DECISIVE:
                break
        tried.append({"candidate": value, "rule": rule, "results": answers})
        chosen = source_hierarchy(answers)
        if chosen["found"]:
            return {**chosen, "raw_value": name, "candidate": value, "rule": rule, "candidates_tried": len(tried),
                    "skipped": False, "strategy": STRATEGY, "sources": tried}
        if chosen["degraded"]:
            break  # 联网的库都没查成：换名字也没用，只会放大等待
    if not tried:  # 没有能查的候选名（空的、只剩残片）：和占位名一样不进分母
        return _unasked(name, skipped=True)
    return {**chosen, "raw_value": name, "candidate": "", "rule": "", "candidates_tried": len(tried), "skipped": False,
            "strategy": STRATEGY, "sources": tried}


def align(doc, ask=None):
    """(药名 → 对齐行, 编号字段 → 格式校验行)。ask 不给就是 authorities()；测试时传自己的查询函数。"""
    extra = doc.get("extra") or {}
    identifiers = {}
    for field, kind in IDENTIFIER_FIELDS.items():
        raw = extra.get(field)
        if isinstance(raw, str) and raw.strip():
            identifiers[field] = {**check_format(kind, raw.strip()), "raw_value": raw, "id_type": kind}
    ask = authorities() if ask is None else ask
    names, down = {}, False
    for name in drug_names.names_of(doc)[:drug_names.MAX_NAMES]:
        if drug_names.is_placeholder(name):
            names[name] = _unasked(name, skipped=True)
        elif down:  # 前一个药名所有联网的库都没查成：这次断网了，其余药名不再一个个等超时
            names[name] = _unasked(name, skipped=False, degraded=True)
        else:
            names[name] = align_name(name, ask)
            down = names[name]["degraded"]
    return names, identifiers
