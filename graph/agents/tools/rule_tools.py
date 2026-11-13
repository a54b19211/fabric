# -*- coding: utf-8 -*-
"""Agent 规则查询工具 — 实体类型字段规则、关系规则。"""

from langchain_core.tools import tool

# 实体类型必填字段
_REQUIRED_FIELDS: dict[str, list[str]] = {
    "Drug": ["name"],
    "Company": ["name"],
    "Target": ["name", "gene_symbol"],
    "Disease": ["name"],
    "ClinicalTrial": ["name", "trial_id"],
    "Compound": ["name"],
    "Pathway": ["name"],
    "Policy": ["name"],
    "Project": ["name"],
    "Indication": ["name"],
}

# 合法关系类型（from_type, to_type） → 允许的 relation_type
_VALID_RELATIONS: dict[tuple[str, str], set[str]] = {
    ("Drug", "Target"): {"binds", "inhibits", "activates", "modulates", "antagonizes", "agonizes"},
    ("Drug", "Disease"): {"treats", "prevents", "indicates", "contraindicates"},
    ("Drug", "Compound"): {"contains", "derived_from", "metabolized_to"},
    ("Drug", "Drug"): {"contains", "synergistic_with", "antagonistic_with", "metabolized_to"},
    ("Company", "Drug"): {"develops", "manufactures", "markets", "holds_patent_for", "distributes"},
    ("Company", "Target"): {"researches"},
    ("ClinicalTrial", "Drug"): {"tests", "evaluates", "compares"},
    ("ClinicalTrial", "Disease"): {"studies", "targets"},
    ("ClinicalTrial", "Target"): {"investigates"},
    ("Target", "Disease"): {"associated_with", "biomarker_for", "causes"},
    ("Target", "Pathway"): {"involved_in", "regulates"},
    ("Disease", "Target"): {"associated_with"},
    ("Drug", "ClinicalTrial"): {"tested_in"},
}

# 实体类型描述（供 LLM 判断类型是否正确）
_TYPE_DESCRIPTIONS: dict[str, str] = {
    "Drug": '药品/药物 — 有明确商品名或通用名的治疗性药物，如「阿司匹林」、「Keytruda」。不包括笼统的「抗生素」这类泛指。',
    "Company": '公司/机构 — 制药企业、生物技术公司、研究机构、监管机构，如「辉瑞」、「NMPA」。',
    "Target": '靶点/蛋白质/基因 — 药物作用的分子靶标，如「PD-1」、「EGFR」、「HER2」。',
    "Disease": '疾病/适应症 — 具体的疾病、症状或医学状况，如「非小细胞肺癌」、「高血压」。',
    "ClinicalTrial": '临床试验 — 有明确编号或名称的临床研究，如「NCT04283461」、「CheckMate-227」。',
    "Compound": '化合物/分子 — 非药物的化学实体，如「白藜芦醇」、「紫杉烷」。',
    "Pathway": '信号通路/代谢通路 — 如「MAPK通路」、「Wnt/β-catenin通路」。',
    "Policy": "政策/法规 — 药政文件、审批政策、医保政策。",
    "Project": "项目/工程 — 科研项目、建设规划。",
    "Indication": "适应症 — 药物获批的适应症描述。",
}


@tool
def get_entity_type_rules(entity_type: str) -> dict:
    """获取指定实体类型的字段规则、合法关系类型和类型描述。

    Args:
        entity_type: 实体类型名称

    Returns:
        {
            "required_fields": [...],
            "valid_relations": [{"pair": [from, to], "types": [...]}, ...],
            "description": "...",
            "type_match_hints": "..."  # 用于判断该类型是否匹配实体名
        }
    """
    required = _REQUIRED_FIELDS.get(entity_type, ["name"])
    description = _TYPE_DESCRIPTIONS.get(entity_type, f"{entity_type} 类型实体")

    # 收集所有涉及该类型的合法关系
    valid_relations = []
    for (from_t, to_t), rel_types in _VALID_RELATIONS.items():
        if from_t == entity_type or to_t == entity_type:
            valid_relations.append({
                "pair": [from_t, to_t],
                "types": sorted(rel_types),
            })

    return {
        "entity_type": entity_type,
        "required_fields": required,
        "valid_relations": valid_relations,
        "description": description,
    }
