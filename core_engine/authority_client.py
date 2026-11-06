"""
标准标识符对齐客户端（论文辅创新 T3）。

把药物 / 化合物名称（含同义词）对齐成标准标识符，解决"同物异名"导致的
跨源无法对齐问题。采用**双权威库分族**策略：

  - PubChem（化合物库）：小分子药物首选，返回 CID + 规范 IUPAC 名
  - ChEMBL（生物活性分子库）：生物制品（单抗 / 融合蛋白 / 细胞因子）的
    覆盖来源。PubChem 化合物库对蛋白质类药物往往只有 substance 记录、
    没有可用的 compound 记录，按名称查 CID 会 404（实测 Nivolumab /
    Ipilimumab / Filgrastim / Pegfilgrastim / Etanercept 五个生物制品
    在 PubChem 全部未命中，而 ChEMBL 精确名匹配 5/5 命中）。

分族顺序与停表条件：**PubChem 未命中且未降级**才查 ChEMBL。若 PubChem 因
限流 / 网络故障降级，继续换库没有意义（可能两个库都连不上），直接返回降级，
避免放大等待时间与把故障误记成"药名不规范"。

设计原则（与前期探索性原型对齐）：
- 网络 / 接口异常或限流时**降级**到本地白名单，再不行返回未命中，
  绝不影响主流程、绝不丢弃原始值；
- 多候选时返回候选列表交由上层裁决，不直接臆断；
- 仅做只读查询，不写库（五元组落库由 C 组的"支撑 T3 五元组"负责）。

返回结构（五元组对齐结果 + 辅助字段）：
    {
      "found":          bool,    # 是否解析到标准标识符
      "std_id":         str|None,# 标准标识符（PUBCHEM:<cid> / CHEMBL:<id> / LOCAL:<id>）
      "source_id":      str,     # 来源（"pubchem" / "chembl" / "local" / "none"）
      "raw_value":      str,     # 原始输入
      "resolved_value": str|None,# 解析后的标准名（规范 IUPAC 名或通用名）
      "confidence":     float,   # 对齐置信度 0~1（区别于 source_mapper 的来源可信度）
      "candidates":     list,    # 多候选时的候选列表（单候选时为空）
      "degraded":       bool,    # 是否走了降级（接口异常 / 限流）
    }
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional, Tuple

PUBCHEM_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
# ChEMBL 分子检索端点（EBI，无需 key；单 IP 建议 ≤1 req/s）
CHEMBL_MOLECULE_URL = "https://www.ebi.ac.uk/chembl/api/data/molecule.json"
DEFAULT_TIMEOUT = 5.0
# 限流 / 服务端故障时的单次退避秒数（仅重试一次，避免拖慢主流程）
_RETRY_BACKOFF = 1.0
# ChEMBL 最小请求间隔（秒）：EBI 限流策略，且我们的调用都是串行的
_CHEMBL_MIN_INTERVAL = 1.0
_last_chembl_call = 0.0


# 本地兜底白名单：原始写法(小写) → (标准通用名, 本地标准ID)
# 接口异常 / 限流时回退，保证主流程不中断。
_LOCAL_FALLBACK = {
    "阿司匹林": ("阿司匹林", "LOCAL:aspirin"),
    "aspirin": ("阿司匹林", "LOCAL:aspirin"),
    "乙酰水杨酸": ("阿司匹林", "LOCAL:aspirin"),
    "扑热息痛": ("对乙酰氨基酚", "LOCAL:paracetamol"),
    "paracetamol": ("对乙酰氨基酚", "LOCAL:paracetamol"),
    "acetaminophen": ("对乙酰氨基酚", "LOCAL:paracetamol"),
    "头孢曲松": ("头孢曲松", "LOCAL:ceftriaxone"),
    "ceftriaxone": ("头孢曲松", "LOCAL:ceftriaxone"),
}


def _http_get_json(url: str, timeout: float) -> dict:
    """发起 GET 并解析 JSON；网络 / HTTP 异常向上抛，由 resolve_identifier 捕获降级。"""
    req = urllib.request.Request(url, headers={"User-Agent": "ZK-DataFabric/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_cids(raw_name: str, timeout: float) -> List[int]:
    """用 PubChem PUG-REST 按名称查 CID 列表。

    限流（429）/ 服务端故障（5xx）退避后重试一次；仍失败则向上抛，由调用方降级。
    """
    name = urllib.parse.quote(raw_name.strip())
    url = f"{PUBCHEM_BASE}/compound/name/{name}/cids/JSON"
    try:
        data = _http_get_json(url, timeout)
    except urllib.error.HTTPError as e:
        if e.code in (429, 500, 502, 503, 504):
            time.sleep(_RETRY_BACKOFF)
            data = _http_get_json(url, timeout)
        else:
            raise
    return data.get("IdentifierList", {}).get("CID", [])


def _fetch_iupac(cid: str, timeout: float) -> Optional[str]:
    """按 CID 取规范 IUPAC 名（多候选时用于解析后的标准名）。"""
    url = f"{PUBCHEM_BASE}/compound/cid/{cid}/property/IUPACName/JSON"
    try:
        data = _http_get_json(url, timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return None
    props = data.get("PropertyTable", {}).get("Properties", [])
    if props:
        return props[0].get("IUPACName")
    return None


def _chembl_throttle() -> None:
    """保证 ChEMBL 两次请求间隔 ≥ _CHEMBL_MIN_INTERVAL（EBI 限流要求）。"""
    global _last_chembl_call
    wait = _CHEMBL_MIN_INTERVAL - (time.time() - _last_chembl_call)
    if wait > 0:
        time.sleep(wait)
    _last_chembl_call = time.time()


def _fetch_chembl_molecule(raw_name: str, timeout: float) -> Optional[dict]:
    """按「精确名称」查 ChEMBL 分子，命中返回分子 dict，未命中返回 None。

    为什么坚持 `pref_name__iexact`（精确）而不是全文检索 `search`：
        ChEMBL 的 `molecule/search` 是全文/模糊匹配，对单 token 常见词会返回
        一堆无关分子（前期在 PubChem 上已被 `Texas` 假命中坑过一次）。
        生物制品的通用名是唯一且规范的（Nivolumab / Etanercept …），
        精确匹配即可覆盖，代价是查不到拼写变体 —— 这个代价是值得的：
        宁可记未命中，也不要把错误分子记成"对齐成功"污染命中率。
    """
    _chembl_throttle()
    query = urllib.parse.urlencode(
        {"pref_name__iexact": raw_name.strip(), "limit": 5}
    )
    url = f"{CHEMBL_MOLECULE_URL}?{query}"

    try:
        data = _http_get_json(url, timeout)
    except urllib.error.HTTPError as e:
        if e.code in (429, 500, 502, 503, 504):
            time.sleep(_RETRY_BACKOFF)
            data = _http_get_json(url, timeout)
        else:
            raise

    molecules = data.get("molecules") or []
    for mol in molecules:
        if mol.get("molecule_chembl_id"):
            return mol
    return None


def _miss(raw_value: str, degraded: bool = False) -> dict:
    """未命中结果。

    degraded=True 表示「没查成」（限流 / 网络 / 服务端故障），
    与「查了但没有」（权威库确认无此名）必须区分——
    前者不进命中率分母，否则限流会被误算成"药名不规范"。
    """
    return {
        "found": False,
        "std_id": None,
        "source_id": "none",
        "raw_value": raw_value,
        "resolved_value": None,
        "confidence": 0.0,
        "candidates": [],
        "degraded": degraded,
    }


# ─────────────────────────────────────────────────────────────────────
# 标识符分族：格式合法性校验层（本地、不联网）
#
# 论文对应「标准标识符对齐」的第一个层次——格式合法性校验（如 CAS 校验位、
# NCT 8 位、NDA/ANDA/BLA 6 位、PMID 纯数字、DOI 10.xxxx/... 结构）。
# 该层只判定「字符串是否为该族的合法标识符」，不确认其是否真实存在于权威库
# （后者属权威源交叉核对）。因此 source_id 标记为 "format"，与 drug_name
# 的 "pubchem" 区分；confidence 仅反映格式合法性，不代表身份对齐置信度。
# ─────────────────────────────────────────────────────────────────────


def _validate_cas_number(raw: str) -> Optional[Tuple[str, str]]:
    """CAS 登记号：NNNNNN-NN-N 结构 + 校验位；合法返回 (std_id, 规范值)。"""
    m = re.fullmatch(r"(\d{2,7})-(\d{2})-(\d)", raw)
    if not m:
        return None
    head, tail, check = m.group(1), m.group(2), int(m.group(3))
    total = sum(int(ch) * (i + 1) for i, ch in enumerate(reversed(head + tail)))
    if total % 10 != check:
        return None
    return f"CAS:{raw}", raw


def _validate_application_number(raw: str) -> Optional[Tuple[str, str]]:
    """FDA 上市申请号：NDA/ANDA/BLA + 6 位数字；合法返回 (std_id, 规范值)。"""
    m = re.fullmatch(r"(NDA|ANDA|BLA)[\s\-]?(\d{6})", raw, re.IGNORECASE)
    if not m:
        return None
    kind, num = m.group(1).upper(), m.group(2)
    return f"{kind}:{num}", num


def _validate_nct_id(raw: str) -> Optional[Tuple[str, str]]:
    """ClinicalTrials.gov 登记号：NCT + 8 位数字。"""
    m = re.fullmatch(r"NCT(\d{8})", raw, re.IGNORECASE)
    if not m:
        return None
    return f"NCT:{m.group(1)}", m.group(1)


def _validate_pmid(raw: str) -> Optional[Tuple[str, str]]:
    """PubMed 文献号：1–8 位纯数字。"""
    m = re.fullmatch(r"\d{1,8}", raw)
    if not m:
        return None
    return f"PMID:{raw}", raw


def _validate_doi(raw: str) -> Optional[Tuple[str, str]]:
    """DOI：10.<registrant>/<suffix> 结构。"""
    m = re.fullmatch(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", raw)
    if not m:
        return None
    return f"DOI:{raw}", raw


_FORMAT_VALIDATORS = {
    "application_number": _validate_application_number,
    "nct_id": _validate_nct_id,
    "pmid": _validate_pmid,
    "doi": _validate_doi,
    "cas_number": _validate_cas_number,
}


def _format_result(raw_value: str, validator) -> dict:
    """格式校验层结果封装：合法→found=True(source_id="format")，非法→found=False。"""
    out = validator(raw_value.strip())
    if out is None:
        return _miss(raw_value)
    std_id, resolved = out
    return {
        "found": True,
        "std_id": std_id,
        "source_id": "format",
        "raw_value": raw_value,
        "resolved_value": resolved,
        "confidence": 0.9,
        "candidates": [],
        "degraded": False,
    }


def resolve_identifier(
    raw_value: str,
    timeout: float = DEFAULT_TIMEOUT,
    id_type: str = "drug_name",
) -> dict:
    """
    把原始标识符解析为标准标识符（五元组对齐结果）。

    按 id_type 分族分流：
        - "drug_name"（默认）：药名 / 同义词 → PubChem（小分子）→ ChEMBL（生物
          制品）双权威库，均未命中再退本地白名单
        - "application_number" / "nct_id" / "pmid" / "doi" / "cas_number"：
          走本地格式合法性校验（不联网），合法即 found=True（source_id="format"）

    参数:
        raw_value: 原始标识符字符串
        timeout:   单次 HTTP 超时（秒，仅 drug_name 联网路径使用）
        id_type:   标识符族（见上）

    返回:
        对齐结果 dict（见模块 docstring 结构）。未命中 / 非法 / 异常时 found=False，
        调用方应保留原始值，不做任何覆盖。
    """
    if not raw_value:
        return _miss(raw_value)

    if id_type != "drug_name" and id_type not in _FORMAT_VALIDATORS:
        raise ValueError(f"未知标识符族 id_type: {id_type!r}")

    validator = _FORMAT_VALIDATORS.get(id_type)
    if validator is not None:
        return _format_result(raw_value, validator)

    # 默认族（drug_name）：在线权威库解析
    degraded = False
    try:
        cids = _fetch_cids(raw_value, timeout)
        if cids:
            cid = cids[0]
            canonical = _fetch_iupac(cid, timeout) or raw_value
            multi = len(cids) > 1
            return {
                "found": True,
                "std_id": f"PUBCHEM:{cid}",
                "source_id": "pubchem",
                "raw_value": raw_value,
                "resolved_value": canonical,
                "confidence": 0.9 if not multi else 0.6,
                "candidates": [f"PUBCHEM:{c}" for c in cids] if multi else [],
                "degraded": False,
            }
    except urllib.error.HTTPError as e:
        # 404 = 权威库确认「无此名称」，是有效结论；其余状态码属故障，结论不可用
        if e.code not in (404,):
            degraded = True
    except (urllib.error.URLError, TimeoutError):
        degraded = True

    # 2) 第二权威库：ChEMBL（补生物制品覆盖）
    #    只在 PubChem「确认未命中」（degraded=False）时查。降级说明网络/限流故障，
    #    此时继续请求第二个库既不会成功，又会把等待时间翻倍。
    if not degraded:
        try:
            mol = _fetch_chembl_molecule(raw_value, timeout)
            if mol:
                chembl_id = mol["molecule_chembl_id"]
                pref = str(mol.get("pref_name") or "").strip() or raw_value
                return {
                    "found": True,
                    "std_id": f"CHEMBL:{chembl_id}",
                    "source_id": "chembl",
                    "raw_value": raw_value,
                    "resolved_value": pref,
                    "confidence": 0.9,
                    "candidates": [],
                    "degraded": False,
                }
        except urllib.error.HTTPError as e:
            # 400 = 该名称无法构成合法查询（如含特殊字符），等同「查不了此名」；
            # 404 = 端点确认无此名。两者都属有效结论，不算故障。
            if e.code not in (400, 404):
                degraded = True
        except (urllib.error.URLError, TimeoutError):
            degraded = True

    # 3) 本地白名单兜底
    fb = _LOCAL_FALLBACK.get(raw_value.strip().lower())
    if fb:
        std_name, std_id = fb
        return {
            "found": True,
            "std_id": std_id,
            "source_id": "local",
            "raw_value": raw_value,
            "resolved_value": std_name,
            "confidence": 0.7,
            "candidates": [],
            "degraded": True,
        }

    # 4) 彻底未命中（区分「查了没有」与「没查成」）
    return _miss(raw_value, degraded=degraded)
