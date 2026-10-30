"""
标准标识符对齐客户端（论文辅创新 T3）。

调用 PubChem PUG-REST，把药物 / 化合物名称（含同义词）对齐成标准标识符
（PubChem CID + 规范 IUPAC 名），解决"同物异名"导致的跨源无法对齐问题。

设计原则（与前期探索性原型对齐）：
- 网络 / 接口异常或限流时**降级**到本地白名单，再不行返回未命中，
  绝不影响主流程、绝不丢弃原始值；
- 多候选时返回候选列表交由上层裁决，不直接臆断；
- 仅做只读查询，不写库（五元组落库由 C 组的"支撑 T3 五元组"负责）。

返回结构（五元组对齐结果 + 辅助字段）：
    {
      "found":          bool,    # 是否解析到标准标识符
      "std_id":         str|None,# 标准标识符（PUBCHEM:<cid> 或 LOCAL:<id>）
      "source_id":      str,     # 来源（"pubchem" / "local" / "none"）
      "raw_value":      str,     # 原始输入
      "resolved_value": str|None,# 解析后的标准名（规范 IUPAC 名或通用名）
      "confidence":     float,   # 对齐置信度 0~1（区别于 source_mapper 的来源可信度）
      "candidates":     list,    # 多候选时的候选列表（单候选时为空）
      "degraded":       bool,    # 是否走了降级（接口异常 / 限流）
    }
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional

PUBCHEM_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
DEFAULT_TIMEOUT = 5.0

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
    """用 PubChem PUG-REST 按名称查 CID 列表。"""
    name = urllib.parse.quote(raw_name.strip())
    url = f"{PUBCHEM_BASE}/compound/name/{name}/cids/JSON"
    data = _http_get_json(url, timeout)
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


def _miss(raw_value: str) -> dict:
    return {
        "found": False,
        "std_id": None,
        "source_id": "none",
        "raw_value": raw_value,
        "resolved_value": None,
        "confidence": 0.0,
        "candidates": [],
        "degraded": False,
    }


def resolve_identifier(raw_value: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """
    把原始药名 / 同义词解析为标准标识符（五元组对齐结果）。

    参数:
        raw_value: 原始药物 / 化合物名称
        timeout:   单次 HTTP 超时（秒）

    返回:
        对齐结果 dict（见模块 docstring 结构）。未命中或异常时 found=False，
        调用方应保留原始值，不做任何覆盖。
    """
    if not raw_value:
        return _miss(raw_value)

    # 1) 在线权威库解析
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
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        pass  # 降级到本地白名单

    # 2) 本地白名单兜底
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

    # 3) 彻底未命中
    return _miss(raw_value)
