"""
实体抽取 Prompt 模板

设计原则：
  - 每个来源有专属 prompt，针对该来源的文档结构优化
  - 通用兜底 prompt 覆盖未定义来源
  - prompt 中内嵌 Neo4j 字段约束，让 LLM 知道要输出什么
  - 输出格式固定为 {"entities": [...], "relations": [...]}

关系类型白名单（relation_type 只能用这些）：

  # 公司维度
  DEVELOPS              Company → Drug          {dev_role("原研"/"合作开发"/"仿制"), rights_role("权益方"/"被授权方"/"无权益"), start_date*}
  PARTICIPATES_IN       Company → DealEvent     {role("转让方"/"受让方"/"投资方"/"被投方"/"中介"), start_date*}
  INVESTED_IN           Company → Company       {round, amount_usd, date*}
  SUBSIDIARY_OF         Company → Company       {ratio, start_date*, end_date}
  SPONSORS              Company → ClinicalTrial
  ISSUES                Company → Policy
  APPROVES              Company → ApprovalEvent
  PARTICIPATES_IN_PROJECT Company → DevelopmentProject {role("主导方"/"合作方"), contribution}

  # 药物维度
  TARGETS               Drug → Target           {mechanism("激动剂"/"拮抗剂"/"抑制剂"/"降解剂"/"调节剂"), binding_affinity}
  TREATS                Drug → Indication       {phase, phase_date, region, status}
  IN_TRIAL              Drug → ClinicalTrial
  HAS_APPROVAL          Drug → ApprovalEvent

  # 交易维度
  HAS_ITEM              DealEvent → DealItem
  INVOLVES_DRUG         DealItem → Drug

  # 合作开发项目维度
  DEVELOPS              DevelopmentProject → Drug

  # 临床试验维度
  EVALUATES             ClinicalTrial → TrialIndication
  FOR_INDICATION        TrialIndication → Indication

  # 靶点/通路维度
  IN_PATHWAY            Target → Pathway        {role("上游"/"下游"/"平行")}
  ASSOCIATED_WITH       Target → Indication
  HOMOLOG_OF            Target → Target         {species("mouse"/"rat"/"monkey")}

  # 适应症层级
  SUBTYPE_OF            Indication → Indication {is_core_subtype}

  # 政策维度
  AFFECTS               Policy → Company/Drug/Indication/Target  {impact_direction("正向"/"负向"/"中性"), impact_scope("研发"/"生产"/"销售"/"定价"), start_date*}

  # 中药维度
  TREATS                TCMFormula → Indication
  DEVELOPED_BY          TCMFormula → Company
  CONTAINS              TCMFormula → Compound   {content_pct}
  TARGETS               Compound → Target       {mechanism, evidence_level("体外"/"动物"/"临床")}
"""

import json
from biz_rules.entity_schemas import ENTITY_SCHEMA_MAP, get_entity_schema_description

# ── 关系类型白名单 ──────────────────────────────────────────────────────
VALID_RELATION_TYPES = {
    # 公司维度
    "DEVELOPS", "PARTICIPATES_IN", "INVESTED_IN", "SUBSIDIARY_OF",
    "SPONSORS", "ISSUES", "APPROVES", "PARTICIPATES_IN_PROJECT",
    # 药物维度
    "TARGETS", "TREATS", "IN_TRIAL", "HAS_APPROVAL",
    # 交易维度
    "HAS_ITEM", "INVOLVES_DRUG",
    # 合作开发项目
    # DEVELOPS 已在公司维度
    # 临床试验维度
    "EVALUATES", "FOR_INDICATION",
    # 靶点/通路维度
    "IN_PATHWAY", "ASSOCIATED_WITH", "HOMOLOG_OF",
    # 适应症层级
    "SUBTYPE_OF",
    # 政策维度
    "AFFECTS",
    # 中药维度
    "DEVELOPED_BY", "CONTAINS",
}

# ── 关系属性说明（供 prompt 引用）──────────────────────────────────────
_RELATION_PROPS_GUIDE = """
## 关系属性说明（relation_props 字段）

| relation_type          | from → to                        | 必填属性                          | 可选属性                        |
|------------------------|----------------------------------|-----------------------------------|---------------------------------|
| DEVELOPS               | Company → Drug                   | dev_role, rights_role, start_date | end_date                        |
| DEVELOPS               | DevelopmentProject → Drug        | —                                 | —                               |
| PARTICIPATES_IN        | Company → DealEvent              | role, start_date                  | —                               |
| INVESTED_IN            | Company → Company                | date                              | round, amount_usd               |
| SUBSIDIARY_OF          | Company → Company                | start_date                        | ratio, end_date                 |
| SPONSORS               | Company → ClinicalTrial          | —                                 | —                               |
| ISSUES                 | Company → Policy                 | —                                 | —                               |
| APPROVES               | Company → ApprovalEvent          | —                                 | —                               |
| PARTICIPATES_IN_PROJECT| Company → DevelopmentProject     | role                              | contribution                    |
| TARGETS                | Drug → Target                    | mechanism                         | binding_affinity                |
| TARGETS                | Compound → Target                | mechanism, evidence_level         | —                               |
| TREATS                 | Drug/TCMFormula → Indication     | —                                 | phase, phase_date, region, status|
| IN_TRIAL               | Drug → ClinicalTrial             | —                                 | —                               |
| HAS_APPROVAL           | Drug → ApprovalEvent             | —                                 | —                               |
| HAS_ITEM               | DealEvent → DealItem             | —                                 | —                               |
| INVOLVES_DRUG          | DealItem → Drug                  | —                                 | —                               || EVALUATES              | ClinicalTrial → TrialIndication  | —                                 | —                               |
| FOR_INDICATION         | TrialIndication → Indication     | —                                 | —                               |
| IN_PATHWAY             | Target → Pathway                 | —                                 | role                            |
| ASSOCIATED_WITH        | Target → Indication              | —                                 | —                               |
| HOMOLOG_OF             | Target → Target                  | species                           | —                               |
| SUBTYPE_OF             | Indication → Indication          | —                                 | is_core_subtype                 |
| AFFECTS                | Policy → Company/Drug/Indication/Target | impact_direction, start_date | impact_scope               |
| DEVELOPED_BY           | TCMFormula → Company             | —                                 | —                               |
| CONTAINS               | TCMFormula → Compound            | —                                 | content_pct                     |

dev_role 枚举: "原研" / "合作开发" / "仿制"
rights_role 枚举: "权益方" / "被授权方" / "无权益"
mechanism 枚举(Drug→Target): "激动剂" / "拮抗剂" / "抑制剂" / "降解剂" / "调节剂"
evidence_level 枚举: "体外" / "动物" / "临床"
impact_direction 枚举: "正向" / "负向" / "中性"
impact_scope 枚举: "研发" / "生产" / "销售" / "定价"
role(IN_PATHWAY) 枚举: "上游" / "下游" / "平行"
species 枚举: "mouse" / "rat" / "monkey"
role(PARTICIPATES_IN) 枚举: "转让方" / "受让方" / "投资方" / "被投方" / "中介"
role(PARTICIPATES_IN_PROJECT) 枚举: "主导方" / "合作方"
item_type(DealItem) 枚举: "药物权益" / "技术平台权益"
status(DevelopmentProject) 枚举: "进行中" / "完成" / "终止"
status(TrialIndication) 枚举: "进行中" / "完成" / "终止"
phase(TrialIndication) 枚举: "I" / "II" / "III"
"""

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
      "relation_props": { /* 关系属性，严格按上方属性说明填写，枚举值必须在规定范围内 */ },
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
- relation_props 中的枚举字段必须使用规定的枚举值，不得自造
- 关系方向必须严格遵守白名单中的 from→to 定义
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
    """NMPA / CDE / FDA 监管公告：重点抽取 Drug、ApprovalEvent、Company、Policy"""
    entity_section = _build_entity_type_section(
        ["Drug", "ApprovalEvent", "Company", "Policy"]
    )
    return f"""你是医药监管信息提取专家。请从以下监管公告中抽取图谱实体和关系。

{entity_section}

{_RELATION_PROPS_GUIDE}

## 抽取重点
- Drug：药品通用名、批准文号、剂型、规格
- ApprovalEvent：批准类型（NDA/BLA/补充申请）、批准日期、适应症
- Company：申请人/持有人企业名称
- Policy：如果是政策性公告，抽取政策实体
- 关系重点：
  * Company -[DEVELOPS]-> Drug（dev_role="原研", rights_role="权益方", start_date必填）
  * Drug -[HAS_APPROVAL]-> ApprovalEvent
  * Company -[APPROVES]-> ApprovalEvent（监管机构批准时使用）
  * Policy -[AFFECTS]-> Drug/Company（impact_direction, start_date必填）

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_clinical_trial_extraction_prompt(text: str) -> str:
    """ChiCTR / ClinicalTrials 临床试验：重点抽取 ClinicalTrial、Drug、Company、Indication、TrialIndication"""
    entity_section = _build_entity_type_section(
        ["ClinicalTrial", "Drug", "Company", "Indication", "TrialIndication", "Target"]
    )
    return f"""你是临床试验信息提取专家。请从以下临床试验注册信息中抽取图谱实体和关系。

{entity_section}

{_RELATION_PROPS_GUIDE}

## 抽取重点
- ClinicalTrial：注册号（NCT/CTR）、期别、状态、样本量、主要终点
- Drug：试验药物名称、研发代号
- Company：申办方/研究机构
- Indication：目标疾病/适应症（标准疾病名）
- TrialIndication：试验中的具体适应症条目（含人群、治疗线描述）
- 关系重点：
  * Company -[SPONSORS]-> ClinicalTrial
  * Drug -[IN_TRIAL]-> ClinicalTrial
  * ClinicalTrial -[EVALUATES]-> TrialIndication
  * TrialIndication -[FOR_INDICATION]-> Indication
  * Drug -[TREATS]-> Indication（phase, region可填）

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_pubmed_extraction_prompt(text: str) -> str:
    """PubMed 学术文献：重点抽取 Drug、Target、Indication、Pathway、Compound"""
    entity_section = _build_entity_type_section(
        ["Drug", "Target", "Indication", "Pathway", "Company", "Compound"]
    )
    return f"""你是医药学术文献信息提取专家。请从以下学术文献中抽取图谱实体和关系。

{entity_section}

{_RELATION_PROPS_GUIDE}

## 抽取重点
- Drug：药物名称、作用机制、研发阶段
- Target：靶点名称、基因符号、靶点类型
- Indication：疾病名称、ICD编码
- Pathway：涉及的信号通路
- Compound：活性化合物（中药/天然产物研究时）
- 关系重点：
  * Drug -[TARGETS]-> Target（mechanism必填，如"抑制剂"/"拮抗剂"）
  * Drug -[TREATS]-> Indication
  * Target -[IN_PATHWAY]-> Pathway（role可填"上游"/"下游"/"平行"）
  * Target -[ASSOCIATED_WITH]-> Indication
  * Target -[HOMOLOG_OF]-> Target（species必填）
  * Compound -[TARGETS]-> Target（mechanism, evidence_level必填）
  * Indication -[SUBTYPE_OF]-> Indication（疾病分型时使用）

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_hkex_extraction_prompt(text: str) -> str:
    """港交所公告：重点抽取 Company、DealEvent、DealItem、Drug、DevelopmentProject"""
    entity_section = _build_entity_type_section(
        ["Company", "DealEvent", "DealItem", "Drug", "ClinicalTrial", "DevelopmentProject"]
    )
    return f"""你是医药行业投融资信息提取专家。请从以下港交所公告中抽取图谱实体和关系。

{entity_section}

{_RELATION_PROPS_GUIDE}

## 抽取重点
- Company：涉及的公司（发布方、合作方、收购方/被收购方）
- DealEvent：交易类型（融资/许可/并购/战略合作）、金额、日期
- DealItem：交易条款明细（首付款/里程碑/股权等，挂在 DealEvent 下）
- Drug：涉及的药物资产
- DevelopmentProject：如有联合开发项目
- 关系重点：
  * Company -[PARTICIPATES_IN]-> DealEvent（role必填："转让方"/"受让方"/"投资方"/"被投方"）
  * DealEvent -[HAS_ITEM]-> DealItem
  * DealItem -[INVOLVES_DRUG]-> Drug
  * Company -[DEVELOPS]-> Drug（dev_role, rights_role, start_date必填）
  * Company -[INVESTED_IN]-> Company（date必填）
  * Company -[SUBSIDIARY_OF]-> Company（start_date必填）
  * Company -[PARTICIPATES_IN_PROJECT]-> DevelopmentProject（role必填）
  * DevelopmentProject -[DEVELOPS]-> Drug

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_fiercepharma_extraction_prompt(text: str) -> str:
    """FiercePharma / PHIRDA 行业新闻：重点抽取 Company、Drug、DealEvent、DealItem"""
    entity_section = _build_entity_type_section(
        ["Company", "Drug", "DealEvent", "DealItem", "ClinicalTrial",
         "ApprovalEvent", "DevelopmentProject"]
    )
    return f"""你是医药行业新闻信息提取专家。请从以下行业新闻中抽取图谱实体和关系。

{entity_section}

{_RELATION_PROPS_GUIDE}

## 抽取重点
- Company：涉及的药企、机构
- Drug：提及的药物（研发代号/商品名/通用名）
- DealEvent：如有交易信息（融资/合作/许可/并购）
- DealItem：交易条款明细（首付款/里程碑等）
- ApprovalEvent：如有审批信息
- 关系重点：
  * Company -[DEVELOPS]-> Drug（dev_role, rights_role, start_date必填）
  * Company -[PARTICIPATES_IN]-> DealEvent（role必填）
  * DealEvent -[HAS_ITEM]-> DealItem
  * DealItem -[INVOLVES_DRUG]-> Drug
  * Company -[INVESTED_IN]-> Company（date必填）
  * Company -[PARTICIPATES_IN_PROJECT]-> DevelopmentProject（role必填）

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_drugbank_extraction_prompt(text: str) -> str:
    """DrugBank 药物数据库：重点抽取 Drug、Target、Pathway、Compound、Indication"""
    entity_section = _build_entity_type_section(
        ["Drug", "Target", "Pathway", "Compound", "Indication"]
    )
    return f"""你是药物数据库信息提取专家。请从以下 DrugBank 药物信息中抽取图谱实体和关系。

{entity_section}

{_RELATION_PROPS_GUIDE}

## 抽取重点
- Drug：完整药物信息（modality、drug_class、phase、CAS号等）
- Target：作用靶点（UniProt ID、基因符号）
- Pathway：涉及通路（KEGG ID）
- 关系重点：
  * Drug -[TARGETS]-> Target（mechanism必填）
  * Drug -[TREATS]-> Indication
  * Target -[IN_PATHWAY]-> Pathway（role可填）
  * Target -[ASSOCIATED_WITH]-> Indication

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_policy_extraction_prompt(text: str) -> str:
    """政策文件（PHIRDA policy/healthcare_reform）：重点抽取 Policy、Drug、Company、Indication"""
    entity_section = _build_entity_type_section(
        ["Policy", "Drug", "Company", "Indication", "Target"]
    )
    return f"""你是医药政策信息提取专家。请从以下政策文件中抽取图谱实体和关系。

{entity_section}

{_RELATION_PROPS_GUIDE}

## 抽取重点
- Policy：政策类型（集采/医保/审批改革等）、文号、生效日期、影响地区
- Drug：政策涉及的药物
- Company：政策涉及的企业
- 关系重点：
  * Policy -[AFFECTS]-> Drug（impact_direction, impact_scope, start_date必填）
  * Policy -[AFFECTS]-> Company（impact_direction, impact_scope, start_date必填）
  * Policy -[AFFECTS]-> Indication（impact_direction, start_date必填）
  * Company -[ISSUES]-> Policy（发布机构）

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_general_extraction_prompt(text: str) -> str:
    """通用兜底 prompt，覆盖未定义来源"""
    entity_section = _build_entity_type_section(list(ENTITY_SCHEMA_MAP.keys()))
    return f"""你是医药行业知识图谱信息提取专家。请从以下文档中抽取图谱实体和关系。

{entity_section}

{_RELATION_PROPS_GUIDE}

## 抽取要求
- 只抽取文档中明确提及的实体，不要推断或编造
- 每个实体必须有 entity_name 和 entity_type
- 关系的 from/to 下标必须对应 entities 数组中的实际位置
- relation_props 中的枚举字段必须使用规定值

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


# ── 来源 → Prompt 函数映射 ──────────────────────────────────────────────
_PROMPT_MAP: dict[str, callable] = {
    "nmpa":                     get_nmpa_extraction_prompt,
    "cde":                      get_nmpa_extraction_prompt,
    "fda_press":                get_nmpa_extraction_prompt,
    "fda_drug_safety":          get_nmpa_extraction_prompt,
    "fda_drug_recalls":         get_nmpa_extraction_prompt,
    "fda_api_drug_label":       get_nmpa_extraction_prompt,
    "fda_api_enforcement":      get_nmpa_extraction_prompt,
    "chictr":                   get_clinical_trial_extraction_prompt,
    "clinicaltrials":           get_clinical_trial_extraction_prompt,
    "pubmed":                   get_pubmed_extraction_prompt,
    "hkex":                     get_hkex_extraction_prompt,
    "fiercepharma":             get_fiercepharma_extraction_prompt,
    "phirda_rd":                get_fiercepharma_extraction_prompt,
    "phirda_industry":          get_fiercepharma_extraction_prompt,
    "phirda_policy":            get_policy_extraction_prompt,
    "phirda_healthcare_reform": get_policy_extraction_prompt,
    "drugbank":                 get_drugbank_extraction_prompt,
    "roche":                    get_fiercepharma_extraction_prompt,
}


def get_extraction_prompt(source_site: str, text: str) -> str:
    """根据来源站点选择对应的抽取 prompt，未知来源走通用 prompt。"""
    fn = _PROMPT_MAP.get(source_site, get_general_extraction_prompt)
    return fn(text)
