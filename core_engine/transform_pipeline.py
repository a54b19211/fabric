"""
字段级加工管线（T6 血缘埋点载体）。

把 T2（L3 语义归一）与 T3（标准标识符对齐）接入 ETL 主链路，
并在加工前后通过 lineage_tracer 记录 normalize / align 两步血缘，
使「按标准字段反查来源」成为可能（配合 C 组的乱查询 lineage_store）。

设计原则（与 T2/T3/T4 一致）：
  - 异常绝不中断主流程、绝不丢原始值。
  - 归一/对齐未命中时保留原值，仅记录血缘，不改写。
"""

import re

from loguru import logger
from prefect import task

from core_engine.lineage_tracer import snapshot, record, record_step
from biz_rules.cleaners.normalizer_l3 import normalize_l3
from biz_rules.drug_name_normalizer import generate_candidates, is_placeholder
from core_engine.authority_client import resolve_identifier

# 单篇文档最多对齐的药名个数（每个药名一次外部请求，实测 1.4~3.4s/次）
_MAX_ALIGN_NAMES = 8
# 药名分隔符：ClinicalTrials 采集器用 "; " 拼接多个干预药物
_NAME_SEPARATOR = re.compile(r"[;；]")

# (容器, 字段名, L3 field_type) —— 需要 L3 归一的字段
_L3_TARGET_FIELDS = [
    ("structured_data", "drug_name", "drug_name"),
    ("structured_data", "dosage", "dosage"),
    ("structured_data", "concentration", "concentration"),
    ("structured_data", "amount", "amount"),
    ("structured_data", "unit", "unit"),
    ("extra", "drug_name", "drug_name"),
    ("extra", "dosage", "dosage"),
    ("extra", "concentration", "concentration"),
]

# 走「本地格式合法性校验」的标识符字段：extra 字段名 → authority_client 标识符族。
# 这一族完全不联网（CAS 校验位 / NCT 8 位 / NDA·ANDA·BLA 6 位 / PMID 数字 / DOI 结构），
# 与 drug_name 的 PubChem 在线解析是两条独立通路，统计口径也必须分开，
# 否则 90%+ 的格式命中率会把药名对齐的真实命中率稀释掉。
_IDENTIFIER_FIELDS = {
    "nct_id": "nct_id",
    "pmid": "pmid",
    "doi": "doi",
    "approval_number": "application_number",
    "cas_number": "cas_number",
}


def _container(parsed_result: dict, name: str) -> dict:
    return parsed_result.setdefault(name, {})


@task(name="apply_l3_normalization")
def apply_l3_normalization(parsed_result: dict) -> dict:
    """
    应用 T2 L3 语义归一（同义词→标准词、单位换算到 g/L），
    并记录 normalize 步血缘。未命中字段保留原值，绝不丢数据。
    """
    before = snapshot(parsed_result)
    for container_name, field, ftype in _L3_TARGET_FIELDS:
        container = _container(parsed_result, container_name)
        val = container.get(field)
        if not isinstance(val, str) or not val.strip():
            continue
        new_val = normalize_l3(val, field_type=ftype)
        if new_val != val:
            record(
                parsed_result,
                field=f"{container_name}.{field}",
                step="normalize",
                input=val,
                output=new_val,
                op="l3_normalize",
            )
            container[field] = new_val
    record_step(parsed_result, step="normalize", before=before, op="l3_normalize")
    return parsed_result


def _extract_drug_names(parsed_result: dict) -> list:
    """
    从结构化字段/站点扩展字段中取出「待对齐药名」列表。

    与旧实现的差异（缺陷 C1/C2）：
      - 旧：无药名时回退到 title，把整条标题当药名送 PubChem（必然查不到，还白耗一次请求）
      - 旧：ClinicalTrials 的 "ASPIRIN; PLACEBO" 被当作单个药名整体查询 → 多药试验必然失败
      - 新：只认真正的药名字段；支持 ";" / "；" 拆分、支持列表类型、按原顺序去重

    返回空列表表示该文档没有可用药名，调用方应跳过对齐（不是报错）。
    """
    sd = parsed_result.get("structured_data") or {}
    extra = parsed_result.get("extra") or {}
    raw_values = []
    for value in (sd.get("drug_name"), extra.get("drug_name")):
        if isinstance(value, str):
            raw_values.append(value)
        elif isinstance(value, (list, tuple, set)):
            raw_values.extend(str(v) for v in value if v not in (None, ""))

    names = []
    for value in raw_values:
        for part in _NAME_SEPARATOR.split(value):
            name = part.strip()
            if name and name not in names:
                names.append(name)
    return names


def _extract_drug_name(parsed_result: dict) -> str:
    """兼容旧调用：返回首个药名（无药名时返回空串，不再回退标题）。"""
    names = _extract_drug_names(parsed_result)
    return names[0] if names else ""


def _degraded_result(name: str) -> dict:
    """网络故障时的降级结果：未命中但不代表药名有误。"""
    return {
        "found": False, "std_id": "", "source_id": "",
        "raw_value": name, "resolved_value": name,
        "confidence": 0.0, "candidates": [], "degraded": True,
        "candidate": "", "rule": "", "candidates_tried": 0,
    }


def _skipped_result(name: str) -> dict:
    """占位名（安慰剂/盐水/空白对照）的结果：不送权威库，也不计入命中率分母。"""
    return {
        "found": False, "std_id": "", "source_id": "none",
        "raw_value": name, "resolved_value": name,
        "confidence": 0.0, "candidates": [], "degraded": False,
        "skipped": True, "skip_reason": "placeholder",
        "candidate": "", "rule": "", "candidates_tried": 0,
    }


def _align_one(name: str) -> dict:
    """对单个原始药名做「候选名生成 + 有序回退」对齐，命中即止。

    回退规则：
      - 命中（found 且 std_id 非空）→ 立即返回，并记录实际查询名与归一规则
      - 确认未命中（非降级）→ 尝试下一个候选
      - 降级（网络/限流）→ 停止后续候选：换名字也不会让接口恢复，只会放大等待
      - 任何异常 → 不抛出，按降级返回，绝不中断主流程
    """
    candidates = generate_candidates(name)
    if not candidates:
        return _skipped_result(name)

    tried = []
    last = None
    for cand in candidates:
        try:
            res = dict(resolve_identifier(cand.value))
        except Exception as e:  # 兜底，绝不中断主流程
            logger.warning(f"[Align] 标识符解析异常，跳过对齐: {name} | {e}")
            res = _degraded_result(name)
            res["candidates_tried"] = len(tried) + 1
            return res

        tried.append(cand.value)
        last = res

        if res.get("found") and str(res.get("std_id") or "").strip():
            res["raw_value"] = name            # 原始采集值必须保留，供回溯
            res["candidate"] = cand.value      # 实际送查的归一后名字
            res["rule"] = cand.rule            # 命中的归一规则（论文统计口径）
            res["candidates_tried"] = len(tried)
            res["degraded"] = False
            return res

        if res.get("degraded"):
            break  # 权威库不可用，继续换候选没有意义

    res = dict(last or _degraded_result(name))
    res["raw_value"] = name
    res["candidate"] = ""
    res["rule"] = ""
    res["candidates_tried"] = len(tried)
    return res


def _align_identifiers(
    parsed_result: dict, resolutions: dict, record_lineage: bool = True
) -> int:
    """对 extra 中的非药名标识符做本地格式合法性校验，返回处理个数。

    与药名对齐的区别：
      - 不联网、无超时、无降级态（纯函数，结果稳定可复现）
      - 只判定「是否为该族的合法标识符」，不确认其在权威库中真实存在
      - 因此单独存放在 identifier_resolutions，不与药名命中率混算

    record_lineage=False 供离线补算脚本使用：存量信封自带的 lineage 是统一
    占位假数据，往里面写真实记录会得到"半真半假"的血缘，比纯占位更危险。

    extra 为空或字段为空时整段跳过，不产生任何请求与错误。
    """
    extra = parsed_result.get("extra") or {}
    count = 0
    for field, id_type in _IDENTIFIER_FIELDS.items():
        raw = extra.get(field)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            res = dict(resolve_identifier(raw.strip(), id_type=id_type))
        except Exception as e:  # 绝不中断主流程
            logger.warning(f"[Align] 标识符格式校验异常，跳过: {field} | {e}")
            continue
        res["raw_value"] = raw
        res["field"] = field
        res["id_type"] = id_type
        resolutions[field] = res
        count += 1
        if record_lineage:
            record(
                parsed_result,
                field=f"extra.{field}",
                step="align",
                input=raw,
                output=res.get("std_id") or "",
                op="identifier_format_check",
            )
    return count


@task(name="apply_identifier_alignment")
def apply_identifier_alignment(parsed_result: dict) -> dict:
    """
    应用 T3 标准标识符对齐，记录 align 步血缘。

    两条独立通路：
      1. 药名 → PubChem 在线权威库（authority_resolutions，含降级/占位语义）
      2. 非药名标识符（nct_id / pmid / doi / approval_number / cas_number）
         → 本地格式合法性校验（identifier_resolutions，零网络）

    对齐前先做药名归一（biz_rules.drug_name_normalizer）：站点原始值往往是整段
    产品描述或「代号 + 剂量」，整段送查必然未命中，故按候选名有序回退。

    落库状态：信封层已带这两个字段、并旁路存档到 data/authority/
    （core_engine.authority_store），关系表 doc_authority_resolution 仅完成设计
    （task_doc/B2_标识符对齐落库设计.md）。

    无药名 / 解析未命中 / 接口降级时均不丢原始值，也绝不中断主流程。
    """
    before = snapshot(parsed_result)

    # ── 通路 2：本地格式校验（先做，零成本且不依赖网络）──
    id_resolutions = parsed_result.setdefault("identifier_resolutions", {})
    _align_identifiers(parsed_result, id_resolutions)

    # ── 通路 1：药名在线对齐 ──
    names = _extract_drug_names(parsed_result)
    if len(names) > _MAX_ALIGN_NAMES:
        logger.warning(
            f"[Align] 药名过多，仅对齐前 {_MAX_ALIGN_NAMES} 个: "
            f"{parsed_result.get('uuid', '?')} | 共 {len(names)} 个"
        )
        names = names[:_MAX_ALIGN_NAMES]

    resolutions = parsed_result.setdefault("authority_resolutions", {})
    for name in names:
        if is_placeholder(name):
            # 安慰剂/盐水不是药品实体，送查只会污染命中率
            res = _skipped_result(name)
            resolutions[name] = res
            record(parsed_result, field="drug_name", step="align",
                   input=name, output="", op="authority_align_skip")
            continue
        res = _align_one(name)
        resolutions[name] = res
        record(
            parsed_result,
            field="drug_name",
            step="align",
            input=name,
            output=res.get("std_id") or "",
            op="authority_align",
        )
    record_step(parsed_result, step="align", before=before, op="authority_align")
    return parsed_result
