"""
实体抽取 Prompt 模板

设计原则：
  - 每个来源有专属 prompt，针对该来源的文档结构优化
  - 通用兜底 prompt 覆盖未定义来源
  - prompt 中内嵌 Neo4j 字段约束，让 LLM 知道要输出什么
  - 输出格式固定为 {"entities": [...], "relations": [...]}

关系类型白名单（relation_type 只能用这些）：
  DEVELOPS          公司 → 药物（研发）
  TARGETS           药物 → 靶点
  TREATS            药物 → 适应症
  PARTICIPATES_IN   公司/药物 → 临床试验
  INVESTED_IN       公司 → 公司（投资）
  HAS_APPROVAL      药物 → 审批事件
  IN_TRIAL          药物 → 临床试验（参与试验）
  AFFECTS           药物/化合物 → 通路
  CONTAINS          中药方剂 → 化合物
  IN_PATHWAY        靶点 → 通路
  LICENSED_TO       公司 → 公司（许可out）
  LICENSED_FROM     公司 → 公司（许可in）
  ACQUIRED_BY       公司 → 公司（被收购）
  COLLABORATED_WITH 公司 → 公司（战略合作）
  COVERS            政策 → 药物/适应症
  EVALUATED_IN      适应症 → 临床试验
"""

import json
from biz_rules.entity_schemas import ENTITY_SCHEMA_MAP, get_entity_schema_description

# ── 关系类型白名单 ──────────────────────────────────────────────────────
VALID_RELATION_TYPES = {
    "DEVELOPS", "TARGETS", "TREATS", "PARTICIPATES_IN",
    "INVESTED_IN", "HAS_APPROVAL", "IN_TRIAL", "AFFECTS",
    "CONTAINS", "IN_PATHWAY", "LICENSED_TO", "LICENSED_FROM",
    "ACQUIRED_BY", "COLLABORATED_WITH", "COVERS", "EVALUATED_IN",
}

# ── 输出格式说明（所有 prompt 共用）────────────────────────────────────
_OUTPUT_FORMAT = """
## 输出格式（严格 JSON，不要添加任何额外文字）
{
  "entities": [
    {
      "entity_type": "实体类型（见上方支持列表）",
      "entity_name": "实体的主要名称（用于消歧）",
      "entity_data": { /* 对应实体类型的字段，见上方字段说明 */ },
      "confidence_score": 0.0到1.0之间的浮点数,
      "confidence_reason": "置信度说明",
      "source_text": "原文中支持该实体的关键句子（原文截取，不超过100字）"
    }
  ],
  "relations": [
    {
      "from_entity_idx": 0,
      "to_entity_idx": 1,
      "relation_type": "关系类型（见上方白名单）",
      "relation_props": { /* 关系属性，如 date、role 等，可为空 {} */ },
      "confidence_score": 0.0到1.0之间的浮点数,
      "confidence_reason": "置信度说明",
      "source_text": "原文中支持该关系的关键句子（不超过100字）"
    }
  ]
}

注意：
- from_entity_idx 和 to_entity_idx 是 entities 数组的下标（从0开始）
- 找不到的字段填空字符串 "" 或 null，不要编造
- 置信度：原文明确提及=0.9+，可推断=0.6-0.8，不确定=0.3-0.5
- 如果文档中没有可抽取的实体，返回 {"entities": [], "relations": []}
"""

# ── 实体类型 + 字段说明（动态生成，供 prompt 引用）──────────────────────
def _build_entity_type_section(entity_types: list[str]) -> str:
    lines = ["## 支持的实体类型及字段说明"]
    for et in entity_types:
        lines.append(f"\n### {et}")
        lines.append(get_entity_schema_description(et))
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# 各来源专属 Prompt
# ═══════════════════════════════════════════════════════════════════════

def get_nmpa_extraction_prompt(text: str) -> str:
    """NMPA / CDE 监管公告：重点抽取 Drug、ApprovalEvent、Company、Policy"""
    entity_section = _build_entity_type_section(
        ["Drug", "ApprovalEvent", "Company", "Policy"]
    )
    return f"""你是医药监管信息提取专家。请从以下 NMPA/CDE 监管公告中抽取图谱实体和关系。

{entity_section}

## 关系类型白名单
{', '.join(sorted(VALID_RELATION_TYPES))}

## 抽取重点
- Drug：药品通用名、批准文号、剂型、规格
- ApprovalEvent：批准类型（NDA/BLA/补充申请）、批准日期、适应症
- Company：申请人/持有人企业名称
- Policy：如果是政策性公告，抽取政策实体
- 关系：Company DEVELOPS Drug、Drug HAS_APPROVAL ApprovalEvent

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_clinical_trial_extraction_prompt(text: str) -> str:
    """ChiCTR / ClinicalTrials 临床试验：重点抽取 ClinicalTrial、Drug、Company、Indication"""
    entity_section = _build_entity_type_section(
        ["ClinicalTrial", "Drug", "Company", "Indication", "Target"]
    )
    return f"""你是临床试验信息提取专家。请从以下临床试验注册信息中抽取图谱实体和关系。

{entity_section}

## 关系类型白名单
{', '.join(sorted(VALID_RELATION_TYPES))}

## 抽取重点
- ClinicalTrial：注册号（NCT/CTR）、期别、状态、样本量、主要终点
- Drug：试验药物名称、研发代号
- Company：申办方/研究机构
- Indication：目标疾病/适应症
- 关系：Company PARTICIPATES_IN ClinicalTrial、Drug IN_TRIAL ClinicalTrial、Drug TREATS Indication

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_pubmed_extraction_prompt(text: str) -> str:
    """PubMed 学术文献：重点抽取 Drug、Target、Indication、Pathway、Company"""
    entity_section = _build_entity_type_section(
        ["Drug", "Target", "Indication", "Pathway", "Company", "Compound"]
    )
    return f"""你是医药学术文献信息提取专家。请从以下学术文献中抽取图谱实体和关系。

{entity_section}

## 关系类型白名单
{', '.join(sorted(VALID_RELATION_TYPES))}

## 抽取重点
- Drug：药物名称、作用机制、研发阶段
- Target：靶点名称、基因符号、靶点类型
- Indication：疾病名称、ICD编码
- Pathway：涉及的信号通路
- 关系：Drug TARGETS Target、Drug TREATS Indication、Target IN_PATHWAY Pathway

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_hkex_extraction_prompt(text: str) -> str:
    """港交所公告：重点抽取 Company、DealEvent、Drug"""
    entity_section = _build_entity_type_section(
        ["Company", "DealEvent", "Drug", "ClinicalTrial"]
    )
    return f"""你是医药行业投融资信息提取专家。请从以下港交所公告中抽取图谱实体和关系。

{entity_section}

## 关系类型白名单
{', '.join(sorted(VALID_RELATION_TYPES))}

## 抽取重点
- Company：涉及的公司（发布方、合作方、收购方/被收购方）
- DealEvent：交易类型（融资/许可/并购）、金额、日期
- Drug：涉及的药物资产
- 关系：Company DEVELOPS Drug、Company LICENSED_TO/FROM Company、Company INVESTED_IN Company

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_fiercepharma_extraction_prompt(text: str) -> str:
    """FiercePharma / PHIRDA 行业新闻：重点抽取 Company、Drug、DealEvent"""
    entity_section = _build_entity_type_section(
        ["Company", "Drug", "DealEvent", "ClinicalTrial", "ApprovalEvent"]
    )
    return f"""你是医药行业新闻信息提取专家。请从以下行业新闻中抽取图谱实体和关系。

{entity_section}

## 关系类型白名单
{', '.join(sorted(VALID_RELATION_TYPES))}

## 抽取重点
- Company：涉及的药企、机构
- Drug：提及的药物（研发代号/商品名/通用名）
- DealEvent：如有交易信息（融资/合作/许可）
- ApprovalEvent：如有审批信息
- 关系：Company DEVELOPS Drug、Company COLLABORATED_WITH Company

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_drugbank_extraction_prompt(text: str) -> str:
    """DrugBank 药物数据库：重点抽取 Drug、Target、Pathway、Compound"""
    entity_section = _build_entity_type_section(
        ["Drug", "Target", "Pathway", "Compound", "Indication"]
    )
    return f"""你是药物数据库信息提取专家。请从以下 DrugBank 药物信息中抽取图谱实体和关系。

{entity_section}

## 关系类型白名单
{', '.join(sorted(VALID_RELATION_TYPES))}

## 抽取重点
- Drug：完整药物信息（modality、drug_class、phase、CAS号等）
- Target：作用靶点（UniProt ID、基因符号）
- Pathway：涉及通路（KEGG ID）
- 关系：Drug TARGETS Target、Drug AFFECTS Pathway、Drug TREATS Indication

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_policy_extraction_prompt(text: str) -> str:
    """政策文件（PHIRDA policy/healthcare_reform）：重点抽取 Policy、Drug、Company"""
    entity_section = _build_entity_type_section(
        ["Policy", "Drug", "Company", "Indication"]
    )
    return f"""你是医药政策信息提取专家。请从以下政策文件中抽取图谱实体和关系。

{entity_section}

## 关系类型白名单
{', '.join(sorted(VALID_RELATION_TYPES))}

## 抽取重点
- Policy：政策类型（集采/医保/审批改革等）、文号、生效日期、影响地区
- Drug：政策涉及的药物
- Company：政策涉及的企业
- 关系：Policy COVERS Drug、Policy COVERS Indication

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_general_extraction_prompt(text: str) -> str:
    """通用兜底 prompt，覆盖未定义来源"""
    entity_section = _build_entity_type_section(list(ENTITY_SCHEMA_MAP.keys()))
    return f"""你是医药行业知识图谱信息提取专家。请从以下文档中抽取图谱实体和关系。

{entity_section}

## 关系类型白名单
{', '.join(sorted(VALID_RELATION_TYPES))}

## 抽取要求
- 只抽取文档中明确提及的实体，不要推断或编造
- 每个实体必须有 entity_name 和 entity_type
- 关系的 from/to 下标必须对应 entities 数组中的实际位置

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


# ── 来源 → Prompt 函数映射 ──────────────────────────────────────────────
_PROMPT_MAP: dict[str, callable] = {
    "nmpa":                    get_nmpa_extraction_prompt,
    "cde":                     get_nmpa_extraction_prompt,
    "fda_press":               get_nmpa_extraction_prompt,
    "fda_drug_safety":         get_nmpa_extraction_prompt,
    "fda_drug_recalls":        get_nmpa_extraction_prompt,
    "fda_api_drug_label":      get_nmpa_extraction_prompt,
    "fda_api_enforcement":     get_nmpa_extraction_prompt,
    "chictr":                  get_clinical_trial_extraction_prompt,
    "clinicaltrials":          get_clinical_trial_extraction_prompt,
    "pubmed":                  get_pubmed_extraction_prompt,
    "hkex":                    get_hkex_extraction_prompt,
    "fiercepharma":            get_fiercepharma_extraction_prompt,
    "phirda_rd":               get_fiercepharma_extraction_prompt,
    "phirda_industry":         get_fiercepharma_extraction_prompt,
    "phirda_policy":           get_policy_extraction_prompt,
    "phirda_healthcare_reform": get_policy_extraction_prompt,
    "drugbank":                get_drugbank_extraction_prompt,
    "roche":                   get_fiercepharma_extraction_prompt,
}


def get_extraction_prompt(source_site: str, text: str) -> str:
    """
    根据来源站点选择对应的抽取 prompt。
    未知来源走通用 prompt。
    """
    fn = _PROMPT_MAP.get(source_site, get_general_extraction_prompt)
    return fn(text)
