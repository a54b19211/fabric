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
  CAUSES                Drug → Indication       {—}  -- 药物导致不良反应/禁忌症（与 TREATS 语义相反）
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

  # 药物制剂维度
  CONTAINS              Drug → Drug             {content_pct}  -- 药物制剂含有活性成分（双向关系：反向遍历即"是XX的活性成分"）
"""

import inspect

from biz_rules.entity_schemas import ENTITY_SCHEMA_MAP, get_entity_schema_description

_PROMPT_DELIMITERS = ["---", "###", "SYSTEM:", "ASSISTANT:", "USER:", "<|im_start|>", "<|im_end|>"]

def _sanitize_metadata(value: str) -> str:
    v = str(value).replace("\n", " ").replace("\r", " ")[:200]
    for d in _PROMPT_DELIMITERS:
        v = v.replace(d, "")
    return v.strip()

_ALL_ENTITY_TYPES = list(ENTITY_SCHEMA_MAP.keys())  # 所有 16 种实体类型，各来源统一可见


def _entity_types_excluding(excluded: set[str]) -> list[str]:
    """白名单收敛：返回排除指定类型后的实体类型列表。"""
    return [t for t in _ALL_ENTITY_TYPES if t not in excluded]


# 白名单收敛（保守版）：仅砍确定无关类型，其余等样本累计再扩；核心消歧类型永不排除。
_CLINICAL_TRIAL_EXCLUDED = {"Policy", "ApprovalEvent"}   # 临床试验注册信息不含监管审批/政策
_FDA_DRUG_LABEL_EXCLUDED = {"DealEvent", "TCMFormula"}   # 药品说明书不含交易/中药方剂

# ── 关系类型白名单 ──────────────────────────────────────────────────────
VALID_RELATION_TYPES = {
    # 公司维度
    "DEVELOPS", "PARTICIPATES_IN", "INVESTED_IN", "SUBSIDIARY_OF",
    "SPONSORS", "ISSUES", "APPROVES", "PARTICIPATES_IN_PROJECT",
    # 药物维度
    "TARGETS", "TREATS", "CAUSES", "IN_TRIAL", "HAS_APPROVAL",
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
    # 安全/召回维度
    "RELATED_TO",
}

# ── 关系属性速查表（紧凑版）──────────────────────────────────────────────
_RELATION_PROPS_GUIDE = """
## relation_type 属性速查（格式: from→to | 必填属性 | 可选属性）

Company→Drug DEVELOPS           | dev_role, rights_role, start_date | end_date
DevProject→Drug DEVELOPS        | —                                 | —
Company→DealEvent PARTICIPATES_IN | role, start_date                  | —
Company→Company INVESTED_IN     | date                              | round, amount_usd
Company→Company SUBSIDIARY_OF   | start_date                        | ratio, end_date
Company→Trial SPONSORS          | —                                 | —
Company→Policy ISSUES           | —                                 | —
Company→ApprovalEvent APPROVES  | —                                 | —
Company→DevProject PARTICIPATES_IN_PROJECT | role                    | contribution
Drug→Target TARGETS             | mechanism                         | binding_affinity
Compound→Target TARGETS         | mechanism, evidence_level          | —
Drug→Indication TREATS          | —                                 | phase, phase_date, region, status
Drug→Indication CAUSES          | —                                 | —  -- 不良反应/禁忌症（与 TREATS 相反）
TCMFormula→Indication TREATS    | —                                 | phase, phase_date, region, status
Drug→Trial IN_TRIAL             | —                                 | —
Drug→ApprovalEvent HAS_APPROVAL | —                                 | —
DealEvent→DealItem HAS_ITEM     | —                                 | —
DealItem→Drug INVOLVES_DRUG     | —                                 | —
Trial→TrialIndication EVALUATES | —                                 | —
TrialIndication→Indication FOR_INDICATION | —                        | —
Target→Pathway IN_PATHWAY       | —                                 | role
Target→Indication ASSOCIATED_WITH | —                               | —
Target→Target HOMOLOG_OF        | species                           | —
Indication→Indication SUBTYPE_OF | —                                | is_core_subtype
Policy→X AFFECTS                | impact_direction, start_date       | impact_scope
TCMFormula→Company DEVELOPED_BY | —                                 | —
TCMFormula→Compound CONTAINS    | —                                 | content_pct
Drug→Drug CONTAINS              | content_pct                        | —
Drug→SafetyEvent RELATED_TO     | —                                 | —
Company→RecallEvent RELATED_TO  | —                                 | —
Drug→RecallEvent RELATED_TO     | —                                 | —

枚举:
  dev_role: 原研 / 合作开发 / 仿制
  rights_role: 权益方 / 被授权方 / 无权益
  mechanism(Drug→Target): 激动剂 / 拮抗剂 / 抑制剂 / 降解剂 / 调节剂
  evidence_level: 体外 / 动物 / 临床
  impact_direction: 正向 / 负向 / 中性
  impact_scope: 研发 / 生产 / 销售 / 定价
  role(IN_PATHWAY): 上游 / 下游 / 平行
  role(PARTICIPATES_IN): 转让方 / 受让方 / 投资方 / 被投方 / 中介 / 许可方 / 被许可方 / 合作方
  role(PARTICIPATES_IN_PROJECT): 主导方 / 合作方
  species: mouse / rat / monkey
  status(DevProject): 进行中 / 完成 / 终止
  status(TrialIndication): 进行中 / 完成 / 终止
  phase(TrialIndication): I / II / III
  item_type(DealItem): 药物权益 / 技术平台权益
"""

# ── 输出格式说明（所有 prompt 共用）────────────────────────────────────
_OUTPUT_FORMAT = """
## 输出格式（严格 JSON，不要添加任何额外文字）
输出纯 JSON 文本，禁止用 ```json 代码块标记包裹，直接以 { 开头。
{
  "entities": [
    {
      "entity_type": "实体类型（必须从上方支持列表中选择，禁止自造）",
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
      "relation_type": "关系类型（必须从上方白名单中选择，禁止自造）",
      "relation_props": { /* 关系属性，严格按上方属性说明填写，枚举值必须在规定范围内 */ },
      "confidence_score": 0.0到1.0之间的浮点数,
      "confidence_reason": "置信度说明",
      "source_text": "原文中支持该关系的关键句子（不超过100字）"
    }
  ]
}

注意：
- entity_type 和 relation_type 必须严格从上方列表/白名单中选取，禁止使用列表外的任何类型
- from_entity_idx 和 to_entity_idx 是 entities 数组的下标（从0开始）
- 找不到的字段填空字符串 "" 或 null，不要编造
- 置信度：原文明确提及=0.9+，可推断=0.6-0.8，不确定=0.3-0.5
- 如果文档中没有可抽取的实体，返回 {"entities": [], "relations": []}
- relation_props 中的枚举字段必须使用规定的枚举值，不得自造
- 关系方向必须严格遵守白名单中的 from→to 定义
- Drug / Target / Indication / Pathway 的 entity_data 必须填写 name_en 字段：
  entity_name 本身是英文 → 直接填入 name_en；entity_name 是中文 → 翻译为英文填入 name_en
- 双语别名规则：entity_name 为英文时，将中文翻译填入 aliases；entity_name 为中文时，将英文翻译填入 aliases
  例如 "Hydrocortisone" → aliases: ["氢化可的松"]；"阿司匹林" → aliases: ["Aspirin"]
"""

# ── 全局抽取质量规则（所有 prompt 共用，压缩版）─────────────────────────
_COMMON_EXTRACTION_RULES = """
## 强制规则（违反将导致输出被拒绝）

### 0. 类型约束（最高优先级）
- entity_type 必须严格从上方的「支持的实体类型」列表中选择，禁止自造或使用列表外的实体类型
- relation_type 必须严格从上方的 relation_type 白名单中选择，禁止自造或使用白名单外的关系类型

### 1. entity_name 命名
- Drug → INN 通用名 + 关键修饰（如 "liposomal doxorubicin"）
- Company → 完整官方名 + 股票代码（如 "Roche Holding AG (ROG.SW)"）
- Indication → ICD/MeSH 标准疾病名 + 分期分型（如 "NSCLC Stage IV"）
- Target → HGNC 基因符号 + 膜蛋白别名（如 "EGFR" / "CLDN18.2"）
- ClinicalTrial → NCT/CTR 注册号（如 "NCT04256421"）
- 禁止: "the drug"、"这家公司"、"上述适应症" 等模糊名称

### 2. 去重
- 同文本内同一实体只出现一次（"Keytruda"="pembrolizumab" → 一个 Drug，aliases 列变体）
- 多块拆分时实体不重复创建，但关系可在任一块建立

### 3. 禁止抽取的实体类型
- 文献元数据（日期/作者/期刊/页码） | 泛称类别名（"化疗药物"→ 填 drug_class 字段，不作为 Drug） | 背景引文中的非核心实体 | 非医药公司（律所/会计所，除非是交易中介） | 辅料溶剂（除非作为 CONTAINS 目标）

### 4. source_text
- 原文截取 ≤100 字符，不改写/不总结/不翻译
- 取最直接的一句证据

### 5. 关系方向（严格按白名单 from→to）
- Company→Drug = DEVELOPS | Drug→Target = TARGETS | Policy→X = AFFECTS
- 方向错误 = 图谱推理链断裂，整条关系无效

### 6. 关系完整性
- from/to 下标必须在 entities 范围内
- 关系引用的实体必须已存在于 entities 数组

### 7. 置信度（禁止全标 0.9+）
- 0.9-1.0: 原文明确提及 | 0.7-0.85: 上下文强推断 | 0.5-0.65: 多线索综合 | 0.3-0.45: 间接暗示/存疑

### 8. 药物制剂名拆解 — CONTAINS 关系（双向）
- 中文药品名常以「片/胶囊/注射液/颗粒/口服液/滴丸/栓剂/软膏/分散片/缓释片/肠溶片/咀嚼片」等剂型后缀结尾
- 识别出剂型后缀后，必须将活性成分也独立抽取为一个 Drug 实体
- 制剂 Drug（含剂型后缀）→ 活性成分 Drug（去掉剂型后缀）: CONTAINS，content_pct 可填
- 例：文档中出现「酚酞片」→ 两个 Drug 实体:
  * Drug「酚酞片」(entity_name 保留剂型后缀)
  * Drug「酚酞」(entity_name 去掉剂型后缀，作为活性成分)
  * 关系: 酚酞片 -[CONTAINS]-> 酚酞
- 例：「阿莫西林胶囊」→ Drug「阿莫西林胶囊」-[CONTAINS]-> Drug「阿莫西林」
- 例：「紫杉醇注射液」→ Drug「紫杉醇注射液」-[CONTAINS]-> Drug「紫杉醇」
- 活性成分如果已在 entities 数组中出现过，直接引用已有下标，不要重复创建
- 复方制剂（如「氨酚伪麻片」）不要拆解——中文复方制剂名多为缩写，LLM 无法可靠映射到具体活性成分
- 关系是双向的：图谱中反向遍历 CONTAINS 即可查询「某活性成分被哪些制剂使用」
- 注意：不要将「葡萄糖注射液」「氯化钠注射液」「注射用水」等辅料/溶媒拆为药品实体
"""

_EMPTY_RESULT_GUIDANCE = """
### 空结果规则
- 如果文档中没有任何可抽取的实体，返回 {"entities": [], "relations": []}
- 不要在无法确认时强行编造实体——宁可返回空结果，也不返回虚假实体
- 空结果示例场景：纯元数据页、目录页、索引页、参考文献列表"""

_CDE_TABLE_FEWSHOT = """
### 表格化药品列表示例

输入：
```
2025 年国家药监局批准的创新药

序号  药品名称      上市许可持有人            药品类型  适应症/功能主治                                  加快上市程序
1     阿夫凯泰片     Cytokinetics,Incorporated 化学药品  梗阻性肥厚型心肌病（HCM, NYHA II-III级）          突破性治疗优先审评审批
2     艾考磷布韦片  宜昌东阳光长江药业股份公司  化学药品  基因1/2/3/6型成人慢性丙型肝炎（HCV）
```

抽取结果：
```json
{
  "entities": [
    {"entity_type": "Drug", "entity_name": "阿夫凯泰片", "entity_data": {"name": "阿夫凯泰片", "drug_class": "化学药品"}, "confidence_score": 0.95, "source_text": "1 阿夫凯泰片"},
    {"entity_type": "Company", "entity_name": "Cytokinetics,Incorporated", "entity_data": {"name": "Cytokinetics,Incorporated", "name_en": "Cytokinetics,Incorporated"}, "confidence_score": 0.9, "source_text": "Cytokinetics,Incorporated"},
    {"entity_type": "Indication", "entity_name": "梗阻性肥厚型心肌病", "entity_data": {"name": "梗阻性肥厚型心肌病", "name_en": "Obstructive Hypertrophic Cardiomyopathy"}, "confidence_score": 0.9, "source_text": "梗阻性肥厚型心肌病（HCM）"},
    {"entity_type": "ApprovalEvent", "entity_name": "阿夫凯泰片-2025创新药批准", "entity_data": {"application_type": "NDA", "designations": ["突破性疗法", "优先审评"], "status": "获批"}, "confidence_score": 0.9, "source_text": "突破性治疗优先审评审批"},
    {"entity_type": "Drug", "entity_name": "艾考磷布韦片", "entity_data": {"name": "艾考磷布韦片", "drug_class": "化学药品"}, "confidence_score": 0.95, "source_text": "2 艾考磷布韦片"},
    {"entity_type": "Company", "entity_name": "宜昌东阳光长江药业股份公司", "entity_data": {"name": "宜昌东阳光长江药业股份公司"}, "confidence_score": 0.9, "source_text": "宜昌东阳光长江药业股份公司"},
    {"entity_type": "Indication", "entity_name": "慢性丙型肝炎", "entity_data": {"name": "慢性丙型肝炎", "name_en": "Chronic Hepatitis C"}, "confidence_score": 0.9, "source_text": "慢性丙型肝炎（HCV）"}
  ],
  "relations": [
    {"from_entity_idx": 1, "to_entity_idx": 0, "relation_type": "DEVELOPS", "relation_props": {"dev_role": "原研"}, "confidence_score": 0.9, "source_text": "Cytokinetics,Incorporated"},
    {"from_entity_idx": 0, "to_entity_idx": 2, "relation_type": "TREATS", "relation_props": {}, "confidence_score": 0.9, "source_text": "梗阻性肥厚型心肌病"},
    {"from_entity_idx": 0, "to_entity_idx": 3, "relation_type": "HAS_APPROVAL", "relation_props": {}, "confidence_score": 0.9, "source_text": "突破性治疗优先审评审批"},
    {"from_entity_idx": 5, "to_entity_idx": 4, "relation_type": "DEVELOPS", "relation_props": {"dev_role": "原研"}, "confidence_score": 0.9, "source_text": "宜昌东阳光长江药业"},
    {"from_entity_idx": 4, "to_entity_idx": 6, "relation_type": "TREATS", "relation_props": {}, "confidence_score": 0.9, "source_text": "慢性丙型肝炎"}
  ]
}
```
注意：第 2 行无加快上市程序，不创建 ApprovalEvent；entity_data 只填原文能确认的字段。
"""

_CT_FEWSHOT = """
### 临床试验注册示例

输入：
```
注册号：ChiCTR2000029418
注册时间：2020-01-30
注册号状态：预注册
注册题目：中药治疗新型冠状病毒感染肺炎(COVID-19)重型患者随机对照试验
研究类型：干预性研究
研究设计：随机平行对照
干预措施：西医治疗
纳入标准：1) 年龄≥18周岁； 2) 符合重型COVID-19诊断标准
```

抽取结果：
```json
{
  "entities": [
    {"entity_type": "ClinicalTrial", "entity_name": "ChiCTR2000029418", "entity_data": {"trial_id": "ChiCTR2000029418", "phase": "", "status": "预注册", "start_date": "2020-01-30"}, "confidence_score": 0.95, "source_text": "注册号：ChiCTR2000029418"},
    {"entity_type": "Drug", "entity_name": "中药", "entity_data": {"name": "中药", "drug_class": "中药"}, "confidence_score": 0.7, "source_text": "中药治疗新型冠状病毒感染肺炎"},
    {"entity_type": "Indication", "entity_name": "新型冠状病毒感染肺炎", "entity_data": {"name": "新型冠状病毒感染肺炎", "name_en": "COVID-19", "category": "感染"}, "confidence_score": 0.9, "source_text": "新型冠状病毒感染肺炎(COVID-19)重型"},
    {"entity_type": "TrialIndication", "entity_name": "ChiCTR2000029418-重型COVID-19-西医治疗", "entity_data": {"phase": "", "status": "预注册"}, "confidence_score": 0.85, "source_text": "中药治疗新型冠状病毒感染肺炎(COVID-19)重型"}
  ],
  "relations": [
    {"from_entity_idx": 1, "to_entity_idx": 0, "relation_type": "IN_TRIAL", "relation_props": {}, "confidence_score": 0.85, "source_text": "中药治疗新型冠状病毒感染肺炎"},
    {"from_entity_idx": 0, "to_entity_idx": 3, "relation_type": "EVALUATES", "relation_props": {}, "confidence_score": 0.9, "source_text": "ChiCTR2000029418"},
    {"from_entity_idx": 3, "to_entity_idx": 2, "relation_type": "FOR_INDICATION", "relation_props": {}, "confidence_score": 0.9, "source_text": "新型冠状病毒感染肺炎(COVID-19)重型"},
    {"from_entity_idx": 1, "to_entity_idx": 2, "relation_type": "TREATS", "relation_props": {}, "confidence_score": 0.8, "source_text": "中药治疗COVID-19"}
  ]
}
```
注意：ChiCTR 不填 phase 为常见情况；药物名称为通用描述（如"中药"）时 confidence_score 适当降低。
"""

_CT_GOV_FEWSHOT = """
### ClinicalTrials.gov JSON 示例

输入（API 返回的 JSON 片段）：
```
{"identificationModule": {"nctId": "NCT00039715", "organization": {"fullName": "National Institutes of Health Clinical Center (CC)"}, "briefTitle": "Effects of Hydrocortisone in Patients With Post-Traumatic Stress Disorder", "officialTitle": "Effect of Hydrocortisone Infusion on Hippocampal Glucose Metabolism...in Patients With PTSD"}}
```

抽取结果：
```json
{
  "entities": [
    {"entity_type": "ClinicalTrial", "entity_name": "NCT00039715", "entity_data": {"trial_id": "NCT00039715", "brief_title": "Effects of Hydrocortisone in Patients With PTSD"}, "confidence_score": 0.95, "source_text": "nctId: NCT00039715"},
    {"entity_type": "Drug", "entity_name": "Hydrocortisone", "entity_data": {"name": "Hydrocortisone", "name_en": "Hydrocortisone", "aliases": ["氢化可的松"]}, "confidence_score": 0.9, "source_text": "Effects of Hydrocortisone"},
    {"entity_type": "Indication", "entity_name": "Post-Traumatic Stress Disorder", "entity_data": {"name": "Post-Traumatic Stress Disorder", "name_en": "Post-Traumatic Stress Disorder", "aliases": ["创伤后应激障碍", "PTSD"]}, "confidence_score": 0.9, "source_text": "Post-Traumatic Stress Disorder (PTSD)"},
    {"entity_type": "Company", "entity_name": "National Institutes of Health Clinical Center", "entity_data": {"name": "National Institutes of Health Clinical Center", "types": ["academic"]}, "confidence_score": 0.85, "source_text": "National Institutes of Health Clinical Center (CC)"}
  ],
  "relations": [
    {"from_entity_idx": 1, "to_entity_idx": 0, "relation_type": "IN_TRIAL", "relation_props": {}, "confidence_score": 0.9, "source_text": "Hydrocortisone in Patients With PTSD"},
    {"from_entity_idx": 1, "to_entity_idx": 2, "relation_type": "TREATS", "relation_props": {}, "confidence_score": 0.9, "source_text": "Hydrocortisone...PTSD"},
    {"from_entity_idx": 3, "to_entity_idx": 0, "relation_type": "SPONSORS", "relation_props": {}, "confidence_score": 0.85, "source_text": "National Institutes of Health Clinical Center"}
  ]
}
```
注意：英文实体名务必把中文翻译填入 aliases（如 Hydrocortisone→氢化可的松，PTSD→创伤后应激障碍）。
"""

# ── 实体类型 + 字段说明（动态生成，供 prompt 引用）──────────────────────
_DRUG_BOUNDARY = """  ⚠️ Drug 边界约束（重要）：
  - Drug 仅指「有明确药理作用的药品」（化学药/生物制品/中药），INN 通用名优先
  - 以下**不是 Drug**，不得抽为 Drug 实体：
    · 医疗器械（导管/支架/螺钉/植入物/球囊/缝线/手术导航系统等）
    · 体外诊断试剂（检测试剂盒/试纸/校准品等）
    · 化妆品（染发剂/防晒霜/爽肤水/面膜/口红等）
    · 牙科/骨科材料（树脂/骨水泥/合金粉末等）
    · 手术/操作类干预（手术、切除术、移植、起搏、导管术、内镜、针灸、康复训练等）
    · 行为/心理干预（健康教育、心理咨询、运动、瑜伽、认知行为疗法等）
    · 器械/设备干预（支架、起搏器、植入物、外骨骼、软件、可穿戴设备等）
    · 膳食/营养干预（维生素补充、膳食强化、营养配方、益生菌、饮食方案等）
    · 放疗/放射干预（放射治疗、放疗方案等；放射性药物如 Radium-223 除外）
  - 区分关键：以「是否含具有药理作用的化学/生物/中药活性成分」为准；手术、行为、器械、膳食、放疗等干预手段一律不是 Drug
  - 上述非 Drug 实体属「本系统暂不覆盖的类型」，跳过不抽取"""


def _build_entity_type_section(entity_types: list[str]) -> str:
    lines = ["## 支持的实体类型及字段说明"]
    for et in entity_types:
        lines.append(f"\n### {et}")
        lines.append(get_entity_schema_description(et))
        if et == "Drug":
            lines.append(_DRUG_BOUNDARY)
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# 各来源专属 Prompt
# ═══════════════════════════════════════════════════════════════════════

def get_nmpa_extraction_prompt(text: str) -> str:
    """NMPA / CDE 监管公告：优先 Drug、ApprovalEvent、Company、Policy、SafetyEvent"""
    entity_section = _build_entity_type_section(_ALL_ENTITY_TYPES)
    return f"""你是医药监管信息提取专家。请从以下 NMPA/CDE 监管公告中抽取图谱实体和关系。

上方列出了全部支持的实体类型，请严格从上方列表中选择，不得使用列表之外的实体类型。

{entity_section}

{_RELATION_PROPS_GUIDE}
{_EMPTY_RESULT_GUIDANCE}

## 本来源抽取重点

### 审批/公告类文档
- 药品批准/补充申请 → Drug + ApprovalEvent
- 现场检查/不合规/警告信 → SafetyEvent（event_type="安全警示"/"现场检查", source_agency="NMPA", severity="严重"/"中等"）
- 产品召回 → RecallEvent（recall_class="I"/"II"/"III"）
- 申请人/持有人/生产企业 → Company（name 用中文全称）
- 政策/通告/指导原则 → Policy（policy_type="审批改革"/"监管规范"/"鼓励政策"）
- Drug：INN通用名优先，批准文号（国药准字H/Z/S/J...）和登记号（Y开头）填入 registration_number
- ApprovalEvent：批准类型（NDA/BLA/补充申请/再注册）、批准日期
- 关系重点：Company-[DEVELOPS]->Drug | Drug-[HAS_APPROVAL]->ApprovalEvent | Policy-[AFFECTS]->Drug/Company | Drug-[RELATED_TO]->SafetyEvent | Company-[RELATED_TO]->RecallEvent | Drug（制剂）-[CONTAINS]->Drug（活性成分）

{_CDE_TABLE_FEWSHOT}

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_fda_extraction_prompt(text: str, sub_source: str = "") -> str:
    """FDA 监管公告：全类型可见，子源调整重点"""
    excluded = _FDA_DRUG_LABEL_EXCLUDED if "drug_label" in sub_source else set()
    entity_section = _build_entity_type_section(_entity_types_excluding(excluded))
    sub_guidance = ""

    if "drug_label" in sub_source:
        sub_guidance = """
## 子源特化：FDA Drug Label（药品说明书）
- 优先 Drug + Indication + Target
- 从说明书中提取完整适应症列表、靶点信息、用法用量
- 不良反应（Adverse Reactions）章节的疾病/症状 → 抽 Indication 实体（疾病本体），关系 Drug -[CAUSES]-> Indication；禁止把这些疾病/症状抽成 SafetyEvent
- 抗感染药适应症边界：适应症写成「[病原体]引起的感染」时（如 "infections caused by Streptococcus pneumoniae"），Indication 的 entity_name 取**感染病名**（如 "Streptococcal Pneumonia"），不取病原体名；病原体（细菌/病毒/真菌等微生物，含 species / ATCC 菌株号）不是 Indication，禁止以病原体名作 Indication 的 entity_name
- SafetyEvent 只用于黑框警告（Boxed Warning）/安全警示这类「事件」，entity_name=药品名+事件类型，禁止用疾病名作 entity_name"""
    elif "drug_safety" in sub_source:
        sub_guidance = """
## 子源特化：FDA Drug Safety Communication
- 优先 SafetyEvent（event_type="安全警示"） + Drug
- 提取安全信号描述（safety_signal）、受影响人群（affected_population）、FDA 建议（recommendation）
- 关系：Drug -[RELATED_TO]-> SafetyEvent"""
    elif "enforcement" in sub_source:
        sub_guidance = """
## 子源特化：FDA Enforcement
- 优先 Policy（policy_type="监管规范"） + Company
- 提取违规企业、违规行为、处罚措施
- 关系：Policy -[AFFECTS]-> Company（impact_direction="负向"）"""

    return f"""你是FDA监管信息提取专家。请从以下FDA监管公告中抽取图谱实体和关系。

上方列出了全部支持的实体类型，请严格从上方列表中选择，不得使用列表之外的实体类型。

{entity_section}

{_RELATION_PROPS_GUIDE}
{_EMPTY_RESULT_GUIDANCE}
{sub_guidance}

## 本来源抽取重点
- Drug：通用名优先、商品名入 aliases、NDA/BLA号、modality、drug_class
- ApprovalEvent：申请类型(NDA/BLA/sNDA/sBLA/IND/ANDA)、designations(突破性疗法/优先审评/孤儿药/快速通道)、多个审批事件分别创建
- Company：申请人/持证人/违规企业，name 用英文官方全称
- SafetyEvent：event_type(不良事件/安全警示/黑框警告/上市后监测)、severity
- RecallEvent：recall_class(I/II/III)、recall_scope(零售/批发/用户级)
- Policy：政策公告或执法文件
- 不抽取: 辅料为Compound | Advisory Committee 纪要 | 同药不同剂型重复创建
- 关系重点：Company-[DEVELOPS]->Drug | Drug-[HAS_APPROVAL]->ApprovalEvent | Drug-[RELATED_TO]->SafetyEvent | Company-[RELATED_TO]->RecallEvent | Policy-[AFFECTS]->Drug/Company | Drug（制剂）-[CONTAINS]->Drug（活性成分）

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_clinical_trial_extraction_prompt(text: str) -> str:
    """ChiCTR / ClinicalTrials 临床试验：重点抽取 ClinicalTrial、Drug、Company、Indication、TrialIndication"""
    entity_section = _build_entity_type_section(_entity_types_excluding(_CLINICAL_TRIAL_EXCLUDED))
    return f"""你是临床试验信息提取专家。请从以下临床试验注册信息中抽取图谱实体和关系。

上方列出了全部支持的实体类型，请严格从上方列表中选择，不得使用列表之外的实体类型。

{entity_section}

{_RELATION_PROPS_GUIDE}
{_EMPTY_RESULT_GUIDANCE}

## 本来源抽取重点
- ClinicalTrial 的 trial_id 必须使用注册号：NCT 后跟8位数字（如 NCT04256421），ChiCTR 后跟数字（如 ChiCTR200003xxxx）
- 不要将 sponsor 的 CRO 公司误认为是研发主体——CRO 作为 Company 抽取但 role 不一定是 DEVELOPS
- 多臂试验中每一条 arm 的药物单独创建 Drug 实体
- 试验的 inclusion/exclusion criteria 描述填入 TrialIndication 中（人群描述、治疗线次）
- ClinicalTrial 实体只创建一次，多臂试验不要创建多个同 NCT 的 ClinicalTrial
- ClinicalTrial：注册号（NCT/CTR 编号，直接填 trial_id）、期别（phase）、状态（status）、样本量（sample_size）、主要终点（primary_endpoint）
- Drug：试验药物名称（INN名）和/或研发代号，多个药物组合时各自拆分

### 干预措施（interventions）按类型判定 Drug（重要，防污染）
- ClinicalTrials.gov：原始 JSON 的 armsInterventionsModule.interventions 中，每条干预带 type 字段：
  * type = DRUG / BIOLOGICAL / COMBINATION_PRODUCT → 抽为 Drug（取干预 name）
  * type = PROCEDURE / DEVICE / BEHAVIORAL / DIETARY_SUPPLEMENT / RADIATION / DIAGNOSTIC_TEST / GENETIC / OTHER → **不是 Drug**，跳过（本系统不覆盖这些干预类型）
- ChiCTR：干预措施为「手术/针灸/推拿/康复/运动/心理治疗/放疗/器械」等非药物手段 → 不是 Drug，跳过；仅「化学药/生物制品/中药」抽为 Drug
- conditionsModule.conditions（或 ChiCTR 的「研究疾病/目标疾病」）是疾病/适应症，抽为 Indication，**严禁抽成 Drug**
- 忽略文档头部 frontmatter 的 drug_name 字段和正文「研究涉及药物/干预」引导句——它可能是上游映射错误（疾病被错标为药物），一律以原始 JSON 的 interventions.type 和 conditions 为准

### 疫苗/生物制品边界（防疾病名误抽为 Drug）
- 疫苗试验中，conditions 里的疾病名（如 Measles / Mumps / Rubella / Influenza / COVID-19 等）是「疫苗预防的疾病」，必须抽为 Indication，**严禁抽成 Drug**
- 只有干预本身（interventions 里 type=BIOLOGICAL 且 name 含 Vaccine/疫苗 的干预，如 MMR Vaccine、Influenza Vaccine）才抽为 Drug
- 判断关键：以 interventions 的 name 为准——干预名含 Vaccine/疫苗 → Drug；conditions 里的裸疾病名 → Indication
- 疫苗接种、接种程序本身不是实体，跳过；「MMR Vaccination」这类表述若指疫苗产品可归 Drug，若指接种动作则跳过
- Company：申办方/合作方/CRO，name 用官方全称
- Indication：目标疾病/适应症，使用标准疾病名（非自定义缩写）
- 不良事件（Adverse Events / Serious Adverse Events）列表中的疾病/症状 → 抽 Indication 实体（疾病本体），关系 Drug -[CAUSES]-> Indication；禁止把这些疾病/症状抽成 SafetyEvent
- TrialIndication：试验中每条适应症 arm 创建一条（含人群描述、治疗线次、phase、status）
- Target：如试验为靶向治疗/免疫治疗，提取对应靶点（含生物标志物检测条件）
- 关系重点：
  * Company -[SPONSORS]-> ClinicalTrial
  * Drug -[IN_TRIAL]-> ClinicalTrial
  * ClinicalTrial -[EVALUATES]-> TrialIndication
  * TrialIndication -[FOR_INDICATION]-> Indication
  * Drug -[TREATS]-> Indication（phase, region可填）
  * Drug -[TARGETS]-> Target（如为靶向药物）

{_CT_FEWSHOT}

{_CT_GOV_FEWSHOT}

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_pubmed_extraction_prompt(text: str, metadata: dict = None) -> str:
    """PubMed 学术文献：重点抽取 Drug、Target、Indication、Pathway、Compound"""
    entity_section = _build_entity_type_section(_ALL_ENTITY_TYPES)
    meta_header = ""
    if metadata:
        lines = ["## 文献元数据"]
        if metadata.get("pmid"):
            lines.append(f"- PMID: {_sanitize_metadata(metadata['pmid'])}")
        if metadata.get("journal"):
            lines.append(f"- 期刊: {_sanitize_metadata(metadata['journal'])}")
        if metadata.get("year"):
            lines.append(f"- 发表年份: {_sanitize_metadata(metadata['year'])}")
        if metadata.get("study_type"):
            lines.append(f"- 研究类型: {_sanitize_metadata(metadata['study_type'])}")
        meta_header = "\n".join(lines) + "\n"

    study_type = (metadata or {}).get("study_type", "")
    study_type = _sanitize_metadata(study_type) if study_type else ""
    strategy = ""
    if study_type and ("RCT" in study_type or "randomized" in study_type.lower() or "临床试验" in study_type):
        strategy = """
## 研究类型特化：随机对照试验（RCT）
- 重点抽取 ClinicalTrial 实体（NCT编号、期别、样本量、主要终点）和 TrialIndication（含定量结果）
- Drug -[TREATS]-> Indication 关系应附带 phase、样本量等信息
- 置信度可适当偏高（RCT 是高质量证据）"""
    elif study_type and ("meta" in study_type.lower() or "系统" in study_type):
        strategy = """
## 研究类型特化：Meta分析 / 系统综述
- 实体置信度可偏高（综合多篇文献结论）
- 严格区分"作者发现"和"被引文献发现"，只抽取本研究的结论
- 注意辨别涉及到的多组 Drug-Indication 配对"""
    elif study_type and ("preclinical" in study_type.lower() or "体外" in study_type or "动物" in study_type or "in vitro" in study_type.lower() or "in vivo" in study_type.lower()):
        strategy = """
## 研究类型特化：临床前研究
- 重点抽取 Target、Compound、Pathway
- Compound -[TARGETS]-> Target 关系的 evidence_level 填 "体外" 或 "动物"
- Drug -[TREATS]-> Indication 关系置信度应偏低（临床前发现未必转化为临床疗效）"""

    return f"""你是医药学术文献信息提取专家。请从以下学术文献中抽取图谱实体和关系。

{meta_header}
{entity_section}

上方列出了全部支持的实体类型，请严格从上方列表中选择，不得使用列表之外的实体类型。

{_RELATION_PROPS_GUIDE}
{_COMMON_EXTRACTION_RULES}
{_EMPTY_RESULT_GUIDANCE}
{strategy}

## 本来源特有规则
- 严格区分"作者发现"和"被引文献发现"：只抽取本研究的结论，不抽取引文中提到的实体（除非声明为指导当前研究的依据）
- 综述（review）类文章：只抽取作者综合分析后得出的结论，背景介绍中提到的实体谨慎抽取（降低置信度）
- 临床前研究（in vitro/in vivo）：不抽取 Drug -[TREATS]-> Indication（临床前结果不等于临床疗效），只建立 Compound/Target/Pathway 维度关系
- 个案报告（case report）：实体较少属于正常情况，不要强行编造

## 抽取重点
- Drug：药物名称（INN名）、作用机制、研发阶段，如文中提到研发代号填入 aliases
- Target：靶点名称、基因符号（HGNC 官方符号优先，如 EGFR 不是 erbB1）、靶点类型（target_type）
- Indication：疾病名称，使用标准 ICD/MeSH 术语，如有分期/分型在 name 中体现
- Pathway：涉及的信号通路（KEGG pathway ID 优先填入 id 字段），pathway_type: 信号通路/代谢通路/免疫通路
- Compound：活性化合物（天然产物/中药组分），compound_type: 生物碱/黄酮/皂苷/多糖/萜类
- Company：机构/药企（仅当文中明确标注机构关联时抽取）
- ClinicalTrial：引用的临床试验注册号（NCT编号填入 trial_id）
- TrialIndication：文献报告的试验适应症条目及疗效数据
- 关系重点：
  * Drug -[TARGETS]-> Target（mechanism必填："激动剂"/"拮抗剂"/"抑制剂"/"降解剂"/"调节剂"）
  * Drug -[TREATS]-> Indication
  * Target -[IN_PATHWAY]-> Pathway（role可填"上游"/"下游"/"平行"）
  * Target -[ASSOCIATED_WITH]-> Indication
  * Target -[HOMOLOG_OF]-> Target（species必填："mouse"/"rat"/"monkey"）
  * Compound -[TARGETS]-> Target（mechanism, evidence_level必填："体外"/"动物"/"临床"）
  * Indication -[SUBTYPE_OF]-> Indication（疾病分型时使用）
  * ClinicalTrial -[EVALUATES]-> TrialIndication
  * TrialIndication -[FOR_INDICATION]-> Indication

## 定量数据抽取
如文献报告了以下疗效数据，务必填入 TrialIndication 或 ClinicalTrial 实体中：
- ORR（客观缓解率，如"ORR 65%"）→ TrialIndication.orr（填小数如 0.65）
- PFS（无进展生存期，如"中位PFS 8.3个月"）→ TrialIndication.pfs_months
- OS（总生存期，如"中位OS 15.6个月"）→ TrialIndication.os_months
- 样本量（如"246 patients"）→ ClinicalTrial.sample_size（填整数）
- 如文献无定量结果，各字段留空，不编造

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_hkex_extraction_prompt(text: str) -> str:
    """港交所公告：重点抽取 Company、DealEvent、DealItem、Drug、DevelopmentProject"""
    entity_section = _build_entity_type_section(_ALL_ENTITY_TYPES)
    return f"""你是医药行业投融资信息提取专家。请从以下港交所公告中抽取图谱实体和关系。

上方列出了全部支持的实体类型，请严格从上方列表中选择，不得使用列表之外的实体类型。

{entity_section}

{_RELATION_PROPS_GUIDE}
{_COMMON_EXTRACTION_RULES}
{_EMPTY_RESULT_GUIDANCE}

## 港交所公告特有结构
公告类型决定实体分布，抽取时注意：
- 自愿性公告（Voluntary Announcement）：常见临床进展、业务更新 → 重点 Drug、ClinicalTrial
- 内幕消息（Inside Information）：重大交易、临床数据披露 → 重点 DealEvent、Drug
- 须予披露交易 / 关连交易（Disclosable/Connected Transaction）→ 重点 DealEvent、DealItem、Company
- 年报/中报（Annual/Interim Report）：管线更新、财务数据 → 重点 DevelopmentProject、Drug、Company

## 本来源特有规则
- Company 务必填写 stock_code（格式"数字.HK"，如 01801.HK）和 exchange = "港股"
- 发布公告的主体公司作为首个 Company 实体，合作方/收购标的/投资方各自独立创建
- 交易金额：原始币种及金额（amount_orig + currency_orig）和折算美元（total_amount_usd）均需填写
- 不要抽取公告中的会计事务所、律师事务所、财务顾问为公司实体（除非其作为交易中介且角色明确）
- DealItem 挂在 DealEvent 下：一份合同有多个条款项（首付款、里程碑、销售分成）时各自创建一条
- 不抽取关连交易中的"独立财务顾问"等非医药机构为公司实体

## 抽取重点
- Company：所有涉及公司，name 中英文都填
  * stock_code：港交所股票代码，格式为"数字.HK"（如 01801.HK、09926.HK）
  * exchange：统一填 "港股"
  * 发布公告的公司 + 合作方/收购标的/投资方 均需抽取
- DealEvent：交易类型（deal_category: 融资/许可/并购/战略合作）、金额、日期（date）、交易状态（status）
  * 港交所公告金额常见币种为 HKD 和 USD，两者都填：
    - amount_orig / currency_orig：原始币种及金额
    - total_amount_usd：折算美元金额
  * 如有汇率信息，填入 exchange_rate_date
  * 关连交易在 deal_category 中体现
- DealItem：交易条款明细（首付款/里程碑款/销售分成/股权比例）
  * item_type: "药物权益" 或 "技术平台权益"
  * rights_regions：权益地区列表（如 ["大中华区", "全球"]）
- Drug：涉及的药物资产（研发代号填入 aliases，INN 通用名填入 name）
- ClinicalTrial：如有临床试验数据披露
- DevelopmentProject：联合开发项目名称、起止日期、状态
- 关系重点：
  * Company -[PARTICIPATES_IN]-> DealEvent（role必填：按交易角色选填）
  * DealEvent -[HAS_ITEM]-> DealItem
  * DealItem -[INVOLVES_DRUG]-> Drug
  * Company -[DEVELOPS]-> Drug（dev_role, rights_role, start_date必填）
  * Company -[INVESTED_IN]-> Company（date必填, round/amount_usd可选）
  * Company -[SUBSIDIARY_OF]-> Company（start_date必填, ratio可选）
  * Company -[PARTICIPATES_IN_PROJECT]-> DevelopmentProject（role必填："主导方"/"合作方", contribution可选）
  * DevelopmentProject -[DEVELOPS]-> Drug

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_fiercepharma_extraction_prompt(text: str) -> str:
    """FiercePharma / PHIRDA 行业新闻：重点抽取 Company、Drug、DealEvent、DealItem"""
    entity_section = _build_entity_type_section(_ALL_ENTITY_TYPES)
    return f"""你是医药行业新闻信息提取专家。请从以下行业新闻中抽取图谱实体和关系。

上方列出了全部支持的实体类型，请严格从上方列表中选择，不得使用列表之外的实体类型。

{entity_section}

{_RELATION_PROPS_GUIDE}
{_COMMON_EXTRACTION_RULES}
{_EMPTY_RESULT_GUIDANCE}

## 本来源特有规则
- FiercePharma 等英文行业新闻中，entity_name 使用英文通用名（INN），商品名填入 aliases
- 新闻标题中的主体公司通常为核心实体，但不要创建"FiercePharma"本身为公司实体
- 新闻中引用的分析师评论、第三方观点不产生实体——只抽取事实性信息
- 交易金额如含多个币种，USD 金额填入 total_amount_usd，原始币种填入 amount_orig/currency_orig

## 抽取重点
- Company：涉及的药企、机构，name 用英文官方全称
- Drug：提及的药物（研发代号填入 aliases，INN 通用名填入 name）
- DealEvent：如有交易信息（融资/合作/许可/并购/战略合作）
- DealItem：交易条款明细（首付款/里程碑等）
- ApprovalEvent：如有审批信息（FDA/EMA/NMPA批准）
- ClinicalTrial：如有临床试验进展
- DevelopmentProject：如有多方合作研发项目
- 关系重点：
  * Company -[DEVELOPS]-> Drug（dev_role, rights_role, start_date必填）
  * Company -[PARTICIPATES_IN]-> DealEvent（role必填）
  * DealEvent -[HAS_ITEM]-> DealItem
  * DealItem -[INVOLVES_DRUG]-> Drug
  * Company -[INVESTED_IN]-> Company（date必填）
  * Company -[PARTICIPATES_IN_PROJECT]-> DevelopmentProject（role必填）
  * Drug -[HAS_APPROVAL]-> ApprovalEvent
  * Company -[SPONSORS]-> ClinicalTrial
  * Drug -[IN_TRIAL]-> ClinicalTrial

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_phirda_rd_industry_prompt(text: str) -> str:
    """PHIRDA研发追踪和产业资讯：重点抽取 Drug、Company、ClinicalTrial、DealEvent、DevelopmentProject"""
    entity_section = _build_entity_type_section(_ALL_ENTITY_TYPES)
    return f"""你是中国医药产业信息提取专家。请从以下药促会研发追踪或产业资讯中抽取图谱实体和关系。

上方列出了全部支持的实体类型，请严格从上方列表中选择，不得使用列表之外的实体类型。

{entity_section}

{_RELATION_PROPS_GUIDE}
{_COMMON_EXTRACTION_RULES}
{_EMPTY_RESULT_GUIDANCE}

## 本来源特有规则
- 中国创新药研发术语识别：1类新药=创新药，2类=改良型新药，3/4类=仿制药
- 研发阶段术语对应：IND受理→临床前，完成临床I/II/III→按实际填写 phase，NDA受理→NDA申报阶段，获批上市→已上市
- DealEvent 许可交易类型：license-in（引进）→受让方角色, license-out（对外授权）→转让方角色
- 不要将政府资助项目名称直接作为 DevelopmentProject 实体抽取——仅当有多方明确合作时抽取
- Company 的 types 标注：biotech（初创药企）、pharma（大型药企）、cro（CRO/CDMO）、regulator（监管机构）

## 抽取重点
- Drug：在研或已上市药物（INN名+研发代号）、研发阶段（phase_cn）、创新类型（innovation_type）
- Company：本土药企、跨国药企、研发机构、CRO/CDMO企业，name用中文全称
- ClinicalTrial：临床试验进展、入组情况、结果公布
- DealEvent：许可引进（license-in）、对外授权（license-out）、合作研发、并购交易
  * deal_category: 融资/许可/并购/战略合作
- DevelopmentProject：企业间联合开发项目
- ApprovalEvent：NMPA审批进展、批准上市
  * designations: 突破性疗法/优先审评/孤儿药/快速通道
- DealItem：交易条款（首付款、里程碑、销售分成）
- 关系重点：
  * Company -[DEVELOPS]-> Drug（dev_role="原研"/"合作开发"/"仿制", rights_role="权益方"/"被授权方", start_date）
  * Company -[PARTICIPATES_IN]-> DealEvent（role必填："转让方"/"受让方"/"许可方"/"被许可方"）
  * DealEvent -[HAS_ITEM]-> DealItem
  * DealItem -[INVOLVES_DRUG]-> Drug
  * Company -[SPONSORS]-> ClinicalTrial
  * Drug -[IN_TRIAL]-> ClinicalTrial
  * Drug -[HAS_APPROVAL]-> ApprovalEvent
  * Company -[PARTICIPATES_IN_PROJECT]-> DevelopmentProject（role必填："主导方"/"合作方"）
  * DevelopmentProject -[DEVELOPS]-> Drug

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_policy_extraction_prompt(text: str) -> str:
    """政策文件（PHIRDA policy/healthcare_reform）：重点抽取 Policy、Drug、Company、Indication"""
    entity_section = _build_entity_type_section(_ALL_ENTITY_TYPES)
    return f"""你是医药政策信息提取专家。请从以下政策文件中抽取图谱实体和关系。

上方列出了全部支持的实体类型，请严格从上方列表中选择，不得使用列表之外的实体类型。

{entity_section}

{_RELATION_PROPS_GUIDE}
{_EMPTY_RESULT_GUIDANCE}

## 本来源特有规则
- Policy 实体需标注政策类型（policy_type）：集采/医保目录/审批改革/临床指南/价格管控/鼓励政策/监管规范
- 政策影响范围（affected_regions）：全国/特定省份/特定城市
- 政策影响的 drug_class 维度：化学药品/生物制品/中药/医疗器械（填在 Policy 的 description 或关联关系中）
- 不抽取政策中举例的通用药品（如"例如XX药品"）为 Drug 实体——仅当政策直接针对特定药品/类别时才抽取

## 抽取重点
- Policy：政策标题（title）、政策类型（policy_type）、文号（doc_number）、发布日期（issue_date）、生效日期（effective_date）、影响地区（affected_regions）
- Drug：政策直接针对的药品/品类（集采目录品种、医保目录品种）
- Company：政策明确涉及的企业
- Indication：政策针对的治疗领域/适应症方向
- Target：如有针对特定靶点的政策（如PD-1/PD-L1集采）
- 关系重点：
  * Policy -[AFFECTS]-> Drug（impact_direction: "正向"/"负向"/"中性", impact_scope: "研发"/"生产"/"销售"/"定价", start_date必填）
  * Policy -[AFFECTS]-> Company（impact_direction, impact_scope, start_date必填）
  * Policy -[AFFECTS]-> Indication（impact_direction, start_date必填）
  * Company -[ISSUES]-> Policy（发布机构）

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_drugbank_extraction_prompt(text: str) -> str:
    """DrugBank 药物数据库：重点抽取 Drug、Target、Pathway、Compound、Indication"""
    entity_section = _build_entity_type_section(_ALL_ENTITY_TYPES)
    return f"""你是药物数据库信息提取专家。请从以下 DrugBank 药物信息中抽取图谱实体和关系。

上方列出了全部支持的实体类型，请严格从上方列表中选择，不得使用列表之外的实体类型。

{entity_section}

{_RELATION_PROPS_GUIDE}
{_EMPTY_RESULT_GUIDANCE}

## 本来源特有规则
- DrugBank 数据是高度结构化的，将所有字段映射到对应 entity_data 字段
- DrugBank ID 填入 Drug.id（如 DB00001），不要与 NMPA 批准文号混淆
- Target 的 UniProt ID 填入 Target.id（如 P00533），不要留空
- Pathway 的 KEGG ID 填入 Pathway.id（如 hsa04151），name 用通路名
- DrugBank 中的 "InChI Key" 不是 CAS号——cas_number 和 InChI Key 是不同的字段
- 不要将 DrugBank 中的 "Carriers" 和 "Transporters" 作为 Target 实体抽取——只有 pharmacological targets 才是 Target

## 抽取重点
- Drug：完整药物信息（DrugBank ID→id, 通用名→name, 商品名→aliases, modality, drug_class, phase, CAS号→cas_number）
- Target：作用靶点（UniProt ID→id, 基因符号→gene_symbol, 靶点名称→name, target_type, target_class）
- Pathway：涉及通路（KEGG ID→id, 通路名称→name, pathway_type）
- Compound：如涉及天然产物组分
- Indication：获批适应症列表，每个适应症创建独立 Indication 实体
- 关系重点：
  * Drug -[TARGETS]-> Target（mechanism必填："激动剂"/"拮抗剂"/"抑制剂"/"降解剂"/"调节剂", binding_affinity可选）
  * Drug -[TREATS]-> Indication
  * Target -[IN_PATHWAY]-> Pathway（role可填："上游"/"下游"/"平行"）
  * Target -[ASSOCIATED_WITH]-> Indication

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_nccn_extraction_prompt(text: str) -> str:
    """NCCN 临床实践指南：重点抽取 Drug、Indication、Target、ClinicalTrial、TrialIndication、Company"""
    entity_section = _build_entity_type_section(_ALL_ENTITY_TYPES)
    return f"""你是临床实践指南信息提取专家。请从以下 NCCN 临床实践指南中抽取图谱实体和关系。

上方列出了全部支持的实体类型，请严格从上方列表中选择，不得使用列表之外的实体类型。

{entity_section}

{_RELATION_PROPS_GUIDE}
{_COMMON_EXTRACTION_RULES}
{_EMPTY_RESULT_GUIDANCE}

## NCCN 指南特有结构
NCCN 指南按以下层级组织治疗方案，抽取时需注意：
- 推荐层级：首选方案（Preferred Regimens）> 其他推荐方案（Other Recommended Regimens）> 特定情况有用（Useful in Certain Circumstances）
- 治疗线次：一线（1st-line）/ 二线（2nd-line）/ 三线（3rd-line）/ 辅助（adjuvant）/ 新辅助（neoadjuvant）/ 维持（maintenance）
- 证据等级：Category 1（高级别证据+统一共识）> Category 2A > 2B > Category 3（低级别证据）
- 生物标志物导向：许多方案附带分子条件（如"for EGFR mutation-positive patients"），应抽取 Drug-[TARGETS]->Target 关系

## 本来源特有规则
- 指南中的 "Preferred Regimens" 下的每个单药/联合方案都要独立处理：联合方案（如 pembrolizumab + pemetrexed + carboplatin）→ 三个 Drug 实体，每个 Drug 都 TREATS 同一个 Indication
- 同一药物可出现在多个治疗线次（一线+二线），每次出现如果 regimen 不同应创建不同的 TrialIndication 或关系
- Indication 的 name 应包含癌种+分期+分型信息（如 "Non-Small Cell Lung Cancer, Stage IV, Non-Squamous"）
- 不要将 "clinical trial enrollment preferred" 等非药物条目抽取为 Drug 实体

## 抽取重点
- Drug：指南推荐的药物（化疗药、靶向药、免疫治疗药），INN 通用名入 name
  * 多条药物组合方案应将每个药物单独抽取
- Indication：癌症类型、分期、分子分型（使用标准疾病名）
- Target：药物作用靶点及对应生物标志物（EGFR/ALK/ROS1/BRAF/PD-L1/HER2/NTRK/KRAS 等），gene_symbol 填入基因符号
- ClinicalTrial：指南引用的关键临床试验NCT编号 -> trial_id
- TrialIndication：试验中的具体适应症条目，标注治疗线次和推荐层级
- Company：药物原研企业（指南中标注的厂家）
- Pathway：涉及的信号通路（KEGG ID优先）
- 关系重点：
  * Drug -[TREATS]-> Indication（在relation_props中标注 treatment_line 和 recommendation_level 和 evidence_category）
  * Drug -[TARGETS]-> Target（mechanism必填；在relation_props中附加evidence_category）
  * ClinicalTrial -[EVALUATES]-> TrialIndication
  * TrialIndication -[FOR_INDICATION]-> Indication
  * Target -[IN_PATHWAY]-> Pathway（role可填："上游"/"下游"/"平行"）
  * Target -[ASSOCIATED_WITH]-> Indication
  * Indication -[SUBTYPE_OF]-> Indication（疾病分型时使用，如胰腺癌分出"Pancreatic Adenocarcinoma"）
  * Company -[DEVELOPS]-> Drug（dev_role="原研", rights_role="权益方"）

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_pharma_company_news_prompt(text: str) -> str:
    """制药企业新闻（如Roche、其他药企官网新闻）：重点抽取 Company、Drug、ClinicalTrial、ApprovalEvent、DealEvent"""
    entity_section = _build_entity_type_section(_ALL_ENTITY_TYPES)
    return f"""你是制药企业新闻信息提取专家。请从以下制药企业官方新闻中抽取图谱实体和关系。

上方列出了全部支持的实体类型，请严格从上方列表中选择，不得使用列表之外的实体类型。

{entity_section}

{_RELATION_PROPS_GUIDE}
{_COMMON_EXTRACTION_RULES}
{_EMPTY_RESULT_GUIDANCE}

## 本来源特有规则
- 企业新闻通常聚焦单一主体（发布方），合作方/被投方作为独立 Company 实体抽取
- 不要抽取新闻中的"公司CEO/CSO/CMO"等个人名字为公司实体
- 研发阶段术语映射：Preclinical→临床前, IND Filed→IND申报, Phase I/II/III→对应 phase, NDA Submitted→NDA申报, Approved→已上市
- 企业财报中的财务数据不抽取为 DealEvent（除非明确是交易金额）

## 抽取重点
- Company：新闻发布药企、合作企业、投资方/被投方，name 用英文官方全称，中文名填入别名
- Drug：涉及药物（INN名入 name，研发代号入 aliases，商品名入 aliases），填写 phase_us/phase_eu/phase_cn
- ClinicalTrial：临床试验进展、结果公布（NCT编号入 trial_id）
- ApprovalEvent：监管批准（FDA、EMA、NMPA等），application_type 填 NDA/BLA/MAA
- DealEvent：企业合作、许可协议、并购交易，deal_category 准确标注
- DealItem：合作条款、首付款、里程碑付款
- DevelopmentProject：研发项目、合作开发计划
- 关系重点：
  * Company -[DEVELOPS]-> Drug（dev_role, rights_role, start_date必填）
  * Company -[SPONSORS]-> ClinicalTrial
  * Company -[PARTICIPATES_IN]-> DealEvent（role必填："转让方"/"受让方"/"合作方"/"许可方"/"被许可方"/"投资方"/"被投方"）
  * DealEvent -[HAS_ITEM]-> DealItem
  * DealItem -[INVOLVES_DRUG]-> Drug
  * Drug -[IN_TRIAL]-> ClinicalTrial
  * Drug -[HAS_APPROVAL]-> ApprovalEvent
  * Company -[PARTICIPATES_IN_PROJECT]-> DevelopmentProject（role必填："主导方"/"合作方"）
  * DevelopmentProject -[DEVELOPS]-> Drug

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


def get_general_extraction_prompt(text: str) -> str:
    """通用兜底 prompt，覆盖未定义来源"""
    entity_section = _build_entity_type_section(_ALL_ENTITY_TYPES)
    return f"""你是医药行业知识图谱信息提取专家。请从以下文档中抽取图谱实体和关系。

{entity_section}

{_RELATION_PROPS_GUIDE}
{_COMMON_EXTRACTION_RULES}
{_EMPTY_RESULT_GUIDANCE}

## 抽取要求
- 只抽取文档中明确提及的实体，不要推断或编造
- 每个实体必须有 entity_name（唯一标识）和 entity_type（从上方支持列表中选取）
- 关系的 from/to 下标必须对应 entities 数组中的实际位置
- relation_props 中的枚举字段必须使用规定值，不得自造
- entity_data 中找不到的字段填空字符串 "" 或 null
- 不确定的实体宁可不抽——空结果优于虚构实体

## 文档内容
{text}

{_OUTPUT_FORMAT}"""


# ── 来源 → Prompt 函数映射 ──────────────────────────────────────────────
_PROMPT_MAP: dict[str, callable] = {
    # 监管公告类
    "nmpa":                     get_nmpa_extraction_prompt,
    "cde":                      get_nmpa_extraction_prompt,
    "cde_zdyz":                 get_nmpa_extraction_prompt,
    "cde_xxgk":                 get_nmpa_extraction_prompt,

    # FDA相关
    "fda_press":                lambda t: get_fda_extraction_prompt(t, ""),
    "fda_drug_safety":          lambda t: get_fda_extraction_prompt(t, "drug_safety"),
    "fda_api_drug_label":       lambda t: get_fda_extraction_prompt(t, "drug_label"),
    "fda_api_enforcement":      lambda t: get_fda_extraction_prompt(t, "enforcement"),

    # 临床试验类
    "chictr":                   get_clinical_trial_extraction_prompt,
    "clinicaltrials":           get_clinical_trial_extraction_prompt,

    # 临床指南
    "nccn":                     get_nccn_extraction_prompt,

    # 学术文献
    "pubmed":                   get_pubmed_extraction_prompt,

    # 金融公告
    "hkex":                     get_hkex_extraction_prompt,

    # 行业新闻
    "fiercepharma":             get_fiercepharma_extraction_prompt,

    # PHIRDA相关
    "phirda_rd":                get_phirda_rd_industry_prompt,
    "phirda_industry":          get_phirda_rd_industry_prompt,
    "phirda_policy":            get_policy_extraction_prompt,
    "phirda_healthcare_reform": get_policy_extraction_prompt,

    # 药物数据库
    "drugbank":                 get_drugbank_extraction_prompt,

    # 制药企业新闻
    "roche":                    get_pharma_company_news_prompt,
}


def get_extraction_prompt(source_site: str, text: str, metadata: dict = None) -> str:
    """根据来源站点选择对应的抽取 prompt，未知来源走通用 prompt。"""
    fn = _PROMPT_MAP.get(source_site, get_general_extraction_prompt)
    params = inspect.signature(fn).parameters
    if metadata is not None and "metadata" in params:
        return fn(text, metadata=metadata)
    return fn(text)
